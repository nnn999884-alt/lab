"""알림 등급 · 당일 파동 · 과거 확률 (2026-11-13 검증, 10/29~11/12 알림 2,539건, 전 종목 현재가 기록 기준).

★ 조건 (세 기간 모두 '빠져도 결국 +20%' 비율을 올림): 최근 5일 안 +30% 급등 이력 · 전날 거래 평소(20일) 3배↑ · 전날 ±10%↑ 움직임.
⚠ = ★ 없음 + 알림 때 전날 대비 +10% 미만 → 결국 +20% 6% (세 기간 5/5/7%) → 텔레그램으로 안 보냄.
당일 파동 = 그날 가격을 지그재그(고점 −10% 면 파동 끝, 저점 +10% 면 새 파동). 2번째 파동↑ 이고 직전 파동 고점을 넘은 알림이 더 잘 감.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
# (결국 +20% %, +50% %, 가기 전 빠짐 보통 %, 나쁠 때 %, 건수) — 그날 안, 알림 뒤 첫 실제 체결가 기준
STATS = {
    "돌파": (38, 17, -4, -20, 175),          # 2번째 파동↑ · 직전 고점 돌파 · ★ 하나↑
    "★★1": (36, 14, -7, -26, 258), "★★2": (28, 9, -4, -15, 430),
    "★1": (21, 10, -3, -13, 291), "★2": (25, 10, -2, -16, 326),
    "·1": (19, 7, -4, -13, 243), "·2": (20, 8, -4, -15, 191),
    "⚠": (6, 2, -2, -16, 800),
}
STATS_SPAN = "10/29~11/12"


def waves(px: list[float], X: float = 10.0) -> tuple[int, list[float]]:
    """→ (상승 파동 수, 끝난 파동 고점들)."""
    if not px:
        return 0, []
    n, highs, mode, ext, lo = 1, [], "up", px[0], px[0]
    for p in px[1:]:
        if mode == "up":
            if p > ext:
                ext = p
            elif p <= ext * (1 - X / 100):
                highs.append(ext); mode, lo = "down", p
        else:
            if p < lo:
                lo = p
            elif p >= lo * (1 + X / 100):
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


def grade(f: dict | None, price: float, wn: int, above_prev: bool) -> dict:
    """→ {skip, key, stars, chg, line}."""
    if not f or not price:
        return {"skip": False, "key": None, "line": ""}
    chg = (price / f["pc"] - 1) * 100
    why = [nm for ok, nm in ((f["run5"] >= 30, "최근 5일 급등"), (f["prev_vx"] >= 3, f"전날 거래 {f['prev_vx']:.0f}배"),
                             (abs(f["prev"]) >= 10, f"전날 {f['prev']:+.0f}%")) if ok]
    st = len(why)
    w2 = "2" if wn >= 2 else "1"
    if st == 0 and chg < 10:
        key = "⚠"
    elif wn >= 2 and above_prev and st >= 1:
        key = "돌파"
    else:
        key = ("★★" if st >= 2 else "★" if st == 1 else "·") + w2
    p20, p50, dd, bad, n = STATS[key]
    badge = "★★" if st >= 2 else "★" if st == 1 else "등급 없음"
    wave = f"오늘 {wn}번째 파동" + (" · 직전 고점 돌파" if wn >= 2 and above_prev else "")
    line = (f"\n{badge}" + (f" ({' · '.join(why)})" if why else "") + f" · {wave}"
            f"\n📈 비슷한 알림 {n}건: 결국 +20% {p20}% · +50% {p50}% · 가기 전 보통 {dd}% (나쁠 때 {bad}%)")
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
                rows = con.execute("SELECT d, high, close, volume FROM daily_bars WHERE symbol=? AND d < ? ORDER BY d DESC LIMIT 25",
                                   (sym, td.isoformat())).fetchall()
                f = day_feats(list(reversed(rows))) or {}
                if cache is not None:
                    cache[key] = f
            px = [r[0] for r in con.execute("SELECT close FROM bars_1m WHERE symbol=? AND ts >= ? AND ts <= ? ORDER BY ts",
                                            (sym, start.astimezone(timezone.utc).isoformat(), now.astimezone(timezone.utc).isoformat())) if r[0]]
        finally:
            con.close()
    except Exception:
        return {"skip": False, "key": None, "line": ""}
    wn, highs = waves(px + [price])
    return grade(f or None, price, wn, bool(highs) and price > max(highs))
