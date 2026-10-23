"""급등주 하루 해부: +50% 급등일의 1분봉 전체(전날 밤 데이마켓 ~ 애프터)로 패턴과 진입·청산 규칙을 검증한다.

대상: 일봉 고가가 전날 종가의 1.2배 이상인 날 ($0.1~$10), 최근 N일 — 신호(+20% 이상)를 볼 수 있는 날 전체라 결과로 고른 편향이 없다
  1. 하루 흐름: 처음 +20% 시점(세션), 고점 시각, 고점 대비 마감, 프리마켓 거래 비중, 회전율
  2. 개미털기: 처음 +30% 뒤 1차 고점 → 다음 신고가 전까지 최대 눌림(깊이·시간), 신고가 회복 비율
  3. VWAP: 정규장 VWAP 위 체류 비율, 마감 vs VWAP, VWAP 회복 후 신고가 비율
  4. 다음 날: 시가 갭, 고가, 종가 (급등일 종가 대비), 2~5일 최고·최저
  5. 진입 규칙 (1분봉, 신호 다음 봉 시가 × 1.01 체결) × 청산 규칙 → 1건당 수익·승률
판단에 미래 정보를 쓰지 않는다 (신호는 그 분까지의 봉만, 체결은 다음 봉).
"""
from __future__ import annotations

import json
import logging
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .bigruns import load_bars
from .research import ResearchDB

log = logging.getLogger(__name__)
SLIP = 1.01
COST = 1.0      # 왕복 비용 % (수수료·스프레드)


def _et():
    from zoneinfo import ZoneInfo
    return ZoneInfo("America/New_York")


def day_window(prev_d: str, d: str):
    et = _et()
    return (datetime.fromisoformat(prev_d).replace(hour=20, tzinfo=et),          # 전날 밤 데이마켓
            datetime.fromisoformat(d).replace(hour=4, tzinfo=et),                # 프리마켓
            datetime.fromisoformat(d).replace(hour=9, minute=30, tzinfo=et),     # 정규장
            datetime.fromisoformat(d).replace(hour=16, tzinfo=et),               # 애프터
            datetime.fromisoformat(d).replace(hour=20, tzinfo=et))


def fetch_day(client, rdb: ResearchDB, sym: str, prev_d: str, d: str) -> list[tuple]:
    """전날 20:00 ET ~ 당일 20:00 ET 1분봉 (캐시: bars_1m_done 의 d='full:<날짜>')."""
    start, *_, end = day_window(prev_d, d)
    key = f"full:{d}"
    if not rdb.q("SELECT 1 FROM bars_1m_done WHERE symbol=? AND d=?", (sym, key)):
        before, rows = end.isoformat(timespec="milliseconds"), []
        for _ in range(9):
            try:
                res = client.candles(sym, "1m", 200, before) or {}
            except Exception as e:
                log.debug("1분봉 실패 %s %s: %s", sym, d, e)
                return []
            page = res.get("candles", [])
            for cd in page:
                ts = datetime.fromisoformat(cd["timestamp"].replace("Z", "+00:00")).astimezone(timezone.utc)
                rows.append({"symbol": sym, "ts": ts.isoformat(), "open": float(cd["openPrice"]), "high": float(cd["highPrice"]),
                             "low": float(cd["lowPrice"]), "close": float(cd["closePrice"]), "volume": float(cd["volume"])})
            before = res.get("nextBefore")
            if not page or not before or datetime.fromisoformat(page[-1]["timestamp"].replace("Z", "+00:00")) <= start:
                break
        rdb.put("bars_1m", rows)
        rdb.put("bars_1m_done", [{"symbol": sym, "d": key}])
    u = lambda t: t.astimezone(timezone.utc).isoformat()
    return rdb.q("SELECT ts, open, high, low, close, volume FROM bars_1m WHERE symbol=? AND ts>=? AND ts<? ORDER BY ts",
                 (sym, u(start), u(end)))


def sess_of(ts: str, w) -> str:
    _, pre, reg, aft, _ = w
    u = lambda t: t.astimezone(timezone.utc).isoformat()
    return "DAY" if ts < u(pre) else "PRE" if ts < u(reg) else "REGULAR" if ts < u(aft) else "AFTER"


# ---------- 1~3. 하루 흐름 ----------
def anatomy(bars: list[tuple], pc: float, w) -> dict | None:
    if len(bars) < 20 or not pc:
        return None
    u = lambda t: t.astimezone(timezone.utc).isoformat()
    reg_open, aft = u(w[2]), u(w[3])
    out: dict = {}
    hod_i = max(range(len(bars)), key=lambda k: bars[k][2])
    hod = bars[hod_i][2]
    out["hod_pct"] = (hod / pc - 1) * 100
    out["hod_sess"] = sess_of(bars[hod_i][0], w)
    out["hod_min_after_open"] = (datetime.fromisoformat(bars[hod_i][0]) - w[2].astimezone(timezone.utc)).total_seconds() / 60
    f20 = next((b for b in bars if b[2] >= pc * 1.2), None)
    out["first20_sess"] = sess_of(f20[0], w) if f20 else None
    reg = [b for b in bars if reg_open <= b[0] < aft]
    tot_v = sum(b[5] for b in bars) or 1
    out["pre_vol_share"] = sum(b[5] for b in bars if b[0] < reg_open) / tot_v
    out["dollar"] = sum(b[4] * b[5] for b in bars)
    last = (reg or bars)[-1][4]
    out["close_vs_hod"] = (last / hod - 1) * 100
    out["close_pct"] = (last / pc - 1) * 100
    # 개미털기: 처음 +30% 이후 → 1차 고점 → 다음 신고가 전 최대 눌림
    k30 = next((k for k, b in enumerate(bars) if b[2] >= pc * 1.3), None)
    out["shakeout"] = None
    if k30 is not None:
        run_hi, run_hi_k, deepest, deep_k, legs = bars[k30][2], k30, 0.0, None, []
        for k in range(k30 + 1, len(bars)):
            h, l = bars[k][2], bars[k][3]
            if h > run_hi:
                if deepest <= -10:                       # 10% 이상 눌렸다가 신고가 → 한 번의 '털기' 완료
                    legs.append({"depth": deepest, "minutes": k - run_hi_k, "recovered": True})
                run_hi, run_hi_k, deepest = h, k, 0.0
            else:
                dd = (l / run_hi - 1) * 100
                if dd < deepest:
                    deepest, deep_k = dd, k
        if deepest <= -10:
            legs.append({"depth": deepest, "minutes": len(bars) - run_hi_k, "recovered": False})
        out["shakeout"] = legs
    # VWAP (정규장)
    if reg:
        cv = cp = 0.0
        above = 0
        for b in reg:
            cv += b[5]
            cp += (b[2] + b[3] + b[4]) / 3 * b[5]
            if cv and b[4] >= cp / cv:
                above += 1
        out["above_vwap_share"] = above / len(reg)
        out["close_vs_vwap"] = (reg[-1][4] / (cp / cv) - 1) * 100 if cv else None
    return out


# ---------- 5. 진입·청산 실험 ----------
def _vwap_series(bars):
    cv = cp = 0.0
    out = []
    for b in bars:
        cv += b[5]
        cp += (b[2] + b[3] + b[4]) / 3 * b[5]
        out.append(cp / cv if cv else b[4])
    return out


def entries(bars: list[tuple], pc: float, w) -> dict:
    """신호 → 체결 인덱스(다음 봉) 와 체결가. {규칙: (k, price, stop_ref)}"""
    u = lambda t: t.astimezone(timezone.utc).isoformat()
    reg_open = u(w[2])
    vw = _vwap_series(bars)
    out = {}
    # E1 눌림 매수: +30% 이후 고점 대비 −X% 눌렸다가, VWAP 위로 다시 올라서는 첫 봉
    for depth in (15, 20, 25, 35):
        hi, armed, low = 0.0, False, None
        for k, b in enumerate(bars[:-1]):
            hi = max(hi, b[2])
            if hi < pc * 1.3:
                continue
            if not armed and b[3] <= hi * (1 - depth / 100):
                armed, low = True, b[3]
            if armed:
                low = min(low, b[3])
                if b[4] > vw[k] and b[4] > bars[k - 1][4]:
                    out[f"E1 눌림 −{depth}% 후 VWAP 회복"] = (k + 1, bars[k + 1][1] * SLIP, low)
                    break
    # E2 고점 돌파: 정규장 10:00 이후, 그때까지의 고가를 거래량 동반(직전 30분 평균의 3배)으로 돌파
    hi = 0.0
    t10 = u(w[2] + timedelta(minutes=30))
    for k, b in enumerate(bars[:-1]):
        if b[0] >= t10 and hi >= pc * 1.3 and b[2] > hi:
            v30 = [x[5] for x in bars[max(0, k - 30):k]]
            if v30 and b[5] >= 3 * statistics.mean(v30):
                out["E2 10시 이후 고점 돌파"] = (k + 1, bars[k + 1][1] * SLIP, bars[k][3])
                break
        hi = max(hi, b[2])
    # E3 정규장 시가 매수 (프리에서 이미 +30% 이상이면)
    ko = next((k for k, b in enumerate(bars) if b[0] >= reg_open), None)
    if ko is not None and max(b[2] for b in bars[:ko] or [(0, 0, 0)]) >= pc * 1.3:
        out["E3 프리 +30% 후 정규장 시가"] = (ko, bars[ko][1] * SLIP, min(b[3] for b in bars[:ko]))
    # E4 첫 +20% 돌파 즉시 (추격, 비교용)
    k20 = next((k for k, b in enumerate(bars[:-1]) if b[2] >= pc * 1.2), None)
    if k20 is not None:
        out["E4 첫 +20% 추격"] = (k20 + 1, bars[k20 + 1][1] * SLIP, pc)
    return out


def exits(bars: list[tuple], k: int, entry: float, stop_ref: float, tp: float | None, sl: float | None,
          use_ref_stop: bool) -> float:
    """체결 다음 봉부터. 익절 +tp%, 손절 −sl% (또는 눌림 저점 −3%), 둘 다면 손절 먼저, 끝까지 안 걸리면 마지막 봉 종가."""
    stop = stop_ref * 0.97 if use_ref_stop else (entry * (1 - sl / 100) if sl else None)
    target = entry * (1 + tp / 100) if tp else None
    for b in bars[k + 1:]:
        if stop and b[3] <= stop:
            return (min(b[1], stop) / entry - 1) * 100
        if target and b[2] >= target:
            return (max(b[1], target) / entry - 1) * 100
    return (bars[-1][4] / entry - 1) * 100


def summarize(xs: list[float]) -> dict | None:
    if not xs:
        return None
    return {"n": len(xs), "mean": statistics.mean(xs), "median": statistics.median(xs), "win": sum(x > 0 for x in xs) / len(xs)}


def run(cfg, days: int = 200, min_mult: float = 1.2, max_events: int = 3000) -> dict:
    from .__main__ import make_client
    rdb = ResearchDB(Path(cfg.state_dir) / "research.db")
    bars = load_bars(rdb)
    client = make_client(cfg, "other")
    client.rate_share = 0.35
    cutoff = (datetime.now() - timedelta(days=days)).date().isoformat()
    events = []
    for s, b in bars.items():
        for i in range(1, len(b) - 5):
            d, o, h, l, c, v = b[i]
            pc = b[i - 1][4]
            if d >= cutoff and pc and 0.1 <= pc <= 10 and h >= pc * min_mult:
                events.append((s, i))
    events = sorted(events, key=lambda e: bars[e[0]][e[1]][0], reverse=True)[:max_events]
    log.info("급등일 %d건 (최근 %d일, 고가 ≥ 전날 종가 × %.1f)", len(events), days, min_mult)
    rows, trades = [], defaultdict(list)
    exit_rules = [("익절 +20%", 20, None, False), ("익절 +30%", 30, None, False), ("익절 +50%", 50, None, False),
                  ("익절 +30% · 손절 −15%", 30, 15, False), ("익절 +30% · 손절 −25%", 30, 25, False),
                  ("익절 +50% · 손절 −25%", 50, 25, False), ("익절 +30% · 손절 눌림저점", 30, None, True),
                  ("익절 +50% · 손절 눌림저점", 50, None, True), ("장 마감까지 보유", None, None, False)]
    for n, (s, i) in enumerate(events):
        b = bars[s]
        prev_d, d, pc = b[i - 1][0], b[i][0], b[i - 1][4]
        w = day_window(prev_d, d)
        m = fetch_day(client, rdb, s, prev_d, d)
        a = anatomy(m, pc, w)
        if not a:
            continue
        nxt = b[i + 1]
        c1 = b[i][4]
        a.update({"symbol": s, "d": d, "pc": pc,
                  "d2_gap": (nxt[1] / c1 - 1) * 100, "d2_high": (nxt[2] / c1 - 1) * 100, "d2_close": (nxt[4] / c1 - 1) * 100,
                  "d5_high": (max(x[2] for x in b[i + 1:i + 6]) / c1 - 1) * 100,
                  "d5_low": (min(x[3] for x in b[i + 1:i + 6]) / c1 - 1) * 100,
                  "close_loc": (c1 - b[i][3]) / (b[i][2] - b[i][3]) if b[i][2] > b[i][3] else None})
        rows.append(a)
        for ename, (k, entry, ref) in entries(m, pc, w).items():
            for xname, tp, sl, ref_stop in exit_rules:
                if ref_stop and ename.startswith("E4"):
                    continue
                trades[(ename, xname)].append(exits(m, k, entry, ref, tp, sl, ref_stop) - COST)
        if (n + 1) % 100 == 0:
            log.info("해부 %d/%d", n + 1, len(events))
    rep = build_report(rows, trades, days, min_mult)
    out = Path(cfg.state_dir) / "research" / f"anatomy-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(rep, ensure_ascii=False, default=str), encoding="utf-8")
    log.info("결과: %s", out)
    return rep


def build_report(rows: list[dict], trades: dict, days: int, min_mult: float) -> dict:
    med = lambda k, xs=None: statistics.median([r[k] for r in (xs or rows) if r.get(k) is not None]) if rows else None
    share = lambda fn, xs=None: sum(1 for r in (xs or rows) if fn(r)) / len(xs or rows) if (xs or rows) else None
    shake = [lg for r in rows for lg in (r.get("shakeout") or [])]
    with_shake = [r for r in rows if r.get("shakeout")]
    depth_buckets = []
    for lo, hi in ((-15, -10), (-25, -15), (-35, -25), (-50, -35), (-100, -50)):
        g = [lg for lg in shake if lo < lg["depth"] <= hi]
        if g:
            depth_buckets.append({"range": f"{hi}% ~ {lo}%", "n": len(g), "recovered": sum(x["recovered"] for x in g) / len(g),
                                  "minutes_med": statistics.median(x["minutes"] for x in g)})
    t = [{"entry": e, "exit": x, **summarize(v)} for (e, x), v in trades.items() if v]
    t.sort(key=lambda r: -r["mean"])
    return {
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"), "days": days, "min_mult": min_mult,
        "n": len(rows),
        "first20": dict(Counter(r.get("first20_sess") or "없음" for r in rows)),
        "hod_sess": dict(Counter(r["hod_sess"] for r in rows)),
        "hod_min_after_open_med": med("hod_min_after_open", [r for r in rows if r["hod_sess"] == "REGULAR"]),
        "hod_pct_med": med("hod_pct"), "close_pct_med": med("close_pct"), "close_vs_hod_med": med("close_vs_hod"),
        "close_above_vwap": share(lambda r: (r.get("close_vs_vwap") or -1) > 0),
        "above_vwap_share_med": med("above_vwap_share"), "pre_vol_share_med": med("pre_vol_share"),
        "dollar_med": med("dollar"),
        "shake_any": len(with_shake) / len(rows) if rows else None,
        "shake_depth_med": statistics.median(x["depth"] for x in shake) if shake else None,
        "shake_recovered": sum(x["recovered"] for x in shake) / len(shake) if shake else None,
        "shake_buckets": depth_buckets,
        "d2": {"gap_med": med("d2_gap"), "high_med": med("d2_high"), "close_med": med("d2_close"),
               "gap_up": share(lambda r: r["d2_gap"] > 0), "high_above_10": share(lambda r: r["d2_high"] >= 10),
               "d5_high_med": med("d5_high"), "d5_low_med": med("d5_low")},
        "d2_by_close_loc": [{"group": g, "n": len(xs), "d2_close_med": med("d2_close", xs), "d2_high_med": med("d2_high", xs),
                             "gap_up": share(lambda r: r["d2_gap"] > 0, xs)}
                            for g, xs in (("고가권 마감 (상위 25%)", [r for r in rows if (r.get("close_loc") or 0) >= 0.75]),
                                          ("중간", [r for r in rows if 0.25 <= (r.get("close_loc") or 0) < 0.75]),
                                          ("저가권 마감 (하위 25%)", [r for r in rows if (r.get("close_loc") or 1) < 0.25])) if xs],
        "trades": t,
        "events": sorted(({k: v for k, v in r.items() if k != "shakeout"} | {"shake_n": len(r.get("shakeout") or [])}
                          for r in rows), key=lambda r: r["d"], reverse=True)[:300],
    }


def format_report(r: dict) -> str:
    p = lambda v: "-" if v is None else f"{v:+.1f}%"
    sh = lambda v: "-" if v is None else f"{v * 100:.0f}%"
    L = [f"급등일 해부 {r['n']}건 (최근 {r['days']}일, 고가 ≥ 전날 종가 × {r['min_mult']})", "",
         "■ 하루 흐름",
         f"  처음 +20%: " + ", ".join(f"{k} {v}" for k, v in sorted(r["first20"].items(), key=lambda x: -x[1])),
         f"  그날 고점: " + ", ".join(f"{k} {v}" for k, v in sorted(r["hod_sess"].items(), key=lambda x: -x[1]))
         + f" · 정규장 고점은 개장 {r['hod_min_after_open_med']:.0f}분 뒤(중앙값)" if r["hod_min_after_open_med"] is not None else "",
         f"  고점 {p(r['hod_pct_med'])} → 마감 {p(r['close_pct_med'])} (고점 대비 {p(r['close_vs_hod_med'])}), "
         f"VWAP 위 마감 {sh(r['close_above_vwap'])}, 프리마켓 거래 비중 {sh(r['pre_vol_share_med'])}", "",
         "■ 개미털기 (+30% 이후 고점 대비 10% 이상 눌림)",
         f"  눌림이 한 번 이상 있던 날 {sh(r['shake_any'])}, 눌림 깊이 중앙값 {p(r['shake_depth_med'])}, 다시 신고가 {sh(r['shake_recovered'])}"]
    for bk in r["shake_buckets"]:
        L.append(f"  깊이 {bk['range']:<14} {bk['n']:>5}회  신고가 회복 {bk['recovered'] * 100:3.0f}%  고점→회복/끝 {bk['minutes_med']:.0f}분")
    d2 = r["d2"]
    L += ["", "■ 다음 날 (급등일 종가 대비)",
          f"  시가 갭 {p(d2['gap_med'])} (갭 상승 {sh(d2['gap_up'])}), 고가 {p(d2['high_med'])} (+10% 이상 {sh(d2['high_above_10'])}), 종가 {p(d2['close_med'])}",
          f"  5일 안 최고 {p(d2['d5_high_med'])}, 최저 {p(d2['d5_low_med'])}"]
    for g in r["d2_by_close_loc"]:
        L.append(f"  {g['group']:<16} {g['n']:>5}건  다음 날 종가 {p(g['d2_close_med'])} 고가 {p(g['d2_high_med'])} 갭 상승 {sh(g['gap_up'])}")
    L += ["", f"■ 진입 × 청산 (1분봉, 다음 봉 시가+1% 체결, 왕복 비용 {COST}%) — 평균 수익 순"]
    for x in r["trades"][:25]:
        L.append(f"  {x['mean']:+6.1f}% (중앙값 {x['median']:+5.1f}%, 승률 {x['win'] * 100:3.0f}%, {x['n']}건)  {x['entry']} → {x['exit']}")
    return "\n".join(L)
