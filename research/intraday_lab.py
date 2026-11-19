"""분봉 전략 연구실 (독립 코드). 정규장(09:30~16:00 ET) 후보일: 전날 +20%↑ 마감한 종목의 다음 날 (전날 정보만으로 정함).
⚠ 1분봉 DB 는 움직인 종목 위주로 채워져 있어 '그날 움직임'이 표본에 섞임 → 실패는 확정, 성공은 현재가 기록(prices)으로 재확인 필요."""
import sqlite3, sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import numpy as np
sys.path.insert(0, "D:/pennybot-live/research")
import daily_lab as L
ET, UTC = ZoneInfo("America/New_York"), timezone.utc
CONS = [sqlite3.connect(f"file:D:/pennybot-live/state/{db}?mode=ro", uri=True) for db in ("market.db", "research.db")]
L.SPLITS = {"탐색": ("2025-09-01", "2026-03-31"), "검증": ("2026-04-01", "2026-08-31"), "최종": ("2026-09-01", "2026-11-13")}


def day_bars(sym, d):
    st = datetime.fromisoformat(d).replace(tzinfo=ET)
    a, b = (st + timedelta(hours=9, minutes=30)).astimezone(UTC).isoformat(), (st + timedelta(hours=16)).astimezone(UTC).isoformat()
    best = []
    for con in CONS:
        rows = con.execute("SELECT ts, open, high, low, close, volume FROM bars_1m WHERE symbol=? AND ts >= ? AND ts < ? ORDER BY ts", (sym, a, b)).fetchall()
        if len(rows) > len(best):
            best = rows
    if len(best) < 120:
        return None
    t0 = datetime.fromisoformat(best[0][0])
    a = np.array([r[1:] for r in best], float)
    idx = np.array([(datetime.fromisoformat(r[0]).astimezone(ET) - st).total_seconds() / 60 - 570 for r in best])   # 09:30 = 0
    return idx, a[:, 0], a[:, 1], a[:, 2], a[:, 3], a[:, 4]


def candidates(S, since="2025-09-01"):
    out = []
    for s, z in S.items():
        for i in range(25, len(z["c"]) - 1):
            if z["d"][i + 1] >= since and z["ret1"][i] >= 0.20 and z["adv"][i] >= L.MIN_ADV and 0.1 <= z["c"][i] <= 10:
                out.append((s, z["d"][i + 1], z["c"][i]))
    return out


def trade(o, h, l, c, k, tp, sl, maxbars=None, end=None):
    """k 봉 신호 → k+1 봉 시가 진입. 같은 봉에 손절·익절 둘 다면 손절 먼저."""
    n = len(c)
    if k + 1 >= n:
        return None
    e = o[k + 1]
    if not (0.10 <= e <= 10):
        return None
    last = n - 1 if end is None else min(n - 1, end)
    if maxbars:
        last = min(last, k + 1 + maxbars)
    for j in range(k + 1, last + 1):
        if sl is not None and l[j] <= e * (1 - sl):
            return -sl
        if tp is not None and h[j] >= e * (1 + tp):
            return tp
    return float(np.clip(c[last] / e - 1, -1, 3))


def vwap(h, l, c, v):
    return np.cumsum((h + l + c) / 3 * v) / np.maximum(np.cumsum(v), 1e-9)
