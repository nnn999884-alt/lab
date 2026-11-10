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
TOP_MIN_DOLLAR_1M = 1e4       # 목록 맨 위 1~3번: 최근 1분 거래대금이 이 이상인 종목만 (2026-11-05)
TOP_RECENT_SEC = 60          # 목록 맨 위 1~3번 자격: 이 초 안에 체결이 있었던 종목 (2026-11-05: 9초 → 60초)
MAX_TURNOVER = 1000.0       # 하루 회전율이 이보다 크면 발행주식 수가 틀린 것으로 본다 (%)
ACTIVE_MIN_DOLLAR_3M = 1e5      # 최근 3분 거래대금이 이 이상이면 '지금 움직임' 후보로 남긴다
ACTIVE_MIN_VOL_X = 5.0          # 1시간 거래량이 평소의 이 배 이상
ACTIVE_MIN_TURN_3M = 0.1         # '지금 움직임' 탭: 최근 3분 회전율(%)이 이 이상
ACTIVE_MIN_DAY_VOL_RATIO = 50.0 # 전날 거래량 대비 이 배 이상
# '지금 움직임' 판정은 세션마다 따로 (2026-11-04: 정규장 기준을 프리에도 써서 프리 새벽에 한 종목도 안 나왔다).
# (3분 거래대금 최소 $, 3분 회전율 최소 %, 1시간 거래량 평소 대비 최소 배) — 거래대금은 꼭, 회전율·거래량 배수는 '둘 중 하나'.
# 이미 하루 종일 달리는 종목은 '평소'가 높아져 거래량 배수가 1 근처로 나오기 때문에(CRIS +96%: 3분 $38만인데 1.1배) 둘 다 요구하면 안 됨.
ACTIVE_BY_SESSION = {"DAY": (1e4, 0.05, 5.0), "PRE": (2e4, 0.1, 5.0), "REGULAR": (1e5, 0.1, 5.0), "AFTER": (2e4, 0.1, 5.0)}


def is_moving(live_m: dict, turnover_3m: float | None, session: str | None = None) -> bool:
    """'지금 움직임' 탭에 넣을 종목인가 (정지 종목은 호출하는 쪽에서 뺀다)."""
    d_min, t_min, x_min = ACTIVE_BY_SESSION.get(session or live_m.get("session") or "REGULAR", ACTIVE_BY_SESSION["REGULAR"])
    return ((live_m.get("vol_3m") or 0) > 0 and (live_m.get("dollar_3m") or 0) >= d_min
            and ((turnover_3m or 0) >= t_min or (live_m.get("vol_x") or 0) >= x_min))


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
    # 30분 고저폭 · 최근 5분 거래량 / 그 앞 20분 (2026-11-05 분봉 65만 시점 연구, momo_flags 참고)
    last30 = [b for b in minute_bars if ts(b) > now - timedelta(minutes=30)]
    if last30 and min(b[3] for b in last30) > 0:
        m["range30"] = (max(b[2] for b in last30) / min(b[3] for b in last30) - 1) * 100
    v5 = sum(b[5] for b in minute_bars if ts(b) > now - timedelta(minutes=5))
    v20 = [b[5] for b in minute_bars if now - timedelta(minutes=25) < ts(b) <= now - timedelta(minutes=5)]
    if len(v20) >= 10:
        base = sum(v20) / 20
        m["vol5_x"] = (v5 / 5) / base if base else (99.0 if v5 else 0.0)
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


# '지금 크게 움직이는 중' 표시 (2026-11-05, 분봉 176거래일 $5 이하 65만 시점: 다음 30분 안 +15% 도달률).
# 확률만 높여 줄 뿐 그 시점에 사서 이기는 규칙은 아님 (익절 10%/손절 5% 로 사면 모든 칸이 비용만큼 손해).
#   30분 고저폭 20%↑ : 정규장 13.5% (평소 0.6%, 23배) · 프리 8.9% (평소 3.4%) · 데이 0.9% (평소 0.2%)
#   최근 5분 거래량이 그 앞 20분의 8배↑ : 프리 10.4% (3배) · 정규장 1.6% (2.8배)
MOMO_RANGE30 = 20.0
MOMO_VOL5_X = 8.0


def momo_flags(m: dict, session: str | None = None) -> list[str]:
    out = []
    if (m.get("range30") or 0) >= MOMO_RANGE30:
        out.append(f"30분 변동폭 {m['range30']:.0f}%")
    if (session or m.get("session")) in ("PRE", "REGULAR") and (m.get("vol5_x") or 0) >= MOMO_VOL5_X:
        out.append(f"5분 거래량 {times(m['vol5_x'])}")
    return out


def alert_grade(ts: str, tier_: int, price: float | None, vol_ratio: float | None, turn3: float | None = None,
                shares: float | None = None, dollar_3m: float | None = None,
                momo: list[str] | None = None) -> tuple[str, list[str]]:
    """알림 등급 A/B/C (2026-11-03 기준 교체). 거래 터짐 12,064건 연구 결과:
    · A = 프리 동부 07:00~09:30 + 3분 회전율 1%↑ + 3분 거래대금 $10만↑ — 8,103개 조합 중 세 시기 모두 수익이 난 유일한 조합
      (135건, +30% 도달 53%, 익절 +30%/손절 −15% 순수익 +3.8%).
    · B = 3분 회전율 1%↑ 이거나, 프리 07~09:30 + 유통주식 1,000만 주 미만 (회전율 ≥3% 는 +30% 도달 23%, 소형주 18%).
    · C = 나머지.
    (옛 기준 '전날 거래량 50배·★★★' 은 변별력이 약해서 뺐다 — 급등 전조 조건 presurge() 에는 그대로 있음)
    tier_·price·vol_ratio 는 옛 호출 모양과 맞추려고 받기만 한다."""
    from .sessions import ET
    t = datetime.fromisoformat(ts).astimezone(ET)
    pre_win = 7 <= t.hour + t.minute / 60 < 9.5
    hot = (turn3 or 0) >= 1
    small = bool(shares) and shares < 1e7
    why = []
    if pre_win:
        why.append("프리 07~09:30")
    if hot:
        why.append(f"3분 회전율 {turn3:.1f}%")
    if small:
        why.append("유통 1,000만주 미만")
    if (dollar_3m or 0) >= 1e5:
        why.append("3분 $10만↑")
    why += momo or []
    if pre_win and hot and (dollar_3m or 0) >= 1e5:
        return "A", why
    if hot or (pre_win and small) or momo:
        return "B", why
    return "C", why


# '📶 거래 터짐' 알림을 보내는 최소 3분 회전율(%). 터짐 12,064건: 하루 75→24건, +30% 도달 9.4→17.1%
MIN_BURST_TURN3 = 0.5


def presurge(ts: str, tier_: int, price: float | None, vol_ratio: float | None, chg: float | None,
            session: str | None = None, turn3: float | None = None) -> tuple[bool, list[str]]:
    """'진짜 급등 전조' (2026-11-02 B안, 시간대별로 다르게).
    · 동부 06~09시 (프리): 원래 조건 — 전날 대비 +5%↑ 이면서 ($10 미만 · 전날 거래량 50배+ · ★★★ 중 하나).
      (원래 $2 였으나 11/02 사용자 요청으로 $10 까지)
      (10/24 재학습, 첫 1분 터짐 476건: +30% 가 −20% 보다 먼저 온 비율 약 50% vs 전체 26%)
    · 그 밖의 시간(데이·그 외 프리·정규·애프터): 위 조건에 더해 3분 회전율 1%↑ 도 채워야 한다
      (11/02 터짐 12,064건 연구: 회전율 ≥3% 는 +30% 도달 23%, <0.1% 는 3%. 데이마켓은 1.5%뿐이라 더 엄격하게).
    못 채운 터짐은 '📶 거래 터짐' 으로 간다. price 는 전날 종가."""
    from .sessions import ET
    t = datetime.fromisoformat(ts).astimezone(ET)
    why = [w for ok, w in ((price and price < 10, "$10 미만"), ((vol_ratio or 0) >= 50, "전날 거래량 50배+"),
                           (tier_ == 3, "★★★")) if ok]
    if 6 <= t.hour < 9:
        return (chg or 0) >= 5 and bool(why), (["프리 06~09시"] + why) if why else why
    ok = (chg or 0) >= 5 and bool(why) and (turn3 or 0) >= 1
    return ok, (why + [f"3분 회전율 {turn3:.1f}%"]) if ok else why


def _head(prefix: str, sym: str, name: str | None, suffix: str = "") -> str:
    """모든 알림의 첫 줄: '<이모지+제목> · <종목>(<이름>)<덧붙임>' 으로 통일한다 (messages.py 의 매매 알림과 같은 모양)."""
    from .trading.messages import sym_s
    return f"{prefix} · {sym_s(sym, name)}{suffix}"


def early20_text(sym: str, name: str | None, chg: float, price: float | None, session_ko: str,
                 turn10: float | None = None) -> str:
    """정규장 열리기 전(프리·데이마켓)에 이미 +20% 찍음 — 거래 터짐보다 약한 신호라 알림도 짧게.
    big300 비교: 이 상태인 날이 그날 +300% 까지 간 비율 17.3% (정규장에서 처음 +20% 찍은 날은 2.6%) — 평소 대비 5.2배.
    2026-11-05: 직전 10분 회전율 0.5%↑ 만 보낸다 — 그 뒤 +30% 더 간 비율 27% (10~11월), 돌파 때 사면 정규장 종가 평균 −17%."""
    from .trading.messages import pct, px
    return "\n".join([
        _head("🌙 정규장 전 +20% 돌파", sym, name),
        f"💵 {px(price)} ({pct(chg)}) · {session_ko}" + (f" · 직전 10분 회전율 {turn10:.1f}%" if turn10 is not None else ""),
        "이런 경우 4건 중 1건이 여기서 +30% 더 감 — 돌파 순간 추격은 평균 손해",
    ])


# 🔥 데이·프리 점화 (2026-11-08): 그날 저점 뒤 처음 종가가 저점 +10% 인 1분봉 = '점화'.
# 9/1~11/6 점화 1.7만 건 중 그날 그 뒤 고가 +50%↑ 더 간 비율 1.2%. 데이·프리 + $2 미만 + 점화 봉 몸통 +2.8%↑
# + 점화까지 누적 대금 $1만↑ → 225건(하루 4.6건) 10.2% (10/19까지 9.9% · 10/20부터 10.4%).
# 같은 종목을 점화 때 사서 고점 대비 −20% 에 팔면 비용 빼기 전부터 손해 → 알림만, 매매 연결 없음.
IGN_UP_PCT = 10.0
IGN_BODY_PCT = 2.8
IGN_MAX_PX = 2.0
IGN_MIN_DOLLAR = 1e4
IGN_SESSIONS = ("DAY", "PRE")


def day_ignition(bars: list[tuple]) -> dict | None:
    """그날 첫 봉부터의 1분봉 (ts, o, h, l, c, v) → 첫 점화 {k, low, price, body, cum, pre60} 또는 None."""
    lo, li, cum = float("inf"), -1, 0.0
    for i, b in enumerate(bars):
        cum += b[4] * b[5]
        if b[3] < lo:
            lo, li = b[3], i
        if 0 < lo and i > li and b[4] >= lo * (1 + IGN_UP_PCT / 100):
            return {"k": i, "low": lo, "price": b[4], "body": (b[4] / b[1] - 1) * 100 if b[1] else 0.0,
                    "cum": cum, "pre60": sum(x[4] * x[5] for x in bars[max(0, i - 60):i])}
    return None


def ignition_ok(ig: dict | None, session: str) -> bool:
    return bool(ig and session in IGN_SESSIONS and ig["price"] < IGN_MAX_PX
                and ig["body"] >= IGN_BODY_PCT and ig["cum"] >= IGN_MIN_DOLLAR)


def ignition_text(sym: str, name: str | None, ig: dict, pc: float | None, session_ko: str,
                  shares: float | None = None) -> str:
    from .trading.messages import money_s, pct, px
    chg = f" (전날 대비 {pct((ig['price'] / pc - 1) * 100)})" if pc else ""
    return "\n".join([
        _head("🔥 데이·프리 점화", sym, name, f" · {session_ko}"),
        f"💵 {px(ig['price'])}{chg} · 오늘 저점 {px(ig['low'])}에서 +{(ig['price'] / ig['low'] - 1) * 100:.0f}%"
        f" · 1분봉 +{ig['body']:.1f}%",
        f"점화 전 60분 대금 {money_s(ig['pre60'])} · 오늘 누적 {money_s(ig['cum'])}"
        + (f" · 주식 {shares / 1e6:.1f}M" if shares else ""),
        "이런 점화 10건 중 1건이 그날 +50% 이상 더 감 (검증 10%, 평소 1.2%) — 9건은 꺼짐. 매수 신호 아님",
    ])


EARLY_UP_PCT = 3.0        # 초기 경보: 10분 전 시가 대비 이만큼 오른 1분봉이 그날 처음
EARLY_TURN_PCT = 0.05     # + 그 1분 회전율(거래량/발행주식) 이만큼 + 양봉
EARLY_MIN_SCORE = 9       # 프리장은 전날 일봉 점수가 이 이상인 종목만, 아니면 1시간 안 공시·뉴스가 있어야


def early_watch(bars: list[tuple], shares: float | None) -> tuple[int, float, float] | None:
    """초기 경보 조건이 '그날 처음' 맞은 봉 → (인덱스, 10분 상승 %, 1분 회전율 %). bars = (ts, o, h, l, c, v).
    검증(2026-11-05, 10/23~11/04): 프리장 경보 중 점수 9↑ 은 120분 안 +20% 도달 28% (전체 19%),
    경보 전 1시간 안 공시·뉴스 있으면 32% (없으면 10%). 매수 규칙은 아님 — 전 종목에 적용하면 6~9월·10~11월 모두 손해."""
    if not shares:
        return None
    for i in range(10, len(bars)):
        base = bars[i - 10][1]
        if base <= 0:
            continue
        up = (bars[i][4] / base - 1) * 100
        turn = bars[i][5] / shares * 100
        if up >= EARLY_UP_PCT and turn >= EARLY_TURN_PCT and bars[i][4] > bars[i][1]:
            return i, up, turn
    return None


def early_watch_text(sym: str, name: str | None, up: float, turn: float, price: float | None,
                     session_ko: str, why: str) -> str:
    from .trading.messages import px
    return "\n".join([
        _head("👀 초기 경보 (사지 않음)", sym, name),
        f"💵 {px(price)} · 10분 +{up:.1f}% · 1분 회전율 {turn:.2f}% · {session_ko}",
        f"이유: {why}",
        "아직 초입 — 이런 경보 중 2시간 안 +20% 간 건 3~4건 중 1건 (10일치 검증, 표본 적음)",
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


def steady_text(sym: str, name: str | None, price: float | None, chg: float | None, n: int, total: int,
                 window_hours: float, session_ko: str) -> str:
    """한 번에 크게 안 터지고 거래대금 랭킹에 꾸준히 올라와 있는 종목('사부작사부작') — 급등 전조·거래 터짐 같은
    단발성 신호와는 다른 성격이라 따로 알린다."""
    from .trading.messages import pct, px
    return "\n".join([
        _head("🫧 꾸준히 거래 붙음", sym, name),
        f"💵 {px(price)}" + (f" ({pct(chg)})" if chg is not None else "") + f" · {session_ko}",
        f"📊 최근 {window_hours:.0f}시간 중 {n}/{total}회 거래대금 상위",
    ])


def signal_text(sym: str, name: str | None, lv: dict, session_ko: str, tags: list | None = None) -> str:
    """거래 터짐 신호 (세션 상관없이, 짧게). 급등 전조 조건까지 맞으면 alert_text 를 대신 보낸다."""
    from .trading.messages import money_s, pct, px
    m = lv["metrics"]
    return "\n".join(x for x in [
        _head("📶 거래 터짐", sym, name, f" · {m['grade']}등급" if m.get("grade") else ""),
        rise_line(m.get("chg")),
        f"💵 {px(m.get('price'))} ({pct(m.get('chg'))}) · {session_ko}",
        f"📊 3분 {money_s(m.get('dollar_3m'))} · 오늘 {money_s(m.get('dollar_today'))}",
        f"🏷 {' · '.join(tags[:4])}" if tags else "",
        "급등 전조 조건(전날 대비 +5%↑ + $10 미만/거래량 50배+/★★★ 중 하나, 프리 06~09시 밖은 3분 회전율 1%↑ 추가)은 아님 — 참고용",
    ] if x)


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
    return "\n".join(x for x in [
        _head(f"{badge} 재가속", sym, name, f" · 오늘 {n}번째"),
        f"💵 {px(r['price'])}" + (f" ({pct(chg)})" if chg is not None else "") + f" · {session_ko}",
        f"📊 고점 {px(r['high'])} → 저점 {px(r['low'])} ({r['drop']:+.0f}%) → 반등 {r['bounce']:+.0f}% · 앞 고점까지 {r['to_high']:+.0f}%",
        f"🏷 {' · '.join(tags[:4])}" if tags else "",
        f"💬 흔들림 큼 — 손절 넓게 (오늘 {n}번째, +300% 도달 {lift[0]})",
    ] if x)


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


def rise_line(chg: float | None) -> str:
    """알림 둘째 줄: 이미 얼마나 오른 상태인지 (알림 260건: 알림 때 +10% 이하 3시간 뒤 평균 +8.8%,
    +20~40% +1.2%, +40% 이상 −10.6% — 이미 많이 오른 뒤 따라 사면 손해라 크게 알린다)."""
    if chg is None:
        return ""
    if chg >= 20:
        return f"🔴 전날 대비 이미 +{chg:.0f}% 오른 상태 — 추격 금지"
    if chg >= 10:
        return f"🟡 전날 대비 이미 +{chg:.0f}% 오름 — 늦었을 수 있음"
    return f"🟢 초입 (전날 대비 +{max(chg, 0):.0f}%)"


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
    turn3 = m.get("vol_3m", 0) / shares * 100 if shares else None
    risk_tags = [t for t in (tags or []) if any(k in t for k in ("공모", "액면병합", "전환사채", "상폐", "희석", "증자", "워런트"))]
    good_tags = [t for t in (tags or []) if t not in risk_tags]
    quote_bits = []
    if turn is not None and turn <= MAX_TURNOVER:
        quote_bits.append(f"회전 {turn:.2f}%" if turn < 1 else f"회전 {turn:.1f}%")
        if turn3 is not None:
            quote_bits[-1] += f" (3분 {turn3:.0f}%)"
    if m.get("ask10") is not None:
        thin = m["ask10"] < (m.get("dollar_3m") or 0)
        quote_bits.append(f"호가 +10%안 {money_s(m['ask10'])} ({'얇음 ✅' if thin else '두꺼움'})")
    tip = {"🔴 추격 주의": "과열 — 추격 금지", "🟠 진행 중": "눌림 뒤 재상승 확인",
           "🟡 움직이기 시작": "거래량 지속 확인", "🟢 아직 조용": "지켜보기"}.get(j["state"], "")
    lines = [
        _head("🚨 급등 전조", sym, name, f" · {g}등급" if g else ""),
        rise_line(m.get("chg")),
        f"💵 {px(m['price'])} ({pct(m['chg'])}) · {session_ko}",
        f"{j['state']} · 거래 {j['flow']}",
        "",
        f"📊 1시간 {m.get('vol_x', 0):.1f}배 · 오늘 {money_s(m.get('dollar_today'))} · 3분 {money_s(m.get('dollar_3m'))}",
    ]
    if quote_bits:
        lines.append(" · ".join(quote_bits))
    lines.append(f"위치 60일 고점 {pct(dm.get('dd60'))} · 전날 거래량 {times(dm.get('vol_ratio'))}"
                 f" · 변동폭 {dm.get('range30') or 0:.0f}%")
    lines.append(f"📉 전조 {day['score']}점" + (f" — {' · '.join(on_d[:5])}" if on_d else ""))
    if risk_tags:
        lines += ["", "⚠️ " + " · ".join(risk_tags[:4])]
    if good_tags:
        lines.append("🏷 " + " · ".join(good_tags[:4]))
    if tip:
        lines += ["", f"💬 {tip}"]
    return "\n".join(lines)


def alert_buttons(sym: str, dashboard: str | None) -> list:
    row = [{"text": f"📋 {sym} 복사", "copy_text": {"text": sym}}]
    if dashboard:
        row.insert(0, {"text": "📊 대시보드에서 보기", "url": f"{dashboard}/?s={sym}"})
    return [row]


# 급등 A/B 가 한 번 붙은 종목은 그날 하루(거래일: 동부 20:00 데이마켓 시작 ~ 다음 날 20:00) 칩을 붙여 둔다 (자리는 고정 안 함)


def _pins_path(db_path: str):
    from pathlib import Path
    return Path(db_path).with_name("hot_pins.json")


def _trade_day(ts: datetime) -> str:
    from .sessions import ET
    return (ts.astimezone(ET) + timedelta(hours=4)).date().isoformat()      # 동부 20:00 → 다음 날 0시로 본다


def _load_pins(db_path: str) -> dict:
    """{종목: {"grade","ts","why"}} — 오늘(거래일) 급등 A/B 가 붙었던 종목."""
    import json
    try:
        raw = json.loads(_pins_path(db_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    today = _trade_day(datetime.now(timezone.utc))
    out = {}
    for sym, v in (raw or {}).items():
        try:
            if _trade_day(datetime.fromisoformat(v["ts"])) == today and v.get("grade") in ("A", "B"):
                out[sym] = v
        except (KeyError, ValueError, TypeError):
            continue
    return out


def _save_pins(db_path: str, pins: dict) -> None:
    import json
    import os
    p = _pins_path(db_path)
    try:
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(pins, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


def _halted_now(con) -> set[str]:
    """지금 거래정지 중인 종목 (LULD 등: 정지 시각 ≤ 지금 < 재개 시각, 재개 시각이 없으면 정지 후 20분까지)."""
    now = datetime.now(timezone.utc)
    out = set()
    try:
        rows = con.execute("SELECT symbol, halt_ts, resume_ts FROM halts WHERE halt_ts >= ?",
                           ((now - timedelta(days=1)).strftime("%Y-%m-%d"),)).fetchall()
    except Exception:
        return out
    for sym, h, rs in rows:
        try:
            t0 = datetime.fromisoformat(h)
            t1 = datetime.fromisoformat(rs) if rs else t0 + timedelta(minutes=20)
            if t0 <= now < t1:
                out.add(sym)
        except (TypeError, ValueError):
            continue
    return out


PROB_SHOW = 10.0   # 후보 탭: 움직임이 없어도 순위 모델 확률이 이 % 이상이면 보여 줌


def ranked(db_path: str, limit: int = 50, only_active: bool = True) -> dict:
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
            """SELECT d.symbol, d.score, d.signals, d.metrics, d.close, d.created,
                      l.signals AS live, l.metrics AS live_m, l.ts, l.alerted,
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
            steady = {r[0] for r in con.execute(
                "SELECT symbol FROM events WHERE kind='steady' AND ts>=?", (since,)).fetchall()}
        except sqlite3.OperationalError:
            reaccel_n, early20, steady = {}, set(), set()
        # 지금 가격: 웹소켓 체결(live_tick, 구독 종목) · 전 종목 3초 훑기(price_now) 중 최근 것 (60초 안)
        # (11/09: 화면 등락률이 1분 감시 계산값이라 3초마다 새로 받아도 그대로였음)
        nowpx: dict = {}
        cut = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        for tbl in ("price_now", "live_tick"):
            try:
                for s_, ts_, px_ in con.execute(f"SELECT symbol, ts, last FROM {tbl} WHERE ts >= ? AND last > 0", (cut,)):
                    if s_ not in nowpx or ts_ > nowpx[s_][0]:
                        nowpx[s_] = (ts_, px_)
            except sqlite3.OperationalError:
                pass
    except sqlite3.OperationalError:
        return {"d0": None, "rows": []}
    finally:
        con.close()
    for r in rows:
        r["signals"] = json.loads(r["signals"] or "[]")
        r["metrics"] = json.loads(r["metrics"] or "{}")
        r["live"] = json.loads(r["live"] or "[]")
        r["live_m"] = json.loads(r["live_m"] or "{}")
        np_ = nowpx.get(r["symbol"])
        if np_ and r.get("close"):
            r["live_m"]["price"] = np_[1]
            r["live_m"]["chg"] = (np_[1] / r["close"] - 1) * 100
            r["live_m"]["px_ts"] = np_[0]
        try:
            created = datetime.fromisoformat(r.get("created") or "")
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            r["new"] = (datetime.now(timezone.utc) - created).total_seconds() <= 1800
        except (TypeError, ValueError):
            r["new"] = False
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
        # '꾸준히 거래 붙음'(2시간 거의 매번 거래대금 상위)도 등급 보정. 알림 대신 데이터로: 다음 날 장중 +30% 도달 14.5%
        # (55종목·일, 후보 전체 기준 2.2% → 6.6배, 2026-11-02 events 'steady' 와 일봉으로 확인; 표본 작음)
        r["steady"] = r["symbol"] in steady
        if r["steady"]:
            r["tier"] = max(r["tier"], 2)
            r["patterns"] = r["patterns"] + ["꾸준히 거래"]
    try:                                           # 순위 모델 확률 (그날 고가 +30%, 없으면 None)
        from .rankmodel import probs, live_probs, background
        from .alertperf import trade_day
        td = trade_day(datetime.now(timezone.utc))
        # 모델 계산(3~10초)은 뒤에서 — 화면 요청은 기다리지 않고 지금 있는 값으로 바로 (처음 몇 초만 빈칸)
        pr = background("probs", (db_path, d0), lambda: probs(db_path, d0), 300)
        prn = background("live", (db_path, d0, td), lambda: live_probs(db_path, d0, td), 60)   # 지금가 +20% 더 확률
    except Exception:
        pr, prn = {}, {}
    for r in rows:
        r["prob"] = pr.get(r["symbol"])
        r["prob_n"] = prn.get(r["symbol"])
    pins = _load_pins(db_path)
    if only_active:
        rows = [r for r in rows if r.get("new") or r["live"] or r["symbol"] in pins or (r["prob"] or 0) >= PROB_SHOW
                or (r["prob_n"] or 0) >= PROB_SHOW
                or (r["live_m"].get("dollar_3m") or 0) >= ACTIVE_MIN_DOLLAR_3M
                or (r["live_m"].get("vol_x") or 0) >= ACTIVE_MIN_VOL_X
                or (r["metrics"].get("vol_ratio") or 0) >= ACTIVE_MIN_DAY_VOL_RATIO]
    # 정렬(추천순) = 최근 1분 회전율 큰 순 (웹소켓 체결 60초 합 live_tick.vol_60s, 실시간 구독 아닌 종목은 3분 1분봉 평균).
    # 동점은 3초 회전율 → 1분 거래대금. 등락률은 보지 않는다. (2026-11-05 변경, 그 전엔 3초·3분 거래대금·거래량·회전율 순위 합)
    def _ranks(vals: list[float]) -> list[int]:
        return [1 + sum(1 for w in vals if w > v) for v in vals]       # 동점은 같은 순위

    live_now: set[str] = set()                     # 최근 TOP_RECENT_SEC 초 안에 체결이 있었던 종목

    w60: dict[str, tuple[float, float]] = {}       # 종목 → (최근 60초 체결 수량, 대금) — 웹소켓 체결 기준

    def one_min(r) -> tuple[float, float]:
        """최근 1분 (수량, 대금): 실시간 체결 60초 값, 실시간 구독이 아닌 종목은 최근 3분 1분봉 평균."""
        if r["symbol"] in w60:
            return w60[r["symbol"]]
        lm = r["live_m"]
        return (lm.get("vol_3m") or 0.0) / 3, (lm.get("dollar_3m") or 0.0) / 3

    def _flow_ranks() -> None:
        fresh = {}
        try:
            c3 = sqlite3.connect(ro_uri(db_path), uri=True)
            try:
                cut = (datetime.now(timezone.utc) - timedelta(seconds=9)).isoformat()
                fresh = {a: (b or 0.0, c or 0.0) for a, b, c in c3.execute(
                    "SELECT symbol, vol_3s, amt_3s FROM live_tick WHERE ts >= ?", (cut,))}
                try:
                    cut60 = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
                    w60.update({a: (b or 0.0, c or 0.0) for a, b, c in c3.execute(
                        "SELECT symbol, vol_60s, amt_60s FROM live_tick WHERE ts >= ? AND vol_60s IS NOT NULL", (cut60,))})
                except sqlite3.OperationalError:
                    pass                           # 수집기가 아직 60초 열을 안 만든 경우
                # 맨 위 1~3번 자격: 최근 TOP_RECENT_SEC 초 안에 체결 (live_tick.ts = 마지막 체결 시각). 9초는 너무 짧아
                # 방금 몇 초 쉰 활발한 종목이 빠졌다 (2026-11-05 사용자 지적)
                cut2 = (datetime.now(timezone.utc) - timedelta(seconds=TOP_RECENT_SEC)).isoformat()
                live_now.update(a for (a,) in c3.execute("SELECT symbol FROM live_tick WHERE ts >= ?", (cut2,)))
            finally:
                c3.close()
        except sqlite3.OperationalError:
            pass                                   # 수집기가 아직 새 열을 안 만든 경우: 3분 값만 쓴다

        live_now.update(a for a, (b, c_) in fresh.items() if b > 0 or c_ > 0)

        def sh_of(r):
            sh = shares.get(r["symbol"])
            if sh and (r["metrics"].get("volume") or 0) / sh * 100 > MAX_TURNOVER:
                return None                        # 발행주식 수를 믿을 수 없음(액면병합 전 기준 등)
            return sh

        def per(r, kind):
            sh = sh_of(r)
            v3, a3 = fresh.get(r["symbol"], (0.0, 0.0))
            v60, a60 = one_min(r)
            return {"s_amt": a3, "s_turn": v3 / sh * 100 if sh else 0.0,
                    "m_amt": a60, "m_turn": v60 / sh * 100 if sh else 0.0}[kind]
        # 2026-11-05: 1분(60초) 회전율(발행주식 대비) 순 (분봉 10~11월: 맨 위 3개 30분 안 +15% 도달 1분 회전율 12.0%,
        # 거래대금+회전율 11.0%, 거래대금만 8.7% — 기간 1·2·3·5분은 거의 같아 가장 빠른 1분). 발행주식을 모르면 거래대금 순으로 그 뒤.
        # flow_rank_3s(3초 회전율)는 동점 가르기에만 쓴다.
        for pre in ("s", "m"):
            tr = _ranks([per(r, f"{pre}_turn") for r in rows])
            am = _ranks([per(r, f"{pre}_amt") for r in rows])
            known = [bool(sh_of(r)) for r in rows]
            for r, t, a, k in zip(rows, tr, am, known):
                r["flow_rank_3s" if pre == "s" else "flow_rank"] = t if k else len(rows) + a
        for r in rows:
            v60, a60 = one_min(r)
            sh = sh_of(r)
            r["dollar_1m"] = a60
            r["turnover_1m"] = v60 / sh * 100 if sh else None

    _flow_ranks()

    def _key(r: dict) -> tuple:
        return (r["flow_rank"], r["flow_rank_3s"], -(r.get("dollar_1m") or 0), -r["tier"], -r["score"],
                -(r["metrics"].get("vol_ratio") or 0))

    rows.sort(key=_key)
    total = len(rows)
    rows = rows[:limit]
    con = sqlite3.connect(ro_uri(db_path), uri=True)
    try:
        for r in rows:                             # 실시간 가격(수집기 1분 현재가)·세션, 발행주식·회전율, 공시 태그
            px = con.execute("SELECT last, session, ts FROM prices WHERE symbol=? ORDER BY ts DESC LIMIT 1", (r["symbol"],)).fetchone()
            try:
                live = con.execute("SELECT last, ts FROM live_tick WHERE symbol=?", (r["symbol"],)).fetchone()
            except sqlite3.OperationalError:
                live = None
            np_ = nowpx.get(r["symbol"])                 # 웹소켓 체결·전 종목 3초 훑기 중 최신 (60초 안)
            if np_ and (not px or np_[0] >= (px[2] or "")):
                r["now"] = {"price": np_[1], "session": px[1] if px else None, "ts": np_[0],
                            "chg": (np_[1] / r["close"] - 1) * 100 if r["close"] else None}
            elif live and (not px or (live[1] or "") >= (px[2] or "")):
                r["now"] = {"price": live[0], "session": px[1] if px else None, "ts": live[1],
                            "chg": (live[0] / r["close"] - 1) * 100 if r["close"] else None}
            elif px:
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
        # 지금 급등 전조 조건을 채운 종목(hot)과 그 등급(A/B/C), 3초 거래 순위 1~3위(flow_top)를 표시 — 대시보드에서 눈에 띄게 한다.
        now_iso = datetime.now(timezone.utc).isoformat()
        pins_dirty = False
        # 맨 위 1~3번 = 지금 체결이 있는 종목(최근 TOP_RECENT_SEC 초) 중 3분 회전율이 가장 큰 3개 (2026-11-05 변경).
        # 체결 있는 종목이 3개 미만이면 모자란 만큼은 아래 흐름 순위 순서대로 채운다.
        halted = _halted_now(con)
        # 2026-11-05: 최근 1분(60초) 회전율 큰 순, 1분 거래대금 TOP_MIN_DOLLAR_1M 미만은 제외 (몇 천 달러짜리 초소형주가 튀지 않게).
        # 발행주식을 모르는 종목은 회전율 -1 로 뒤.
        by_amt = sorted((r for r in rows if r["symbol"] in live_now and r["symbol"] not in halted
                         and (r.get("dollar_1m") or 0) >= TOP_MIN_DOLLAR_1M),
                        key=lambda r: (-(r["turnover_1m"] if r.get("turnover_1m") is not None and not r.get("shares_suspect") else -1),
                                       -(r.get("dollar_1m") or 0)))[:3]
        top_syms = [r["symbol"] for r in by_amt] + [r["symbol"] for r in rows if r["symbol"] not in live_now
                                                    and r["symbol"] not in halted][:3 - len(by_amt)]
        top_set = set(top_syms)
        for i, r in enumerate(rows):
            r["flow_top"] = r["symbol"] in top_set
            m_ = r["live_m"]                       # '지금 움직임' 탭: 하루 단위 신호(live)는 거의 전부 켜져 있어서 따로 본다
            r["halted"] = r["symbol"] in halted
            r["moving"] = r["symbol"] not in halted and is_moving(m_, r.get("turnover_3m"))
            lm = r["live_m"]
            if lm.get("price") is None:
                continue
            ok, why = presurge(now_iso, r.get("tier") or 0, r.get("close"), (r.get("metrics") or {}).get("vol_ratio"),
                               lm.get("chg"), None, r.get("turnover_3m"))
            r["hot"], r["hot_why"] = ok, why
            r["grade"] = alert_grade(now_iso, r.get("tier") or 0, None, None, r.get("turnover_3m"), r.get("shares"),
                                     lm.get("dollar_3m"))[0]
            if r["hot"] and r["grade"] in ("A", "B"):          # 급등 A/B 가 붙은 종목은 기록해 둔다
                old = pins.get(r["symbol"])
                if not old or old["grade"] != "A" or r["grade"] == "A":
                    pins[r["symbol"]] = {"grade": r["grade"] if not old or r["grade"] == "A" else old["grade"],
                                         "ts": old["ts"] if old else now_iso, "why": r["hot_why"]}
                    pins_dirty = True
        # 맨 위 1~3번 = 지금 거래 흐름 1~3위 (계속 고정), 그 아래 = 급등 조건 충족 종목, 나머지는 흐름 순 (안정 정렬)
        for r in rows:                             # 조건이 풀렸어도 오늘 A/B 가 붙었던 종목은 칩을 유지한다 (자리는 일반 순위)
            pin = pins.get(r["symbol"])
            if pin and not r.get("hot"):
                r["hot"], r["grade"], r["pinned"] = True, pin["grade"], True
                r["hot_why"] = ["조건 충족 후 유지"] + list(pin.get("why") or [])
        if pins_dirty:
            _save_pins(db_path, pins)
        # 맨 위 1~3번 → 지금 급등 조건을 채운 종목 → 나머지(흐름 순). 예전에 A/B 가 붙었던 종목(pinned)은 칩만 남고
        # 자리는 일반 순위를 따라 움직인다 (자리를 고정하지 않음).
        rows.sort(key=lambda r: (0, top_syms.index(r["symbol"])) if r.get("flow_top") else
                  (1, 0) if r.get("hot") and not r.get("pinned") else (2, 0))
        from .condscan import _epoch_min, trend_of
        for r in rows:                             # 추세(5분봉 MA5/MA20): 올라가는 중인지 내려가는 중인지
            raw = con.execute("SELECT ts, open, high, low, close, volume FROM bars_1m WHERE symbol=? ORDER BY ts DESC LIMIT 300",
                              (r["symbol"],)).fetchall()
            r["trend"] = trend_of([(_epoch_min(x[0]), *x[1:]) for x in reversed(raw)]) if raw else None
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
    r = ranked(db_path, 400, only_active=False)
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
            f"🎯 익절 {px(e * (1 + tp / 100))} (+{tp:g}%) · 🛑 손절 {px(e * (1 - sl / 100))} (−{sl:g}%) · "
            + ("정규장 마감에 정리" if st.get("close") == "regular" else "애프터장 마감에 정리"),
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
