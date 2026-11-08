import json
import sqlite3

from pennybot import condscan as cs
from pennybot import screens as sc


def _day(prices, vol=10000, ses="PRE", start_h=9):
    out = []
    for i, p in enumerate(prices):
        m = start_h * 60 + i
        out.append((f"2026-11-06T{m // 60:02d}:{m % 60:02d}:00+00:00", ses, p, p * 1.01, p * 0.99, p, vol))
    return out


def _prior(n=30, close=1.0, vol=100000):
    return [(f"2026-10-{i + 1:02d}", close, close * 1.05, close * 0.95, close, vol) for i in range(n)]


def test_basic_features():
    f = sc.Feat(_day([1.0] * 30 + [1.2] * 10, vol=50000), _prior(), 5e6)
    assert round(f.get("chg"), 1) == 20.0
    assert f.get("dayvol") == 40 * 50000 and round(f.get("rvol"), 1) == 20.0
    assert round(f.get("rot"), 2) == 0.4 and round(f.get("mcap")) == 6_000_000
    assert f.get("hod_dist") < 1.1 and f.get("vwap_dist") > 0
    assert f.get("et_min") == 4 * 60 + 39          # 09:39 UTC → 11/06 은 서머타임 끝난 뒤(UTC−5) → 04:39 ET


def test_missing_data_is_none_not_false():
    f = sc.Feat(_day([1.0] * 10), [], None)
    assert f.get("chg") is None and f.get("rot") is None and f.get("hi52_dist") is None
    assert sc.evaluate({"t": "f", "k": "rot", "min": 1}, f) is None
    assert sc.evaluate({"t": "ses", "in": ["PRE"]}, f) is True


def test_flags_red_green_reversal_orb():
    pr = _prior(close=1.0)
    rg = sc.Feat(_day([0.98] * 5 + [1.01] * 5, ses="REGULAR"), pr, None)
    assert rg.get("red_green") == 1
    rv = sc.Feat(_day([0.94] * 5 + [0.98] * 5, ses="REGULAR"), pr, None)
    assert rv.get("reversal") == 1
    orb = sc.Feat(_day([1.0] * 20 + [1.2], ses="REGULAR"), pr, None)
    assert orb.get("orb") == 1


def test_dilution_news_halts_respect_time():
    day = _day([1.0] * 10)
    now = day[-1][0]
    side = {"filings": [("2026-11-01T00:00:00+00:00", "S-3", 0), ("2026-11-05T00:00:00+00:00", "8-K", -1),
                        ("2026-11-07T00:00:00+00:00", "424B5", -1)],          # 미래 공시는 세지 않는다
            "news": ["2026-11-06T08:00:00+00:00", "2026-11-04T00:00:00+00:00"],
            "halts": [("2026-11-06T09:00:00+00:00", "2026-11-06T09:05:00+00:00")]}
    f = sc.Feat(day, _prior(), None, side)
    assert f.get("shelf") == 1 and f.get("dil180") == 1 and f.get("news24") == 1
    assert 0 <= f.get("resume_min") <= 10 and now > "2026-11-06T09:05"


def test_side_at_uses_only_earlier_days():
    sd = {"filings": [], "news": [], "halts": [], "fee_by_d": [("2026-11-05", 30.0), ("2026-11-06", 99.0)],
          "sr_by_d": [], "sp_by_d": [("2026-11-04", 0.8)]}
    s = sc.side_at(sd, "2026-11-06")
    assert s["fee"] == 30.0 and s["spread"] == 0.8 and s["short_ratio"] is None


def test_presets_are_merged_into_condscan_and_have_text():
    pre = cs.presets(None)
    assert "로스 5대 조건" in pre and pre["로스 5대 조건"]["group"] == "해외"
    assert all(len(n) <= 40 for n in pre)
    for p in sc.PRESETS.values():
        assert p["min_ok"] == len(p["conds"])
        assert all(cs.cond_text(c) for c in p["conds"])


def test_scan_runs_overseas_preset_on_db(tmp_path):
    db = tmp_path / "m.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE bars_1m(symbol,ts,trade_date,session,open,high,low,close,volume)")
    con.execute("CREATE TABLE stock_info(symbol,name,shares,list_date,updated)")
    con.execute("CREATE TABLE daily_bars(symbol,d,open,high,low,close,volume)")
    for i in range(60):
        px = 1.0 if i < 40 else 1.3
        con.execute("INSERT INTO bars_1m VALUES('RUN',?,?,?,?,?,?,?,?)",
                    (f"2026-11-06T{9 + i // 60:02d}:{i % 60:02d}:00+00:00", "2026-11-06", "PRE", px, px, px, px, 50000))
    for d in range(20):
        con.execute("INSERT INTO daily_bars VALUES('RUN',?,1,1.05,0.95,1.0,100000)", (f"2026-10-{d + 1:02d}",))
    con.execute("INSERT INTO stock_info VALUES('RUN','Run Inc',5000000,NULL,NULL)")
    con.commit(); con.close()
    cs._cache.clear()
    r = cs.scan(db, "로스 5대 조건", None, recent_min=600, ttl=0)
    assert [x["symbol"] for x in r["rows"]] == ["RUN"] and r["rows"][0]["full"]
    assert r["groups"]["로스 5대 조건"] == "해외" and r["groups"]["추세 돌파"] == "기본"
    json.dumps(r, ensure_ascii=False)


def test_eval_summary_and_text():
    evs = [{"d": "2026-11-01", "s": "A", "ses": "PRE", "hits": ["로스 5대 조건"], "up": 40, "ret": 30},
           {"d": "2026-11-05", "s": "B", "ses": "PRE", "hits": [], "up": 1, "ret": -15}]
    rows = sc.evaluate_all(evs, "2026-11-04")
    ross = next(r for r in rows if r["name"] == "로스 5대 조건")
    assert ross["a"]["n"] == 1 and ross["b"]["n"] == 0
    assert "판정" in sc.text(rows, "2026-11-04", None)
