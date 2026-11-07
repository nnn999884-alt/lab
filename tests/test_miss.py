import sqlite3

from pennybot import miss


def _db():
    con = sqlite3.connect(":memory:")
    con.executescript(
        "CREATE TABLE daily_bars (symbol TEXT, d TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL);"
        "CREATE TABLE stock_info (symbol TEXT, name TEXT, shares REAL, list_date TEXT, updated TEXT);"
        "CREATE TABLE universe (snapshot_date TEXT, symbol TEXT, market TEXT, name TEXT, last REAL);"
        "CREATE TABLE bars_1m (symbol TEXT, ts TEXT, trade_date TEXT, session TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL);"
        "CREATE TABLE events (ts TEXT, symbol TEXT, kind TEXT, text TEXT, value REAL);"
        "CREATE TABLE precursor_daily (d0 TEXT, symbol TEXT, score INTEGER, signals TEXT, metrics TEXT, close REAL, created TEXT);")
    for s in ("FAST", "LATE", "MISS"):
        con.execute("INSERT INTO daily_bars VALUES (?, '2026-11-05', 1, 1, 1, 1.0, 1000)", (s,))
        con.execute("INSERT INTO daily_bars VALUES (?, '2026-11-06', 1, 1.6, 1, 1.3, 9000)", (s,))
        con.execute("INSERT INTO stock_info VALUES (?, ?, 3e6, NULL, NULL)", (s, s))
        for i, px in enumerate((1.02, 1.05, 1.12, 1.25, 1.5, 1.6)):
            con.execute("INSERT INTO bars_1m VALUES (?, ?, '2026-11-06', 'REGULAR', ?, ?, ?, ?, 1000)",
                        (s, f"2026-11-06T14:{30 + i:02d}:00+00:00", px - .01, px, px - .02, px))
    con.execute("INSERT INTO universe VALUES ('2026-11-06', 'FAST', 'NASDAQ', 'f', 1)")
    con.execute("INSERT INTO events VALUES ('2026-11-06T14:31:10+00:00', 'FAST', 'fast', '', 0)")    # +5% 지점
    con.execute("INSERT INTO events VALUES ('2026-11-06T14:34:10+00:00', 'LATE', 'burst', '', 0)")   # +50% 지점
    return con


def test_review_marks_early_late_and_miss_with_reasons():
    con = _db()
    d, rows = miss.review(con, "2026-11-06")
    v = {r["symbol"]: r for r in rows}
    assert v["FAST"]["verdict"] == "early" and v["LATE"]["verdict"] == "late" and v["MISS"]["verdict"] == "miss"
    assert "감시 가격대 밖(유니버스에 없음)" in v["MISS"]["reasons"]
    assert round(v["LATE"]["lag_min"]) == 2                  # +10% 넘은 14:32 → 알림 14:34
    miss.save(con, d, rows)
    msg = miss.summary_text(d, rows, miss.history(con))
    assert "놓친 급등주 복기" in msg and "MISS" in msg and "LATE" in msg
