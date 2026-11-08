import sqlite3
from datetime import date, timedelta

from pennybot import dna


def _db(tmp_path, spike=True, shares=2e6, fall=True):
    p = tmp_path / "m.db"
    con = sqlite3.connect(p)
    con.executescript("CREATE TABLE daily_bars (symbol TEXT, d TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL);"
                      "CREATE TABLE stock_info (symbol TEXT, name TEXT, shares REAL, list_date TEXT, updated TEXT);"
                      "CREATE TABLE universe (snapshot_date TEXT, symbol TEXT, market TEXT, name TEXT, last REAL);"
                      "CREATE TABLE prices (symbol TEXT, ts TEXT, trade_date TEXT, session TEXT, last REAL, price_ts TEXT);"
                      "CREATE TABLE events (ts TEXT, symbol TEXT, kind TEXT, text TEXT, value REAL);")
    con.execute("INSERT INTO stock_info VALUES ('PEN','펜',?,NULL,NULL)", (shares,))
    d = date(2026, 7, 1)
    px = 2.0
    for i in range(80):
        hi = px * (1.4 if spike and i == 40 else 1.02)
        if i >= 75 and fall:
            px *= 0.95                                   # 마지막 5일 하락 → 바닥
        con.execute("INSERT INTO daily_bars VALUES ('PEN',?,?,?,?,?,10000)", ((d + timedelta(days=i)).isoformat(), px, hi, px * 0.98, px))
    con.commit()
    return con, p


def test_picks_tiny_float_bottom_repeat_runner(tmp_path):
    con, p = _db(tmp_path)
    d0, rows = dna.compute(con)
    assert [r["symbol"] for r in rows] == ["PEN"] and rows[0]["spikes60"] == 1 and rows[0]["r5"] < 0
    dna.save(con, d0, rows)
    out = dna.latest(str(p))
    assert out["d0"] == d0 and out["rows"][0]["symbol"] == "PEN" and out["rows"][0]["name"] is None
    r = out["rows"][0]
    assert round(r["last_spike_pct"]) == 40 and r["last_spike_days"] == 39   # 40번째 날 +40%, 그 뒤 39거래일


def test_summary_text_reports_previous_list_and_new_list(tmp_path):
    con, p = _db(tmp_path)
    d0, rows = dna.compute(con)
    dna.save(con, "2026-01-01", [dict(rows[0])])           # 예전 목록 하나
    prev = dna.previous(con, d0)
    assert prev == ("2026-01-01", ["PEN"])
    msg = dna.summary_text(d0, rows, prev, {"PEN": "펜"}, con)
    assert "급등 이력 감시 정리" in msg and "PEN 펜" in msg and "매수 신호 아님" in msg


def test_rejects_without_spike_big_float_or_no_drop(tmp_path):
    for kw in ({"spike": False}, {"shares": 50e6}, {"fall": False}):
        con, _ = _db(tmp_path / str(len(kw)) if False else tmp_path, **kw)
        assert dna.compute(con)[1] == [], kw
        con.close()
        (tmp_path / "m.db").unlink()
