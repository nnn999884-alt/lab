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
from datetime import datetime, timedelta

# 과거 +500% 사례에서 비교군보다 훨씬 흔했던 증상일수록 무겁게 (최대 10점)
WEIGHT = {"crashed": 2, "spiked": 2, "vol_up_strong": 2, "vol_up": 1, "volatile": 1, "bleed": 1, "cheap": 1}
DAILY_LABELS = {
    "vol_up_strong": "거래량 폭발", "vol_up": "거래량 상승", "crashed": "낙폭과대", "spiked": "급등 이력",
    "volatile": "고변동", "bleed": "거래 마름", "cheap": "동전주",
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
    if not shares:
        return 0
    vr, dd = metrics.get("vol_ratio") or 0, metrics.get("dd60") or 0
    turn = (metrics.get("volume") or 0) / shares * 100
    if price and price < 2 and dd <= -50 and vr >= 5 and 10 <= turn <= MAX_TURNOVER:
        return 3
    if shares >= 1e7:
        return 0
    if vr >= 10 and dd <= -50:
        return 2
    return 1 if vr >= 10 or (vr >= 5 and dd <= -50) else 0


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
    sig = {
        "crashed": dd60 <= -50,
        "volatile": (statistics.mean(rng) if rng else 0) >= 10 or gaps_dn >= 2 or max_dn <= -20,
        "spiked": spikes >= 1,
        "cheap": c < 2,
        "bleed": ret10 <= -10 and v60 > 0 and v10 / v60 <= 0.5,
        "vol_up": vol_ratio >= 1.5,
        "vol_up_strong": vol_ratio >= 3,
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
                        "box10": box10, "updown20": up_v / dn_v if dn_v else None}}


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
    ★★★, $2 미만, 전날 거래량 50배+ 일수록 이어지는 경우가 많았다. 둘 이상이면 A, 하나면 B, 없으면 C."""
    from .sessions import ET
    t = datetime.fromisoformat(ts).astimezone(ET)
    why = []
    if 6 <= t.hour < 9:
        why.append("프리 06~09시")
    if tier_ == 3:
        why.append("★★★")
    if price and price < 2:
        why.append("$2 미만")
    if (vol_ratio or 0) >= 50:
        why.append("전날 거래량 50배+")
    return ("A" if len(why) >= 2 else "B" if why else "C"), why


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
    from datetime import datetime, timezone
    from .sessions import KST
    from .trading.messages import money_s, pct, px
    m, dm = lv["metrics"], day["metrics"]
    j = judge(m, dm)
    on = set(day["signals"]) - ({"vol_up"} if "vol_up_strong" in day["signals"] else set())
    on_d = [lab for k, lab in DAILY_LABELS.items() if k in on]
    t = (now or datetime.now(timezone.utc)).astimezone(KST)
    g = m.get("grade")
    return "\n".join([
        (f"{ {'A': '🅰', 'B': '🅱', 'C': '©'}.get(g, '')} {g}급 " if g else "") + f"⚡ 급등 전조 ({sym}" + (f" · {name}" if name else "") + ")",
        "──────────────",
        f"💵 현재 {px(m['price'])} ({pct(m['chg'])}) · {session_ko}",
        f"현재 상태: {j['state']}",
        f"타이밍: {j['timing']}",
        f"거래 흐름: {j['flow']} (1시간 거래량 {m.get('vol_x', 0):.1f}배 · 오늘 {money_s(m.get('dollar_today'))})",
        *( [f"회전율: 오늘 {m.get('vol_today', 0) / shares * 100:.1f}% · 3분 {m.get('vol_3m', 0) / shares * 100:.2f}% (발행 {shares / 1e6:.1f}M주)"]
           if shares and m.get('vol_today', 0) / shares * 100 <= MAX_TURNOVER else [] ),
        *( [f"호가: 매도 +10% 안 {money_s(m['ask10'])}"
             + (f" · 3분 거래대금 {money_s(m.get('dollar_3m'))} → {'얇음 ✅' if m['ask10'] < (m.get('dollar_3m') or 0) else '두꺼움'}")]
           if m.get("ask10") is not None else [] ),
        f"3분 거래: {money_s(m.get('dollar_3m'))} · {m.get('vol_3m', 0):,.0f}주",
        f"변동성 위험: {j['risk']} (하루 변동폭 {dm.get('range30') or 0:.0f}%)",
        f"한줄 판단: {j['verdict']}",
        "",
        f"📉 차트 전조 {day['score']}점: " + " · ".join(on_d),
        *( [f"🏷 {' · '.join(tags)}"] if tags else [] ),
        f"   60일 고점 대비 {pct(dm['dd60'])} · 60일 급등 {dm['spikes60']}회 · 전날 거래량 {times(dm['vol_ratio'])}",
        f"⏱ 갱신: {t:%m.%d %H:%M:%S}",
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
    rows.sort(key=lambda r: (-len(set(r["live"]) & {"wake", "creep"}), -r["tier"], -r["score"], -(r["metrics"].get("vol_ratio") or 0)))
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
                 + (f" · 회전율 {x['turnover_d0']:.1f}%" if x.get("turnover_d0") is not None else "")
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
    head = f"({sym}" + (f" · {name}" if name else "") + ")"
    if kind == "order":
        basis = LIMIT_KO.get(st.get("entry"), "지정가").format(dip=st.get("dip", ""))
        return "\n".join([
            f"🧪 {tag}가상 지정가 주문 {head}",
            "──────────────",
            f"🧾 {px(v['entry'])} 에 매수 대기 ({basis}) · {st.get('minutes', 60)}분 유효 · {session_ko} · 실제 주문 아님",
            f"근거: {v['reason']}",
            "한줄 판단: 정규장 전에 거래가 터졌습니다. 추격하지 않고 털릴 때 받습니다.",
        ])
    if kind == "buy":
        e, tp, sl = v["entry"], v.get("tp") or st.get("tp", 10), v.get("sl") or st.get("sl", 35)
        return "\n".join([
            f"🧪 {tag}가상 매수 체결 {head}",
            "──────────────",
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
        f"{icon} {tag}가상 매도 {head} · {v['exit_reason']}",
        "──────────────",
        f"💵 {px(v['entry'])} → {px(v['exit_price'])} ({pct(ret)}) · {session_ko}",
        f"보유 {held_s} · 보유 중 최고 {pct((v['high'] / v['entry'] - 1) * 100)}{peak} · 최저 {pct((v['low'] / v['entry'] - 1) * 100)}",
        "실제 주문 아님 — 전략 검증용 기록",
    ])
