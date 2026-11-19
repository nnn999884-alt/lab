"""알림 등급 · 당일 파동 · 과거 확률 (2026-11-13 검증, 10/29~11/12 알림 2,539건, 전 종목 현재가 기록 기준).

★ 조건 (세 기간 모두 '빠져도 결국 +20%' 비율을 올림): 최근 5일 안 +30% 급등 이력 · 전날 거래 평소(20일) 3배↑ · 전날 ±10%↑ 움직임.
⚠ = ★ 없음 + 알림 때 전날 대비 +10% 미만 → 결국 +20% 6% (세 기간 5/5/7%) → 텔레그램으로 안 보냄.
당일 파동 = 그날 3분봉 종가로, 꺾임이 '그날 지금까지 움직인 폭(고가−저가)'의 35% 이상이고 15% 이상일 때만 파동 끝/새 파동
(종목마다 크게 노는 정도가 달라 고정 % 는 잔물결을 셈 → 그날 폭에 맞춘 기준, 11/14 차트로 확인) — 정보로만 표시.
11/14 다시 검증: 처음엔 1분봉 10% 로 셌더니 급등주가 하루 11~19 파동이 나와 잔물결까지 셌고, '뒤쪽 파동·직전 고점 돌파가 잘 간다'는
결과도 그 착시였음 (5분봉 20% 로 세면 알림의 87% 가 1번째 파동, 3번째↑·돌파의 효과는 세 기간 중 마지막에만 → 확률에 안 씀).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
# (결국 +20% %, +50% %, 가기 전 빠짐 보통 %, 나쁠 때 %, 건수) — 그날 안, 알림 뒤 첫 실제 체결가 기준
STATS = {
    "★★": (31, 11, -5, -19, 687),           # 세 기간 22 / 27 / 36%
    "★": (23, 10, -2, -15, 616),            # 19 / 18 / 26%
    "·": (20, 7, -4, -13, 432),             # 9 / 19 / 28%
    "⚠": (6, 2, -2, -16, 799),              # 5 / 5 / 7%  → 안 보냄
}
WAVE_BAR_MIN, WAVE_RANGE, WAVE_FLOOR = 3, 0.35, 15.0
STATS_SPAN = "10/29~11/12"
# 차트 모양 (11/14 검증, 10/29~11/13 보낸 알림 ~1,800건). '그날 결국'은 이른 알림일수록 남은 시간이 길어 유리하므로
# 알림 뒤 2시간 안 +20% 로(2시간이 다 남은 알림만) 같은 세션끼리 비교해서 세 기간 모두 유지된 것만.
# 👍 알림 전 30분 25%↑ 출렁임   👎 20일 고점 근처(90%↑) · 알림 전 1시간 10%↓ 횡보 · 5일 박스 20%↓ · 3일 연속 음봉
# (매집봉 = 최근 5일 거래 5배↑ 장대양봉은 '결국'으론 좋아 보였지만 2시간 기준으론 평균과 같아 뺌)
PAT_BASE = 14   # 보낸 알림 전체의 2시간 안 +20% %
PAT_STATS = {   # (2시간 안 +20% %, +50% %, 건수, 기간별 +20%)
    "good": (34, 12, 220, "35/28/36"), "mixed": (20, 9, 109, "0/13/29"),
    "bad": (9, 3, 773, "6/11/9"), "none": (14, 4, 712, "15/17/13"),
}


def waves(px: list[float], X: float = 10.0, k: float = 0.0) -> tuple[int, list[float]]:
    """→ (상승 파동 수, 끝난 파동 고점들). 꺾임 = X% 이상 + 그날 지금까지 폭(고가−저가)의 k 이상."""
    if not px:
        return 0, []
    n, highs, mode, ext, lo = 1, [], "up", px[0], px[0]
    dlo = dhi = px[0]
    for p in px[1:]:
        dlo, dhi = min(dlo, p), max(dhi, p)
        need = k * (dhi - dlo)
        if mode == "up":
            if p > ext:
                ext = p
            elif p <= ext * (1 - X / 100) and ext - p >= need:
                highs.append(ext); mode, lo = "down", p
        else:
            if p < lo:
                lo = p
            elif p >= lo * (1 + X / 100) and p - lo >= need:
                n += 1; mode, ext = "up", p
    return n, highs


def day_feats(daily: list[tuple]) -> dict | None:
    """daily = [(d, high, close, volume)] 오래된 것부터, 오늘 제외. → 전날 종가·최근 5일 급등·전날 거래 배수·전날 등락."""
    if len(daily) < 7:
        return None
    pc = daily[-1][2]
    if not pc:
        return None
    vols = [x[2] * (x[3] or 0) for x in daily[-21:-1]]
    avg = sum(vols) / len(vols) if vols else 0
    run5 = max(((x[1] / y[2] - 1) * 100 for x, y in zip(daily[-5:], daily[-6:-1]) if y[2]), default=0)
    pp = daily[-2][2]
    return {"pc": pc, "run5": run5, "prev_vx": (daily[-1][2] * (daily[-1][3] or 0)) / avg if avg else 0,
            "prev": (pc / pp - 1) * 100 if pp else 0}


def day_patterns(rows: list[tuple]) -> dict:
    """rows = [(d, open, high, low, close, volume)] 오래된 것부터, 오늘 제외 → {good: [...], bad: [...]}."""
    good, bad = [], []
    if len(rows) < 26:
        return {"good": good, "bad": bad}
    o, h, l, c = ([float(x[i] or 0) for x in rows] for i in (1, 2, 3, 4))
    pc = c[-1]
    if pc and pc >= 0.9 * max(h[-20:]):
        bad.append("20일 고점 근처")
    if min(l[-5:]) > 0 and max(h[-5:]) / min(l[-5:]) - 1 < 0.2:
        bad.append("5일 횡보")
    if all(c[-k] < o[-k] for k in (1, 2, 3)):
        bad.append("3일 연속 음봉")
    return {"good": good, "bad": bad}


def intraday_patterns(seq: list[tuple], price: float, now: datetime) -> dict:
    """seq = [(ts iso, close)] 오늘 1분봉 → 알림 전 30분 출렁임(👍) · 1시간 횡보(👎)."""
    good, bad = [], []

    def win(sec):
        out = []
        for ts, c in seq:
            try:
                if (now - datetime.fromisoformat(ts)).total_seconds() <= sec:
                    out.append(c)
            except Exception:
                pass
        return out + [price]
    w30, w60 = win(1800), win(3600)
    if len(w30) >= 11 and min(w30) > 0 and max(w30) / min(w30) - 1 >= 0.25:
        good.append("30분 25%↑ 출렁임")
    if len(w60) >= 21 and min(w60) > 0 and max(w60) / min(w60) - 1 < 0.10:
        bad.append("1시간 횡보")
    return {"good": good, "bad": bad}


def pattern_line(pat: dict | None) -> str:
    g, b = (pat or {}).get("good") or [], (pat or {}).get("bad") or []
    if not g and not b:
        return ""
    p20, p50, n, _ = PAT_STATS["mixed" if g and b else "good" if g else "bad"]
    head = " · ".join(([f"👍 {' · '.join(g)}"] if g else []) + ([f"👎 {' · '.join(b)}"] if b else []))
    return f"\n🧩 {head} → 이런 모양 {n}건: 2시간 안 +20% {p20}% (보통 {PAT_BASE}%) · +50% {p50}%"


def grade(f: dict | None, price: float, wn: int, above_prev: bool, pat: dict | None = None) -> dict:
    """→ {skip, key, stars, chg, line}."""
    if not f or not price:
        return {"skip": False, "key": None, "line": ""}
    chg = (price / f["pc"] - 1) * 100
    why = [nm for ok, nm in ((f["run5"] >= 30, "최근 5일 급등"), (f["prev_vx"] >= 3, f"전날 거래 {f['prev_vx']:.0f}배"),
                             (abs(f["prev"]) >= 10, f"전날 {f['prev']:+.0f}%")) if ok]
    st = len(why)
    key = "⚠" if st == 0 and chg < 10 else "★★" if st >= 2 else "★" if st == 1 else "·"
    p20, p50, dd, bad, n = STATS[key]
    badge = "★★" if st >= 2 else "★" if st == 1 else "등급 없음"
    wave = f"오늘 {wn}번째 파동" + (" · 직전 파동 고점 위" if wn >= 2 and above_prev else "") if wn else ""
    line = (f"\n{badge}" + (f" ({' · '.join(why)})" if why else "") + (f" · {wave}" if wave else "")
            + f"\n📈 비슷한 알림 {n}건: 결국 +20% {p20}% · +50% {p50}% · 가기 전 보통 {dd}% (나쁠 때 {bad}%)" + pattern_line(pat))
    return {"skip": key == "⚠", "key": key, "stars": st, "chg": chg, "line": line}


def for_symbol(db_path_uri: str, sym: str, price: float, now: datetime | None = None, cache: dict | None = None) -> dict:
    """DB(읽기 전용 URI)에서 일봉·오늘 1분봉을 읽어 등급. 실패하면 등급 없음(skip 안 함)."""
    now = now or datetime.now(timezone.utc)
    td = (now.astimezone(ET) + timedelta(hours=4)).date()
    start = datetime(td.year, td.month, td.day, 20, tzinfo=ET) - timedelta(days=1)
    try:
        con = sqlite3.connect(db_path_uri, uri=True, timeout=5)
        try:
            key = (sym, td.isoformat())
            f = cache.get(key) if cache is not None else None
            if f is None:
                rows = list(reversed(con.execute(
                    "SELECT d, open, high, low, close, volume FROM daily_bars WHERE symbol=? AND d < ? ORDER BY d DESC LIMIT 30",
                    (sym, td.isoformat())).fetchall()))
                f = day_feats([(r[0], r[2], r[4], r[5]) for r in rows]) or {}
                if f:
                    f["pat"] = day_patterns(rows)
                if cache is not None:
                    cache[key] = f
            b5, seq = {}, []
            for ts, c in con.execute("SELECT ts, close FROM bars_1m WHERE symbol=? AND ts >= ? AND ts <= ? ORDER BY ts",
                                     (sym, start.astimezone(timezone.utc).isoformat(), now.astimezone(timezone.utc).isoformat())):
                if c:
                    b5[ts[:14] + str(int(ts[14:16]) // WAVE_BAR_MIN)] = c     # 3분 구간 마지막 종가
                    seq.append((ts, c))
            px = list(b5.values())
        finally:
            con.close()
    except Exception:
        return {"skip": False, "key": None, "line": ""}
    wn, highs = waves(px + [price], WAVE_FLOOR, WAVE_RANGE)
    dp = (f or {}).get("pat") or {"good": [], "bad": []}
    ip = intraday_patterns(seq, price, now)
    pat = {"good": dp["good"] + ip["good"], "bad": dp["bad"] + ip["bad"]}
    return grade(f or None, price, wn, bool(highs) and price > max(highs), pat)
