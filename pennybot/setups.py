"""모멘텀 단타 진입법 재현 (남들이 쓰는 방식) — +300% 급등일(모양 보기) vs +20% 급등일 전체(신호에 힘이 있나).

진입 (각각 그날 첫 신호 하나, 1분봉, 신호 봉 다음에 '트리거 가격' 이상이면 체결 = max(시가, 트리거) + 슬리피지):
  F 첫 눌림 돌파(불플래그): 전날 대비 +30% 넘은 첫 상승 뒤, 첫 상승분의 50% 이내로 눌리고(거래량 감소),
                           VWAP 위에서 직전 봉 고가를 넘는 첫 봉. 손절 = 눌림 저점 −1%
  P 프리마켓 고점 돌파: 정규장 시작 뒤 2시간 안에 프리마켓 고가를 넘을 때. 손절 = 돌파 봉 직전 5분 저점
  O 개장 5분 고점 돌파: 09:30~09:35 고가를 넘을 때 (2시간 안). 손절 = 그 5분 저가
  V VWAP 회복: 10분 넘게 VWAP 아래 있다가 거래량(직전 30분 평균의 2배+) 실린 봉이 VWAP 위로 마감. 손절 −8%
매도 (단타식): 2R(손절 폭의 2배)에 절반 → 나머지는 직전 봉 저가를 깨는 첫 봉(새 저점)에서 정리, 손절은 그대로,
              정규장 마감에 남은 것 정리. 비용 1%.
"""
from __future__ import annotations

import statistics
from datetime import timedelta, timezone

SLIP = 1.005


def vwap_series(bars):
    cv = cp = 0.0
    out = []
    for b in bars:
        cv += b[5]
        cp += (b[2] + b[3] + b[4]) / 3 * b[5]
        out.append(cp / cv if cv else b[4])
    return out


def trade(bars, k, trigger, stop):
    """k 봉에서 trigger 이상이면 체결 → (수익 %, R배수, 최고 %)."""
    o, h = bars[k][1], bars[k][2]
    if h < trigger:
        return None
    entry = max(o, trigger) * SLIP
    if stop >= entry:
        return None
    risk = entry - stop
    pos, real, took = 1.0, 0.0, False
    best = entry
    for j in range(k, len(bars)):
        t, o, h, l, c, v = bars[j]
        if l <= stop:
            px_ = min(o, stop) if j > k else stop
            real += pos * (px_ / entry - 1) * 100
            return real, real / (risk / entry * 100), (best / entry - 1) * 100
        best = max(best, h)
        if not took and h >= entry + 2 * risk:
            real += 0.5 * (2 * risk / entry * 100)
            pos, took = 0.5, True
        if took and j > k and l < bars[j - 1][3]:              # 새 저점 → 나머지 정리
            px_ = min(o, bars[j - 1][3])
            real += pos * (px_ / entry - 1) * 100
            return real, real / (risk / entry * 100), (best / entry - 1) * 100
    real += pos * (bars[-1][4] / entry - 1) * 100
    return real, real / (risk / entry * 100), (best / entry - 1) * 100


def setups(bars, pc, w) -> dict:
    u = lambda t: t.astimezone(timezone.utc).isoformat()
    reg_open, reg_close = u(w[2]), u(w[3])
    vw = vwap_series(bars)
    out = {}
    # F 첫 눌림 돌파
    leg_lo, hi, hi_k, in_pb, pb_lo, leg_vol = pc, 0.0, None, False, None, []
    for k, b in enumerate(bars[:-1]):
        if b[0] >= reg_close:
            break
        if hi_k is None:
            leg_vol.append(b[5])
            if b[2] >= pc * 1.3:
                hi, hi_k = b[2], k
            continue
        if not in_pb:
            if b[2] > hi:
                hi, hi_k = b[2], k
                leg_vol.append(b[5])
                continue
            in_pb, pb_lo, pb_vol = True, b[3], [b[5]]
        else:
            pb_lo = min(pb_lo, b[3])
            pb_vol.append(b[5])
        retr = (hi - pb_lo) / (hi - leg_lo) if hi > leg_lo else 1
        if retr > 0.5:
            break                                               # 깊게 빠짐 → 불플래그 아님
        vol_ok = statistics.mean(pb_vol) < 0.7 * statistics.mean(leg_vol[-max(5, len(leg_vol) // 3):] or [1])
        nb = bars[k + 1]
        if len(pb_vol) >= 2 and vol_ok and b[4] > vw[k]:
            r = trade(bars, k + 1, b[2] + 0.0001, pb_lo * 0.99)
            if r:
                out["F 첫 눌림 돌파"] = r
            break
    # P 프리마켓 고점 돌파 / O 개장 5분 고점 돌파
    pre = [b for b in bars if b[0] < reg_open]
    reg = [(k, b) for k, b in enumerate(bars) if reg_open <= b[0] < reg_close]
    if pre and reg and max(b[2] for b in pre) >= pc * 1.2:     # 갭 상승 종목만: 정규장 전에 이미 +20% (매수 전에 아는 사실)
        pmh = max(b[2] for b in pre)
        t2 = u(w[2] + timedelta(hours=2))
        for k, b in reg:
            if b[0] > t2:
                break
            if b[2] > pmh and k + 0 < len(bars):
                lo5 = min(x[3] for x in bars[max(0, k - 5):k + 1])
                r = trade(bars, k, pmh + 0.0001, lo5)
                if r:
                    out["P 프리마켓 고점 돌파"] = r
                break
        first5 = [b for _, b in reg if b[0] < u(w[2] + timedelta(minutes=5))]
        if first5:
            oh, ol = max(b[2] for b in first5), min(b[3] for b in first5)
            for k, b in reg:
                if b[0] < u(w[2] + timedelta(minutes=5)):
                    continue
                if b[0] > t2:
                    break
                if b[2] > oh:
                    r = trade(bars, k, oh + 0.0001, ol)
                    if r:
                        out["O 개장 5분 고점 돌파"] = r
                    break
    # V VWAP 회복
    below = 0
    for k, b in enumerate(bars[:-1]):
        if b[0] >= reg_close:
            break
        if b[4] < vw[k]:
            below += 1
            continue
        if below >= 10 and k >= 30 and max(x[2] for x in bars[:k + 1]) >= pc * 1.2:
            avg = statistics.mean(x[5] for x in bars[k - 30:k]) or 1
            if b[5] >= 2 * avg:
                nb = bars[k + 1]
                r = trade(bars, k + 1, nb[1], nb[1] * SLIP * 0.92)
                if r:
                    out["V VWAP 회복"] = r
                break
        below = 0
    return out


def shape(bars, pc, w) -> dict | None:
    """+300% 날 모양: 첫 상승(+30% 돌파 → 첫 고점), 첫 눌림 깊이·시간·거래량, 그 뒤 신고가 여부."""
    hi_k = next((k for k, b in enumerate(bars) if b[2] >= pc * 1.3), None)
    if hi_k is None:
        return None
    hi, k = bars[hi_k][2], hi_k
    while k + 1 < len(bars) and bars[k + 1][2] >= hi * 0.97:    # 3% 안쪽 흔들림은 같은 상승
        k += 1
        hi = max(hi, bars[k][2])
    leg_end = k
    lo, j = hi, k
    while j + 1 < len(bars) and bars[j + 1][2] <= hi:
        j += 1
        lo = min(lo, bars[j][3])
    leg_vol = statistics.mean(b[5] for b in bars[max(0, hi_k - 30):leg_end + 1]) or 1
    pb_vol = statistics.mean(b[5] for b in bars[leg_end + 1:j + 1]) if j > leg_end else None
    day_hi = max(b[2] for b in bars)
    return {"first_hi_pct": (hi / pc - 1) * 100, "pb_depth": (lo / hi - 1) * 100,
            "pb_retr": (hi - lo) / (hi - pc) * 100 if hi > pc else None, "pb_min": j - leg_end,
            "pb_vol_ratio": pb_vol / leg_vol if pb_vol else None, "new_high_after": day_hi > hi * 1.001,
            "more_after_pct": (day_hi / hi - 1) * 100}


def run(cfg):
    import json
    from datetime import datetime
    from pathlib import Path
    from .__main__ import make_client
    from .anatomy import day_window, fetch_day
    from .bigruns import load_bars
    from .research import ResearchDB
    rdb = ResearchDB(Path(cfg.state_dir) / "research.db")
    bars = load_bars(rdb)
    client = make_client(cfg, "other")
    client.rate_share = 0.3
    cut = (datetime.now() - timedelta(days=365)).date().isoformat()
    big, base = [], []
    for s, b in bars.items():
        for i in range(1, len(b)):
            pc = b[i - 1][4]
            if b[i][0] < cut or not pc or pc < 0.1:
                continue
            if b[i][2] >= pc * 4:
                big.append((s, i))
            elif b[i][2] >= pc * 1.2 and b[i][0] >= (datetime.now() - timedelta(days=200)).date().isoformat():
                base.append((s, i))
    res = {"big": {}, "base": {}}
    shapes = []
    for grp, items in (("big", big), ("base", base)):
        for s, i in items:
            b = bars[s]
            d0, d1, pc = b[i - 1][0], b[i][0], b[i - 1][4]
            w = day_window(d0, d1)
            m = fetch_day(client if grp == "big" else None, rdb, s, d0, d1)
            if len(m) < 30:
                continue
            for name, r in setups(m, pc, w).items():
                res[grp].setdefault(name, []).append(r)
            if grp == "big":
                sh = shape(m, pc, w)
                if sh:
                    shapes.append(sh)
    rep = {"n_big": len(big), "n_base": len(base), "shapes": shapes, "res": res}
    Path(cfg.state_dir, "research", f"setups-{datetime.now():%Y%m%d-%H%M%S}.json").write_text(json.dumps(rep), encoding="utf-8")
    return rep


def format_report(rep) -> str:
    sh = rep["shapes"]
    med = lambda k: statistics.median(x[k] for x in sh if x.get(k) is not None)
    L = [f"■ 장중 +300% 날 {len(sh)}건의 모양",
         f"  첫 상승 고점 +{med('first_hi_pct'):.0f}% → 첫 눌림 {med('pb_depth'):.0f}% (첫 상승분의 {med('pb_retr'):.0f}% 되돌림) · {med('pb_min'):.0f}분",
         f"  눌림 중 거래량 = 상승 때의 {med('pb_vol_ratio') * 100:.0f}% · 그 뒤 신고가 간 비율 {sum(x['new_high_after'] for x in sh) / len(sh) * 100:.0f}%"
         f" · 첫 고점에서 더 간 폭 중앙값 +{med('more_after_pct'):.0f}%", ""]
    L.append(f"■ 진입법 (2R 절반 익절 → 새 저점 봉 정리, 손절 고정, 비용 1%)")
    L.append(f"  {'진입':<20} {'+300% 날 (결과 알고 고름)':>30} │ {'+20% 날 전체 (실제 성능)':>30}")
    names = sorted(set(rep["res"]["big"]) | set(rep["res"]["base"]))
    def cell(xs):
        if not xs:
            return f"{'-':>30}"
        r = [x[0] - 1.0 for x in xs]
        return f"{len(r):>5}건 평균 {statistics.mean(r):+5.1f}% 승률 {sum(v > 0 for v in r) / len(r) * 100:3.0f}%"
    for n in names:
        L.append(f"  {n:<20} {cell(rep['res']['big'].get(n)):>30} │ {cell(rep['res']['base'].get(n)):>30}")
    return "\n".join(L)
