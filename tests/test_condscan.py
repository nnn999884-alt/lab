from pennybot import condscan as cs


def _bars(closes, start=1000):
    return [(start + i, c, c, c, c, 100) for i, c in enumerate(closes)]


def test_resample_groups_minutes():
    out = cs.resample([(0, 1, 2, 0.5, 1.5, 10), (1, 1.5, 3, 1, 2, 10), (5, 2, 2, 2, 2, 5)], 5)
    assert out[0] == (0, 1, 3, 0.5, 2, 20) and out[1][0] == 1


def test_ma_align_true_on_uptrend_and_false_on_downtrend():
    up = _bars([float(i) for i in range(1, 101)])
    dn = _bars([float(i) for i in range(100, 0, -1)])
    c = [{"t": "ma_align", "tf": 1, "ma": [5, 20, 60]}]
    assert cs.check(up, c) == [True]
    assert cs.check(dn, c) == [False]


def test_insufficient_data_is_skipped_not_failed():
    short = _bars([1.0] * 30)
    assert cs.check(short, [{"t": "ma_align", "tf": 1, "ma": [5, 20, 112]}]) == [None]


def test_bollinger_and_envelope_breakout_need_cross_on_last_bar():
    flat = [10.0] * 40
    assert cs.check(_bars(flat + [14.0]), [{"t": "bb_break", "tf": 1, "n": 20, "k": 2.0}]) == [True]
    assert cs.check(_bars(flat + [14.0, 15.0]), [{"t": "bb_break", "tf": 1, "n": 20, "k": 2.0}]) == [False]
    assert cs.check(_bars(flat + [14.0]), [{"t": "env_break", "tf": 1, "n": 12, "pct": 2.0}]) == [True]


def test_volume_and_marketcap_conditions():
    b = _bars([2.0] * 30)
    c = [{"t": "day_vol", "min": 300000}, {"t": "mcap", "min": 30e6}]
    assert cs.check(b, c, day_vol=500000, shares=20e6) == [True, True]
    assert cs.check(b, c, day_vol=1000, shares=None) == [False, None]


def test_presets_text_and_scan_on_db(tmp_path):
    import sqlite3
    db = tmp_path / "m.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE bars_1m(symbol,ts,trade_date,session,open,high,low,close,volume)")
    con.execute("CREATE TABLE stock_info(symbol,name,shares,list_date,updated)")
    con.execute("CREATE TABLE daily_bars(symbol,d,open,high,low,close,volume)")
    for i in range(300):
        px = 1 + i * 0.01
        con.execute("INSERT INTO bars_1m VALUES('UP',?,?,?,?,?,?,?,?)",
                    (f"2026-11-03T{i // 60:02d}:{i % 60:02d}:00+00:00", "2026-11-03", "PRE", px, px, px, px, 10000))
    con.commit(); con.close()
    cs._cache.clear()
    spec = {"t1": {"label": "t1", "min_ok": 1, "conds": [{"t": "ma_align", "tf": 5, "ma": [5, 20, 60]}]}}
    (tmp_path / "condscan.json").write_text(__import__("json").dumps(spec), encoding="utf-8")
    r = cs.scan(db, "t1", tmp_path, recent_min=600, ttl=0)
    assert [x["symbol"] for x in r["rows"]] == ["UP"] and r["rows"][0]["full"]
    assert all(cs.cond_text(c) for p in cs.PRESETS.values() for c in p["conds"])


def test_scan_serves_cache_without_waiting_and_refreshes_in_background(tmp_path):
    import sqlite3, time
    db = tmp_path / "m.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE bars_1m(symbol,ts,trade_date,session,open,high,low,close,volume)")
    con.execute("CREATE TABLE stock_info(symbol,name,shares,list_date,updated)")
    con.execute("CREATE TABLE daily_bars(symbol,d,open,high,low,close,volume)")
    con.commit(); con.close()
    cs._cache.clear(); cs._running.clear()
    first = cs.scan(db, "추세 돌파", tmp_path, ttl=30)
    assert first.get("loading") is True            # 처음엔 기다리지 않고 '계산 중'
    for _ in range(50):
        if cs._cache:
            break
        time.sleep(0.1)
    second = cs.scan(db, "추세 돌파", tmp_path, ttl=30)
    assert "rows" in second and not second.get("loading")


def test_hot_pins_keep_today_ab_and_drop_other_days(tmp_path):
    from datetime import datetime, timedelta, timezone
    from pennybot import precursor as pc
    db = str(tmp_path / "market.db")
    now = datetime.now(timezone.utc)
    pc._save_pins(db, {"NEW": {"grade": "B", "ts": now.isoformat(), "why": ["$10 미만"]},
                       "OLD": {"grade": "A", "ts": (now - timedelta(days=2)).isoformat(), "why": []},
                       "CEE": {"grade": "C", "ts": now.isoformat(), "why": []}})
    assert set(pc._load_pins(db)) == {"NEW"}


def test_halted_now_only_inside_halt_window():
    import sqlite3
    from datetime import datetime, timedelta, timezone
    from pennybot import precursor as pc
    now = datetime.now(timezone.utc)
    f = lambda d: (now + timedelta(minutes=d)).isoformat()
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE halts(symbol, halt_ts, resume_ts)")
    con.executemany("INSERT INTO halts VALUES(?,?,?)", [
        ("NOW", f(-2), f(3)), ("DONE", f(-30), f(-25)), ("OPEN", f(-5), None), ("OLDOPEN", f(-60), None)])
    assert pc._halted_now(con) == {"NOW", "OPEN"}


def test_trend_of_up_down_flat():
    up = _bars([float(i) for i in range(1, 201)])
    dn = _bars([float(i) for i in range(200, 0, -1)])
    assert cs.trend_of(up)["d"] == "up" and cs.trend_of(dn)["d"] == "down"
    assert cs.trend_of(_bars([5.0] * 200))["d"] == "flat"
    assert cs.trend_of(_bars([1.0] * 10)) is None
