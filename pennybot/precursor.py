"""급등 전조 점수: 과거 +500% 급등 사례에서 찾은 증상을 종목마다 켜졌는지 본다.

일봉 (전날 종가까지, 하루 한 번):
  ① 크게 빠짐     60일 고점 대비 −50% 이하
  ② 흔들림 큼     30일 하루 변동폭 평균 10%+ 또는 갭 하락(−10%) 2회+ 또는 하루 −20% 폭락 이력
  ③ 튄 이력       60일 안에 하루 +30% (고가 기준)
  ④ 싼 소외주     $2 미만
  ⑤ 조용히 흘러내림  10일 −10% 이하 + 최근 10일 거래량이 60일 평균의 절반 이하
  ⑥ 거래량 늘기 시작 전날 거래량이 20일 평균의 1.5배+ (3배+ 면 2점)
  ⑦ 고점 근처(60일 고점 대비 −20% 이내)는 0점 — 이런 데서는 거의 안 터졌다
  점수: 크게 빠짐 2 · 튄 이력 2 · 거래량 급증 +2 · 나머지 1 (최대 10). 평소 거래량 = 중앙값
장중 (1분봉, 데이·프리·정규장):
  ⑧ 거래 깨어남   최근 60분 분당 거래량이 그 이전(같은 날)의 3배 이상, 최근 60분 거래 있던 분 5분 이상
  ⑨ 슬금슬금 상승  최근 60분 +3% 이상, 최근 15분 +1.5% 이상
  ⑩ 이미 +10%    전날 종가 대비 +10% 이상 (늦었을 수 있음 — 참고)
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone

# 과거 +500% 사례에서 비교군보다 훨씬 흔했던 증상일수록 무겁게 (최대 10점)
WEIGHT = {"crashed": 2, "spiked": 2, "vol_up_strong": 2, "vol_up": 1, "volatile": 1, "bleed": 1, "cheap": 1}
DAILY_LABELS = {
    "vol_up_strong": "거래량 폭발", "vol_up": "거래량 상승", "crashed": "낙폭과대", "spiked": "급등 이력",
    "volatile": "고변동", "bleed": "거래 마름", "cheap": "동전주", "wake": "거래 깨어남",
}
PATTERN_LABELS = {"compress": "응축", "accum": "매집 의심", "base": "바닥 다지기"}
MAX_TURNOVER = 1000.0        # 하루 회전율이 이보다 크면 발행주식 수가 틀린 것으로 본다 (%)


def times(v: float | None) -> str:
    """거래량 배수 표시 (평소 거래가 거의 없으면 수천 배가 나오므로 100배 넘으면 '100배+')."""
    if v is None:
        return "-"
    return "100배+" if v >= 100 else f"{v:.1f}배"
LIVE_LABELS = {"wake": "거래 급증", "creep": "매수세 유입", "up10": "+10% 돌파", "money": "거래대금 터짐", "burst": "⚡ 1분 거래 터짐"}
# '거래대금 터짐': 3분 거래대금 $5만+ 또는 3분 회전율 0.5%+ (전날 대비 +5% 이상일 때). 캐시된 1분봉 3,951일 비교:
#   +10% 돌파보다 헛알림이 적고(그날 +200% 비율 8.8% → 11.4%), 그날 +200% 300건 중 292건에 켜짐 (켜질 때 중앙값 +39%).
MONEY_3M = 5e4
TURN_3M = 0.5

# 급등 후보 등급 — 최근 200일 일봉 검증 (다음 날 장중 +200% 간 비율, 아무것도 안 거르면 0.08%, +200% 전체 197건).
# 특징 24개를 하나씩·조합으로 보고, 앞 70% 기간에서 고른 조합이 뒤 30% 기간에도 유지되는지 확인했다.
#   3 (★★★) $2 미만 + 60일 고점 −50% 이하 + 전날 거래량 5배+ + 전날 회전율 10%+ → 하루 ~10종목, 2.3% (뒤 기간 1.9%), 32건
#   2 (★★)  발행 1천만 주 미만 + 거래량 10배+ + −50% 이하 (★★★ 아닌 것)     → 하루 ~12종목, 1.3%, 23건
#   1 (★)   발행 1천만 주 미만 + (거래량 10배+ 또는 5배+ & −50% 이하)        → 하루 ~19종목, 0.7%, 18건
# 셋 합쳐 하루 ~42종목에 +200% 의 37%. 나머지는 전날 일봉에 티가 안 남 (장중 감시로 잡아야 함).
TIER_LABEL = {3: "급등 후보 ★★★", 2: "급등 후보 ★★", 1: "급등 후보 ★"}


def tier(metrics: dict, shares: float | None, price: float | None = None) -> int:
    """급등 후보 등급. 일봉 22개월(2026-04~10, 종목·일 20.8만) 다음 날 +100% 확률 — 기준 0.2%:
    회전율 30%+ 3.8% · 거래 깨어남 1.7% · 둘 다 5.3%. 예전 ★ 조건만으로는 +100% 의 22% 만 잡혔고, 두 조건을 더하면 45% 안팎."""
    if not shares:
        return 0
    vr, dd = metrics.get("vol_ratio") or 0, metrics.get("dd60") or 0
    turn = (metrics.get("volume") or 0) / shares * 100
    t30, wake = 30 <= turn <= MAX_TURNOVER, bool(metrics.get("wake"))
    if (t30 and wake) or (price and price < 2 and dd <= -50 and vr >= 5 and 10 <= turn <= MAX_TURNOVER):
        return 3
    t = 0
    if shares < 1e7:
        t = 2 if vr >= 10 and dd <= -50 else 1 if vr >= 10 or (vr >= 5 and dd <= -50) else 0
    if t30:
        t = max(t, 2)
    if wake:
        t = max(t, 1)
    return t


def daily(bars: list[tuple]) -> dict | None:
    """bars: [(d, o, h, l, c, v)] 오래된 → 최신, 마지막 = 전날(D0). 61개 이상 필요."""
    if len(bars) < 61:
        return None
    d, o, h, l, c, v = bars[-1]
    if c <= 0:
        return None
    cl = [x[4] for x in bars]
    w60, w30 = bars[-60:], bars[-30:]
    hi60 = max(x[2] for x in w60)
    dd60 = (c / hi60 - 1) * 100
    prev = cl[-31:-1]
    rng = [(x[2] - x[3]) / p * 100 for x, p in zip(w30, prev) if p]
    gaps_dn = sum(1 for x, p in zip(w30, prev) if p and x[1] / p <= 0.9)
    max_dn = min((x[4] / p - 1) * 100 for x, p in zip(w30, prev) if p)
    spikes = sum(1 for k in range(len(bars) - 60, len(bars)) if cl[k - 1] and bars[k][2] / cl[k - 1] >= 1.3)
    # 평소 거래량은 중앙값 (펌핑 날 하루가 평균을 부풀리지 않게)
    v20 = statistics.median(x[5] for x in bars[-21:-1]) or 0
    v60 = statistics.median(x[5] for x in w60) or 0
    v10 = statistics.median(x[5] for x in bars[-10:])
    ret10 = (c / cl[-11] - 1) * 100 if cl[-11] else 0
    vol_ratio = v / v20 if v20 else 0
    # 거래 깨어남: 직전 5일 평균 거래량이 60일 평균의 절반 밑으로 말랐다가 전날 그 5일 평균의 3배 (다음 날 +100% 1.7%, 기준의 8배)
    v5m, v60m = statistics.mean(x[5] for x in bars[-6:-1]), statistics.mean(x[5] for x in bars[-61:-1])
    wake = v60m > 0 and v5m < v60m * 0.5 and v >= 3 * max(v5m, 1)
    sig = {
        "crashed": dd60 <= -50,
        "volatile": (statistics.mean(rng) if rng else 0) >= 10 or gaps_dn >= 2 or max_dn <= -20,
        "spiked": spikes >= 1,
        "cheap": c < 2,
        "bleed": ret10 <= -10 and v60 > 0 and v10 / v60 <= 0.5,
        "vol_up": vol_ratio >= 1.5,
        "vol_up_strong": vol_ratio >= 3,
        "wake": wake,
    }
    # 차트 패턴 (점수에는 안 넣고 태그로): 응축 · 매집 의심 · 바닥 다지기
    rng_pct = [(x[2] - x[3]) / x[4] * 100 for x in bars[-60:] if x[4]]
    box10 = (max(x[2] for x in bars[-10:]) / min(x[3] for x in bars[-10:]) - 1) * 100 if min(x[3] for x in bars[-10:]) > 0 else 999
    sig["compress"] = len(rng_pct) >= 60 and statistics.mean(rng_pct[-10:]) <= 0.6 * statistics.mean(rng_pct[:50]) and box10 <= 30
    w20 = bars[-20:]
    up_v = sum(x[5] for k, x in enumerate(w20) if x[4] > bars[-21 + k][4])
    dn_v = sum(x[5] for k, x in enumerate(w20) if x[4] < bars[-21 + k][4])
    ret20 = (c / cl[-21] - 1) * 100 if cl[-21] else 0
    sig["accum"] = dn_v > 0 and up_v / dn_v >= 1.5 and ret20 <= 10       # 오를 때 거래가 더 많은데 가격은 안 오름
    lows = [x[3] for x in bars[-10:]]
    sig["base"] = dd60 <= -40 and min(lows[5:]) >= min(lows[:5]) and ret10 > -10   # 낙폭 뒤 저점이 더 안 깨짐
    last_spike = next(((bars[k][0], (bars[k][2] / cl[k - 1] - 1) * 100) for k in range(len(bars) - 1, len(bars) - 61, -1)
                       if cl[k - 1] and bars[k][2] / cl[k - 1] >= 1.3), (None, None))
    near_high = dd60 > -20
    score = 0 if near_high else sum(WEIGHT.get(k, 0) for k, on in sig.items() if on)
    return {"d0": d, "close": c, "score": score, "near_high": near_high,
            "signals": [k for k, on in sig.items() if on],
            "metrics": {"dd60": dd60, "range30": statistics.mean(rng) if rng else None, "gaps_dn": gaps_dn, "max_dn": max_dn,
                        "spikes60": spikes, "ret10": ret10, "vol_ratio": vol_ratio, "vol10_60": v10 / v60 if v60 else None,
                        "dollar20": statistics.mean(x[4] * x[5] for x in bars[-20:]),
                        "volume": v, "last_spike_d": last_spike[0], "last_spike_pct": last_spike[1],
                        "box10": box10, "updown20": up_v / dn_v if dn_v else None, "wake": wake}}


def live(minute_bars: list[tuple], prev_close: float, now: datetime | None = None, shares: float | None = None) -> dict:
    """minute_bars: [(ts_iso, o, h, l, c, v)] 시간순 (오늘 세션들). → {'signals': [...], 'metrics': {...}}"""
    out = {"signals": [], "metrics": {}}
    if not minute_bars or not prev_close:
        return out
    now = now or datetime.fromisoformat(minute_bars[-1][0])
    t60, t15 = now - timedelta(minutes=60), now - timedelta(minutes=15)
    ts = lambda b: datetime.fromisoformat(b[0])
    last60 = [b for b in minute_bars if ts(b) > t60]
    last15 = [b for b in minute_bars if ts(b) > t15]
    before = [b for b in minute_bars if ts(b) <= t60]
    price = minute_bars[-1][4]
    m = {"price": price, "chg": (price / prev_close - 1) * 100,
         "active_min_60": len([b for b in last60 if b[5] > 0])}
    if before:
        span = max(1.0, (t60 - ts(before[0])).total_seconds() / 60)
        vb = sum(b[5] for b in before) / span
        va = sum(b[5] for b in last60) / 60
        m["vol_x"] = va / vb if vb else (99.0 if va else 0.0)
    m["ret60"] = (price / last60[0][1] - 1) * 100 if last60 else None
    m["ret15"] = (price / last15[0][1] - 1) * 100 if last15 else None
    m["dollar_today"] = sum(b[4] * b[5] for b in minute_bars)
    m["vol_today"] = sum(b[5] for b in minute_bars)
    last3 = [b for b in minute_bars if ts(b) > now - timedelta(minutes=3)]
    m["vol_3m"] = sum(b[5] for b in last3)
    m["dollar_3m"] = sum(b[4] * b[5] for b in last3)
    if (m.get("vol_x") or 0) >= 3 and m["active_min_60"] >= 5:
        out["signals"].append("wake")
    if (m["ret60"] or 0) >= 3 and (m["ret15"] or 0) >= 1.5:
        out["signals"].append("creep")
    if m["chg"] >= 10:
        out["signals"].append("up10")
    turn3 = m["vol_3m"] / shares * 100 if shares else 0
    if m["chg"] >= 5 and (m["dollar_3m"] >= MONEY_3M or TURN_3M <= turn3 <= MAX_TURNOVER):
        out["signals"].append("money")
    out["metrics"] = m
    return out


def money_at(bars: list[tuple], k: int, prev_close: float, shares: float | None) -> bool:
    """k 번째 1분봉에서 '거래대금 터짐' 조건 (live 의 money 와 같음)."""
    last3 = bars[max(0, k - 2):k + 1]
    d3, v3 = sum(b[4] * b[5] for b in last3), sum(b[5] for b in last3)
    turn3 = v3 / shares * 100 if shares else 0
    return (bars[k][4] / prev_close - 1) * 100 >= 5 and (d3 >= MONEY_3M or TURN_3M <= turn3 <= MAX_TURNOVER)


def alert_grade(ts: str, tier_: int, price: float | None, vol_ratio: float | None) -> tuple[str, list[str]]:
    """급등 전조 알림 등급 (장중 재학습 결과, 첫 1분 터짐 476건): 프리 06~09시 터짐(+30% 먼저 41% vs 정규장 8%),
    ★★★, $5 미만, 전날 거래량 50배+ 일수록 이어지는 경우가 많았다. 둘 이상이면 A, 하나면 B, 없으면 C.
    (06~09시는 더는 필수 조건이 아니라 등급에만 보너스로 반영 — presurge() 참고)"""
    from .sessions import ET
    t = datetime.fromisoformat(ts).astimezone(ET)
    why = []
    if 6 <= t.hour < 9:
        why.append("프리 06~09시")
    if tier_ == 3:
        why.append("★★★")
    if price and price < 5:
        why.append("$5 미만")
    if (vol_ratio or 0) >= 50:
        why.append("전날 거래량 50배+")
    return ("A" if len(why) >= 2 else "B" if why else "C"), why


def presurge(ts: str, tier_: int, price: float | None, vol_ratio: float | None, chg: float | None,
            session: str | None = None) -> tuple[bool, list[str]]:
    """'진짜 급등 전조' — 거래가 붙었다는 것만으로는 안 보내고, 정규장·애프터마켓이 아니고(정규장 터짐은
    +30% 먼저 올 확률이 8%로 훨씬 낮았음) 전날 대비 +5%↑ 이면서 ($5 미만 · 전날 거래량 50배+ · ★★★ 중 하나) 일 때만 보낸다.
    원래는 동부 06~09시 터짐만 쳤는데($2 미만 포함 조합 +30% 먼저 약 50%) — RENT 사례(그 시간대 밖에서 터져서
    급등 전조를 못 받음)를 계기로 '정규장만 아니면 됨'으로 풀었다. 새 조건의 적중률은 아직 재검증 전이라 지켜봐야 한다."""
    from .sessions import AFTER, REGULAR
    why = [w for ok, w in ((price and price < 5, "$5 미만"), ((vol_ratio or 0) >= 50, "전날 거래량 50배+"),
                           (tier_ == 3, "★★★")) if ok]
    ok = session not in (REGULAR, AFTER) and (chg or 0) >= 5 and bool(why)
    return ok, why


def _head(prefix: str, sym: str, name: str | None, suffix: str = "") -> str:
    """모든 알림의 첫 줄: '<이모지+제목> · <종목>(<이름>)<덧붙임>' 으로 통일한다 (messages.py 의 매매 알림과 같은 모양)."""
    from .trading.messages import sym_s
    return f"{prefix} · {sym_s(sym, name)}{suffix}"


def early20_text(sym: str, name: str | None, chg: float, price: float | None, session_ko: str) -> str:
    """정규장 열리기 전(프리·데이마켓)에 이미 +20% 찍음 — 거래 터짐보다 약한 신호라 알림도 짧게.
    big300 비교: 이 상태인 날이 그날 +300% 까지 간 비율 17.3% (정규장에서 처음 +20% 찍은 날은 2.6%) — 평소 대비 5.2배."""
    from .trading.messages import pct, px
    return "\n".join([
        _head("🌙 정규장 전 +20% 돌파", sym, name),
        f"💵 {px(price)} ({pct(chg)}) · {session_ko}",
        "오늘 +300%까지 갈 확률이 평소보다 5.2배 높은 상태 (연구: 17.3% vs 정규장 중 2.6%)",
    ])


def after_move_text(sym: str, name: str | None, chg: float, price: float | None) -> str:
    """애프터마켓 중 정규장 종가 대비 누적으로 많이 움직임 — 1분 급등(spike_up) 기준(+4%)엔 안 걸릴 만큼
    천천히 쌓인 경우가 많아서 따로 잡는다. early20 과 같은 성격의 '약한 신호' 쪽."""
    from .trading.messages import pct, px
    return "\n".join([
        _head("🌆 애프터마켓 누적 변동", sym, name),
        f"💵 {px(price)} ({pct(chg)}, 정규장 종가 대비) · 애프터마켓",
        "1분 급등 기준엔 안 걸릴 만큼 천천히 쌓인 움직임 — 참고용",
    ])


BOTTOM_BOUNCE_PCT = 15.0  # 며칠 전 급등 뒤 바닥(그 뒤 최저가) 대비 이만큼 오르면 — 아직 옛 고점 근처는 아닐 때


def bottom_bounce_text(sym: str, name: str | None, price: float, low: float, level: float, days: int,
                       run_chg: float, session_ko: str) -> str:
    """며칠 전 급등주가 그 뒤 깊게 빠졌다가(일봉 기준) 바닥 찍고 다시 오르기 시작 — 2차 파동 초입.
    고점 접근(approach)은 옛 고점 가까이 와야 뜨고, 재가속(reaccel)은 당일 눌림만 보니 그 사이 구간은 못 잡았었다."""
    from .trading.messages import pct, px
    return "\n".join([
        _head("🌊 바닥 찍고 재반등", sym, name, f" — {days}일 전 +{run_chg:.0f}% 급등주"),
        f"💵 {px(price)} ({(price / low - 1) * 100:+.0f}%, 바닥 대비) · {session_ko}",
        f"바닥 {px(low)} → 옛 고점 {px(level)}까지 아직 {(level / price - 1) * 100:+.0f}% 남음",
    ])


def signal_text(sym: str, name: str | None, lv: dict, session_ko: str, tags: list | None = None) -> str:
    """거래 터짐 신호 (세션 상관없이, 짧게). 급등 전조 조건까지 맞으면 alert_text 를 대신 보낸다."""
    from .trading.messages import money_s, pct, px
    m = lv["metrics"]
    return "\n".join([
        _head("📶 거래 터짐", sym, name),
        f"💵 {px(m.get('price'))} ({pct(m.get('chg'))}) · {session_ko}",
        f"3분 {money_s(m.get('dollar_3m'))} · 오늘 {money_s(m.get('dollar_today'))}",
        *([f"🏷 {' · '.join(tags)}"] if tags else []),
        "급등 전조 조건($5 미만/거래량 50배+/★★★ 중 하나 + 전날 대비 +5%↑)은 아님 — 참고용",
    ])


# 재가속: 급등 뒤 고점에서 15% 눌렸다가 저점에서 10% 다시 오른 순간.
# 보관 1분봉 (그날 +30% 찍은 종목·일 1.6만 건 중 1,619건): 53% 가 그날 앞 고점을 다시 뚫었고 그 뒤 평균 최고 +41%.
# 단, 그 자리에서 사면 −10% 먼저 54% · +20% 먼저 28% — 흔들림이 커서 타이트한 손절로는 못 먹는다.
REACCEL_DROP, REACCEL_BOUNCE = 15.0, 10.0
# 그날 안에서 재가속이 몇 번째냐 → +300% 까지 갈 확률 (big300 비교: +20%대 그친 날 6410건 vs +300% 간 날 219건, 기준 3.3%)
REACCEL_LIFT = {1: ("18%", "5.5배"), 2: ("30%", "9배"), 3: ("44%", "13배"), 4: ("64%", "20배"), 5: ("77%", "23배")}


def reaccel(bars: list[tuple], since: str, price: float | None = None,
            drop: float = REACCEL_DROP, bounce: float = REACCEL_BOUNCE) -> dict | None:
    """bars: (ts, o, h, l, c, v). since 이후 고점 → drop% 눌림 → 그 저점에서 bounce% 반등이면 그 값들."""
    hi, hi_ts, lo, lo_ts = 0.0, None, None, None
    for b in bars:
        if b[0] < since:
            continue
        if lo is None:
            if b[2] > hi:
                hi, hi_ts = b[2], b[0]
            if hi and b[3] <= hi * (1 - drop / 100):
                lo, lo_ts = b[3], b[0]
        elif b[3] < lo:
            lo, lo_ts = b[3], b[0]
    if lo is None:
        return None
    p = price if price is not None else bars[-1][4]
    if p < lo * (1 + bounce / 100):
        return None
    return {"high": hi, "high_ts": hi_ts, "low": lo, "low_ts": lo_ts, "price": p,
            "drop": (lo / hi - 1) * 100, "bounce": (p / lo - 1) * 100, "to_high": (hi / p - 1) * 100}


def reaccel_text(sym: str, name: str | None, r: dict, prev_close: float | None, session_ko: str, n: int,
                 dollar_2m: float | None = None, tags: list | None = None) -> str:
    from .trading.messages import money_s, pct, px
    chg = (r["price"] / prev_close - 1) * 100 if prev_close else None
    badge = "🔁" if n == 1 else "🔁🔁" if n == 2 else "🔥🔥🔥"
    lift = REACCEL_LIFT.get(min(n, 5))
    return "\n".join([
        _head(f"{badge} 재가속", sym, name, f" — 오늘 {n}번째"),
        f"💵 {px(r['price'])}" + (f" ({pct(chg)})" if chg is not None else "") + f" · {session_ko}",
        f"고점 {px(r['high'])} → 저점 {px(r['low'])} ({r['drop']:+.0f}%) → 지금 저점 대비 {r['bounce']:+.0f}%",
        f"앞 고점까지 {r['to_high']:+.0f}%" + (f" · 최근 2분 {money_s(dollar_2m)}" if dollar_2m else ""),
        *([f"🏷 {' · '.join(tags)}"] if tags else []),
        f"오늘 {n}번째 재가속 종목이 그날 +300% 까지 간 비율 {lift[0]} (평소 대비 {lift[1]}) · 흔들림 커서 손절 넓게",
    ])


def runner_tag(r: dict) -> str:
    """'🔙 6일 전 +120% 급등주'."""
    return f"🔙 {r['days']}일 전 +{r['run_chg']:.0f}% 급등주"


# 며칠 전(1~20거래일) +50% 급등주가 '급등 뒤 최고가' 에 다가올 때 — 넘기 전에 알린다.
# 보관 1분봉 3,523 종목·일: 고점 15% 아래 + 그 1분 거래량 3배 → 그날 돌파 51% (이후 최고 중앙 +18%),
# 10% 아래 + 3배 → 64% (+17%), 5% 아래 → 79% (+16%). 일찍 볼수록 먹을 폭은 크고 확률은 낮다.
# (단계, 고점 대비 선, 필요한 거래량 배수, 제목, 과거 결과)
APPROACH = ((1, 0.85, 3.0, "👀 이전 고점 접근", "고점 15% 아래 + 거래 3배: 그날 돌파 51% · 이후 최고 중앙 +18%"),
            (2, 0.95, 0.0, "🎯 이전 고점 임박", "고점 5% 아래: 그날 돌파 79% · 이후 최고 중앙 +16%"))


def approach_text(sym: str, name: str | None, r: dict, stage: int, price: float, prev_close: float | None, session_ko: str,
                  dollar_60s: float | None = None, tags: list | None = None) -> str:
    """며칠 전 급등주가 그 뒤 최고가(r['level']) 에 다가오는 중 — 1단계 15% 아래, 2단계 5% 아래."""
    from .trading.messages import money_s, pct, px
    _, _, _, title, past = APPROACH[stage - 1]
    chg = (price / prev_close - 1) * 100 if prev_close else None
    return "\n".join([
        _head(title, sym, name, f" — {r['days']}일 전 +{r['run_chg']:.0f}% 급등주"),
        f"💵 {px(price)}" + (f" ({pct(chg)})" if chg is not None else "") + f" · {session_ko}",
        f"넘어야 할 고점 {px(r['level'])} ({r['run_d'][5:]} 급등 뒤 최고) · 남은 거리 {(r['level'] / price - 1) * 100:+.1f}%",
        f"급등 뒤 최저 {px(r['low'])} ({(r['low'] / r['level'] - 1) * 100:+.0f}%)" + (f" · 최근 1분 {money_s(dollar_60s)}" if dollar_60s else ""),
        *([f"🏷 {' · '.join(tags)}"] if tags else []),
        f"과거: {past}",
    ])


def ask_depth(ob: dict, price: float, pct: float = 10.0) -> float | None:
    """호가창 매도 쪽: 현재가 ~ +pct% 안에 걸린 물량의 달러 합 (작을수록 얇다 = 조금만 사도 잘 오른다)."""
    asks = (ob or {}).get("asks") or []
    if not asks or not price:
        return None
    return sum(float(a["price"]) * float(a["volume"]) for a in asks if float(a["price"]) <= price * (1 + pct / 100))


def judge(m: dict, dm: dict) -> dict:
    """알림·대시보드용 한눈 판단. m: 장중 지표, dm: 일봉 지표."""
    chg = m.get("chg") or 0
    if chg >= 30:
        state, timing, verdict = "🔴 추격 주의", "과열", "급등이 이미 많이 진행됐습니다. 무리한 추격은 피하세요."
    elif chg >= 15:
        state, timing, verdict = "🟠 진행 중", "중반", "이미 움직이는 중입니다. 눌림(개미털기) 뒤 다시 오르는지 확인하세요."
    elif chg >= 5:
        state, timing, verdict = "🟡 움직이기 시작", "초반", "전조가 켜지고 막 오르기 시작했습니다. 거래량이 계속 붙는지 보세요."
    else:
        state, timing, verdict = "🟢 아직 조용", "초기", "전조 증상이 켜졌지만 가격은 아직 그대로입니다. 지켜볼 구간."
    vx = m.get("vol_x") or 0
    flow = "매우 강함" if vx >= 10 else "강함" if vx >= 5 else "보통" if vx >= 3 else "약함"
    rg = dm.get("range30") or 0
    risk = "높음" if rg >= 15 else "보통" if rg >= 8 else "낮음"
    return {"state": state, "timing": timing, "flow": flow, "risk": risk, "verdict": verdict}


def alert_text(sym: str, name: str | None, day: dict, lv: dict, session_ko: str, dashboard: str | None = None,
               now=None, shares: float | None = None, tags: list | None = None) -> str:
    """텔레그램 알림: 첫 줄 = 무슨 일·종목, 다음 = 한눈 판단, 마지막 = 근거 숫자."""
    from .trading.messages import money_s, pct, px
    m, dm = lv["metrics"], day["metrics"]
    j = judge(m, dm)
    on = set(day["signals"]) - ({"vol_up"} if "vol_up_strong" in day["signals"] else set())
    on_d = [lab for k, lab in DAILY_LABELS.items() if k in on]
    g = m.get("grade")
    turn = m.get("vol_today", 0) / shares * 100 if shares else None
    return "\n".join([
        _head("🚨 급등 전조", sym, name, f" [{g}등급]" if g else ""),
        f"💵 {px(m['price'])} ({pct(m['chg'])}) · {session_ko}",
        f"{j['state']} · 거래 {j['flow']}",
        f"1시간 {m.get('vol_x', 0):.1f}배 · 오늘 {money_s(m.get('dollar_today'))}",
        f"3분 {money_s(m.get('dollar_3m'))} · {m.get('vol_3m', 0):,.0f}주",
        *([f"회전율 오늘 {turn:{'.2f' if turn < 1 else '.1f'}}%"
           f" · 3분 {m.get('vol_3m', 0) / shares * 100:.2f}%"] if turn is not None and turn <= MAX_TURNOVER else []),
        *([f"호가 +10%안 {money_s(m['ask10'])}"
           + (f" · {'얇음 ✅' if m['ask10'] < (m.get('dollar_3m') or 0) else '두꺼움'}")]
          if m.get("ask10") is not None else []),
        f"📉 전조 {day['score']}점: " + " · ".join(on_d),
        f"60일 고점대비 {pct(dm.get('dd60'))} · 전날거래량 {times(dm.get('vol_ratio'))}",
        *([f"🏷 {' · '.join(tags)}"] if tags else []),
        f"💬 {j['verdict']}",
        f"변동성 {j['risk']} · 변동폭 {dm.get('range30') or 0:.0f}%",
    ])


def alert_buttons(sym: str, dashboard: str | None) -> list:
    row = [{"text": f"📋 {sym} 복사", "copy_text": {"text": sym}}]
    if dashboard:
        row.insert(0, {"text": "📊 대시보드에서 보기", "url": f"{dashboard}/?s={sym}"})
    return [row]


def ranked(db_path: str, limit: int = 50) -> dict:
    """최신 기준일의 전조 목록 (일봉 점수 + 장중 증상), 장중 증상 켜진 것 → 점수 순."""
    import json
    import sqlite3
    from pathlib import Path
    from .collector.store import ro_uri
    if not Path(db_path).exists():
        return {"d0": None, "rows": []}
    con = sqlite3.connect(ro_uri(db_path), uri=True)
    con.row_factory = sqlite3.Row
    try:
        d0 = con.execute("SELECT MAX(d0) FROM precursor_daily").fetchone()[0]
        if not d0:
            return {"d0": None, "rows": []}
        rows = [dict(r) for r in con.execute(
            """SELECT d.symbol, d.score, d.signals, d.metrics, d.close, l.signals AS live, l.metrics AS live_m, l.ts, l.alerted,
                      (SELECT name FROM universe u WHERE u.symbol=d.symbol ORDER BY snapshot_date DESC LIMIT 1) AS name
               FROM precursor_daily d LEFT JOIN precursor_live l ON l.d0=d.d0 AND l.symbol=d.symbol
               WHERE d.d0=? ORDER BY d.score DESC, d.symbol""", (d0,))]
        try:
            shares = dict(con.execute("SELECT symbol, shares FROM stock_info WHERE shares > 0").fetchall())
        except sqlite3.OperationalError:
            shares = {}
        try:
            since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            reaccel_n = dict(con.execute(
                "SELECT symbol, COUNT(*) FROM events WHERE kind='reaccel' AND ts>=? GROUP BY symbol", (since,)).fetchall())
            early20 = {r[0] for r in con.execute(
                "SELECT symbol FROM events WHERE kind='early20' AND ts>=?", (since,)).fetchall()}
        except sqlite3.OperationalError:
            reaccel_n, early20 = {}, set()
    except sqlite3.OperationalError:
        return {"d0": None, "rows": []}
    finally:
        con.close()
    for r in rows:
        r["signals"] = json.loads(r["signals"] or "[]")
        r["metrics"] = json.loads(r["metrics"] or "{}")
        r["live"] = json.loads(r["live"] or "[]")
        r["live_m"] = json.loads(r["live_m"] or "{}")
        on = set(r["signals"]) - ({"vol_up"} if "vol_up_strong" in r["signals"] else set())
        r["labels"] = [lab for k, lab in DAILY_LABELS.items() if k in on]
        r["live_labels"] = [LIVE_LABELS[k] for k in r["live"]]
        r["patterns"] = [PATTERN_LABELS[k] for k in PATTERN_LABELS if k in r["signals"]]
        r["tier"] = tier(r["metrics"], shares.get(r["symbol"]), r["close"])
        # 재가속 횟수로 등급 보정 (big300 비교: 2번째부터 +300% 비율이 평소의 9배, 4번째부터 20배)
        r["reaccel_n"] = reaccel_n.get(r["symbol"], 0)
        if r["reaccel_n"] >= 4:
            r["tier"] = max(r["tier"], 3)
        elif r["reaccel_n"] >= 2:
            r["tier"] = max(r["tier"], 2)
        elif r["reaccel_n"] >= 1:
            r["tier"] = max(r["tier"], 1)
        # 정규장 전(프리·다크마켓) +20% 돌파도 등급 보정 (big300 비교: 평소 대비 5.2배, +300% 종목의 77%가 여기 해당)
        r["early20"] = r["symbol"] in early20
        if r["early20"]:
            r["tier"] = max(r["tier"], 1)
    # '거래 급증'(wake)은 방향을 안 본다 — 폭락하면서도 거래량이 터지면 뜬다. 지금 내려가는 중인데
    # '거래 급증·매수세 유입'으로 맨 위에 뜨는 걸 막으려고, 지금 오르고 있을 때만(live_m.chg > 0) 그 우선순위를 준다.
    rows.sort(key=lambda r: (-(len(set(r["live"]) & {"wake", "creep"}) if (r["live_m"].get("chg") or 0) > 0 else 0),
                             -r["tier"], -r["score"], -(r["metrics"].get("vol_ratio") or 0)))
    total = len(rows)
    rows = rows[:limit]
    con = sqlite3.connect(ro_uri(db_path), uri=True)
    try:
        for r in rows:                             # 실시간 가격(수집기 1분 현재가)·세션, 발행주식·회전율, 공시 태그
            px = con.execute("SELECT last, session, ts FROM prices WHERE symbol=? ORDER BY ts DESC LIMIT 1", (r["symbol"],)).fetchone()
            if px:
                r["now"] = {"price": px[0], "session": px[1], "ts": px[2], "chg": (px[0] / r["close"] - 1) * 100 if r["close"] else None}
            try:
                info = con.execute("SELECT shares, list_date FROM stock_info WHERE symbol=?", (r["symbol"],)).fetchone()
            except sqlite3.OperationalError:
                info = None
            if info and info[0]:
                r["shares"] = info[0]
                r["turnover_d0"] = (r["metrics"].get("volume") or 0) / info[0] * 100
                if r["turnover_d0"] > MAX_TURNOVER:      # 발행주식 수가 액면병합 전 기준 등 → 믿을 수 없음
                    r["turnover_d0"] = None
                    r["shares_suspect"] = True
                if r["live_m"].get("vol_3m") is not None and not r.get("shares_suspect"):
                    r["turnover_3m"] = r["live_m"]["vol_3m"] / info[0] * 100
                    r["turnover_today"] = (r["live_m"].get("vol_today") or 0) / info[0] * 100
            try:
                tg = con.execute("SELECT tags, info FROM stock_tags WHERE symbol=? ORDER BY d0 DESC LIMIT 1", (r["symbol"],)).fetchone()
            except sqlite3.OperationalError:
                tg = None
            r["filing_tags"] = json.loads(tg[0]) if tg else []
            r["filing_info"] = json.loads(tg[1]) if tg and tg[1] else {}
        for r in rows[:80]:                        # 카드 미니 차트: 최근 30일 [o, h, l, c, v]
            bs = con.execute("SELECT open, high, low, close, volume FROM daily_bars WHERE symbol=? AND d<=? "
                             "ORDER BY d DESC LIMIT 30", (r["symbol"], d0)).fetchall()
            r["bars"] = [list(b) for b in reversed(bs)]
            if r["live_m"].get("price") is not None:
                r["judge"] = judge(r["live_m"], r["metrics"])
    except sqlite3.OperationalError:
        pass
    finally:
        con.close()
    return {"d0": d0, "rows": rows, "n": total}


def top_text(db_path: str, limit: int = 15, dashboard: str | None = None) -> str:
    """텔레그램 /전조."""
    from .trading.messages import pct, px
    r = ranked(db_path, limit)
    if not r["rows"]:
        return "급등 전조 목록이 아직 없습니다 (수집기가 장 마감 뒤 전 종목을 계산합니다)."
    L = [f"⚡ 급등 전조 목록 · {r['d0']} 종가 기준 (전체 {r['n']}종목 중 상위 {len(r['rows'])})", ""]
    for x in r["rows"]:
        live = f" · 🔥 {', '.join(x['live_labels'])}" if x["live_labels"] else ""
        chg = x["live_m"].get("chg")
        now = f" → 지금 {pct(chg)}" if chg is not None else ""
        L.append(f"{x['score']}점 {x['symbol']} {px(x['close'])}{now}{live}")
        L.append(f"    {', '.join(x['labels'])}")
    if dashboard:
        L += ["", f"📊 {dashboard}/#symbol"]
    return "\n".join(L)


def daily_list_text(db_path: str, n: int = 10, dashboard: str | None = None) -> str:
    """장 마감 뒤 '오늘 급등 전 감시 목록' 알림."""
    from .trading.messages import pct, px
    r = ranked(db_path, 400)
    picks = [x for x in r["rows"] if x.get("tier")]
    rows = sorted(r["rows"], key=lambda x: (-x.get("tier", 0), -x["score"], -(x["metrics"].get("vol_ratio") or 0)))[:min(max(n, len(picks)), 20)]          # 텔레그램 4096자 제한
    if not rows:
        return "급등 전 감시 목록이 비어 있습니다."
    L = [f"📋 오늘 급등 전 감시 목록 ({r['d0']} 종가 기준)",
         (f"🎯 급등 후보 {len(picks)}종목 (★★★ {sum(x['tier'] == 3 for x in picks)} · ★★ {sum(x['tier'] == 2 for x in picks)})"
          " — 많이 빠짐 + 전날 거래량 폭발 + 유통 주식 대비 많이 거래"
          if picks else f"전체 {r['n']}종목 중 점수 상위 {len(rows)}"),
         "장중에 거래가 터지면 따로 알림" + (f" · 나머지 {len(picks) - 20}종목은 대시보드" if len(picks) > 20 else ""),
         "──────────────"]
    for i, x in enumerate(rows, 1):
        m = x["metrics"]
        tags = (x.get("filing_tags") or [])[:2] + (x.get("patterns") or [])[:2]
        vol = [l for l in x["labels"] if l.startswith("거래량")]
        star = f" · {'★' * x['tier']}" if x.get("tier") else ""
        L.append(f"{i}. {x['symbol']} {x.get('name') or ''}{star} · {x['score']}점 · {px(x['close'])}")
        L.append(f"   고점 대비 {pct(m.get('dd60'))} · 전날 거래량 {times(m.get('vol_ratio'))}"
                 + (f" · 회전율 {x['turnover_d0']:{'.2f' if x['turnover_d0'] < 1 else '.1f'}}%" if x.get("turnover_d0") is not None else "")
                 + (f" · 최근 급등 {m['last_spike_d'][5:]}" if m.get("last_spike_d") else ""))
        if vol or tags:
            L.append("   🏷 " + " · ".join(vol + tags))
    if dashboard:
        L += ["", f"📊 {dashboard}/#home"]
    return "\n".join(L)


def until_s(start: str, end: str) -> str:
    """두 시각 사이 → '매수 35분 뒤' / '매수 2시간 10분 뒤'."""
    from datetime import datetime
    m = int((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() // 60)
    return f"매수 {m // 60}시간 {m % 60}분 뒤" if m >= 60 else f"매수 {max(m, 0)}분 뒤"


def vsignal_text(kind: str, sym: str, name: str | None, v: dict, cfg, session_ko: str) -> str:
    """가상 주문·매수·매도 알림 (실제 주문 없음). v["strat"] = vstrat.STRATEGIES 키."""
    from datetime import datetime
    from .trading.messages import pct, px
    from .vstrat import LIMIT_KO, STRATEGIES
    st = STRATEGIES.get(v.get("strat") or "", {})
    tag = f"[{st['name']}] " if st else ""
    if kind == "order":
        basis = LIMIT_KO.get(st.get("entry"), "지정가").format(dip=st.get("dip", ""))
        return "\n".join([
            _head(f"🧪 {tag}가상 지정가 주문".strip(), sym, name),
            f"🧾 {px(v['entry'])} 에 매수 대기 ({basis}) · {st.get('minutes', 60)}분 유효 · {session_ko} · 실제 주문 아님",
            f"근거: {v['reason']}",
            "한줄 판단: 정규장 전에 거래가 터졌습니다. 추격하지 않고 털릴 때 받습니다.",
        ])
    if kind == "buy":
        e, tp, sl = v["entry"], v.get("tp") or st.get("tp", 10), v.get("sl") or st.get("sl", 35)
        return "\n".join([
            _head(f"🧪 {tag}가상 매수 체결".strip(), sym, name),
            f"💵 {px(e)} · {session_ko} · 실제 주문 아님",
            f"🎯 익절 {px(e * (1 + tp / 100))} (+{tp:g}%) · 🛑 손절 {px(e * (1 - sl / 100))} (−{sl:g}%) · 애프터장 마감에 정리",
            f"근거: {v['reason']}",
            f"한줄 판단: {st.get('why', '')}" + (f" (과거 검증 {st['test']})" if st.get("test") else ""),
        ])
    ret = (v["exit_price"] / v["entry"] - 1) * 100
    held = datetime.fromisoformat(v["closed"]) - datetime.fromisoformat(v["opened"])
    h = held.total_seconds()
    held_s = f"{int(h // 86400)}일 {int(h % 86400 // 3600)}시간" if h >= 86400 else f"{int(h // 3600)}시간 {int(h % 3600 // 60)}분"
    icon = "🔵" if ret >= 0 else "🔴"
    peak = f" ({until_s(v['opened'], v['high_at'])})" if v.get("high_at") else ""
    return "\n".join([
        _head(f"{icon} {tag}가상 매도".strip(), sym, name, f" · {v['exit_reason']}"),
        f"💵 {px(v['entry'])} → {px(v['exit_price'])} ({pct(ret)}) · {session_ko}",
        f"보유 {held_s} · 보유 중 최고 {pct((v['high'] / v['entry'] - 1) * 100)}{peak} · 최저 {pct((v['low'] / v['entry'] - 1) * 100)}",
        "실제 주문 아님 — 전략 검증용 기록",
    ])
