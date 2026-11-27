import json
import sqlite3
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from pennybot.config import Config
from pennybot.dashboard import make_server
from pennybot.dashboard.demo import build


@pytest.fixture
def server(tmp_path):
    cfg = Config()
    cfg.trading.db_path, cfg.collector.db_path = build(tmp_path)
    cfg.state_dir = str(tmp_path)
    srv = make_server(cfg, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", tmp_path
    srv.shutdown()


def get(url):
    with urllib.request.urlopen(url) as r:
        return json.loads(r.read())


def post(url, body, origin):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **({"Origin": origin} if origin else {})})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def test_read_endpoints(server):
    base, _ = server
    ov = get(base + "/api/overview")
    assert ov["mode"] == "paper" and ov["open_trades"] == 3
    st = {s["strategy"]: s for s in get(base + "/api/strategies")}
    assert set(st) == {"runner_overnight", "scalp", "squeeze", "news", "reversal", "pump", "catalyst_pullback",
                       "premarket_turnover_breakout", "open_vwap_hold", "overnight_no_dilution_hold",
                       "offering_support_bounce", "theme_chain"}
    assert st["scalp"]["config_enabled"] is True and st["runner_overnight"]["config_enabled"] is True
    pos = get(base + "/api/positions")
    assert {p["symbol"] for p in pos} == {"PENX", "BIOZ", "NANO"}
    assert any("unrealized" in p for p in pos)
    col = get(base + "/api/collector")
    assert [s["session"] for s in col["sessions"]] == ["DAY", "PRE", "REGULAR", "AFTER"]
    res = get(base + "/api/research")
    assert res and any(r["attention"]["n"] for r in res)
    assert len(get(base + "/api/equity?days=1")) > 100
    rn = get(base + "/api/runners")
    assert rn["rows"] and {b["bucket"] for b in rn["by_bucket"]} >= {"low", "high"}
    assert all("open" in r["ret"] for r in rn["rows"])
    ev = get(base + "/api/dilution")
    assert any(e["events"] == "ATM_TERMINATED" and e["direction"] == 1 for e in ev)
    bt = get(base + "/api/backtests")
    assert [b["run"] for b in bt] == ["demo-2", "demo-1"] and "trades" in bt[0] and "trades" not in bt[1]
    assert bt[0]["params"] == {"runner_overnight.trail_pct": 6}
    reps = get(base + "/api/reports")
    assert reps and reps[0]["text"].startswith("📊 일일 리포트") and "trades" in reps[0]
    assert [r["trade_date"] for r in reps] == sorted((r["trade_date"] for r in reps), reverse=True)
    sr = get(base + "/api/signal_reasons?hours=6")
    assert sr["total_rejected"] > 0 and sr["top"]
    labels = {x["reason"] for x in sr["top"]}
    assert "스프레드 너무 넓음" in labels and "리스크 관리에 막힘" in labels
    sh = get(base + "/api/shorts")
    assert sh and sh[0]["held"] and any(r["ssr"] for r in sh) and all("fee_rate" in r for r in sh)
    b = get(base + "/api/symbol?s=penx")
    assert b["found"] and b["ssr"] and b["short"]["ratio"] and b["borrow"]["fee_rate"] and b["halts"]
    assert get(base + "/api/symbol?s=NOPE")["found"] is False
    with urllib.request.urlopen(base + "/") as r:
        assert b"Mongbot" in r.read()                    # 페이지 제목 (사용자가 Pennybot → Mongbot 으로 바꿈)


def test_recent_closes_follow_exit_time_and_update_immediately(server):
    base, state = server
    db = state / "trading.db"
    con = sqlite3.connect(db)
    old_id = con.execute("SELECT id FROM trades WHERE status='CLOSED' ORDER BY id LIMIT 1").fetchone()[0]
    with con:
        con.execute("UPDATE trades SET exit_ts=?, pnl=? WHERE id=?",
                    ("2030-01-01T00:00:00+00:00", 12.5, old_id))
    path = "/api/trades?limit=1&recent_closes_test=1"
    first = get(base + path)
    assert first[0]["id"] == old_id
    with con:
        con.execute("UPDATE trades SET pnl=? WHERE id=?", (19.5, old_id))
    con.close()
    assert get(base + path)[0]["pnl"] == 19.5


def test_recent_orders_follow_last_status_update(server):
    base, state = server
    con = sqlite3.connect(state / "trading.db")
    old_id = con.execute("SELECT order_id FROM orders ORDER BY ts LIMIT 1").fetchone()[0]
    with con:
        con.execute("INSERT INTO orders (order_id, ts, symbol, side, type, qty, price, status, filled, updated) "
                    "VALUES ('newer-submitted', '2029-01-01T00:00:00+00:00', 'NEWX', 'BUY', 'LIMIT', 1, 1, 'OPEN', 0, '2029-01-01T00:00:00+00:00')")
        con.execute("UPDATE orders SET updated=?, status='CANCELED' WHERE order_id=?",
                    ("2030-01-01T00:00:00+00:00", old_id))
    con.close()
    orders = get(base + "/api/orders?limit=1&recent_orders_test=1")
    assert orders[0]["order_id"] == old_id


def test_paper_a1_m1_signals_do_not_appear_as_virtual_trades(tmp_path):
    from pennybot.dashboard.server import Data

    cfg = Config()
    cfg.mode = "paper"
    cfg.trading.db_path, cfg.collector.db_path = build(tmp_path)
    cfg.state_dir = str(tmp_path)
    cfg.trading.strategies["alert_limit"]["enabled"] = True
    cfg.trading.strategies["model_pick"]["enabled"] = True
    con = sqlite3.connect(cfg.collector.db_path)
    with con:
        con.executemany(
            "INSERT INTO vsignals(symbol,d0,opened,session,entry,score,reason,status,strat) "
            "VALUES (?, '2026-11-25', '2026-11-25T10:00:00+00:00', 'PRE', 1, 1, '', 'OPEN', ?)",
            [("AONE", "A1"), ("MONE", "M1"), ("OTHER", "S1")],
        )
    con.close()
    rows = Data(cfg).virtual_signals(100)
    assert {r["strat"] for r in rows} == {"S1"}


def test_dashboard_view_reset_filters_history_without_deleting_db(tmp_path):
    from pennybot.dashboard.server import Data

    cfg = Config()
    cfg.trading.db_path, cfg.collector.db_path = build(tmp_path)
    cfg.state_dir = str(tmp_path)
    since = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(timespec="seconds")
    (tmp_path / "dashboard_view.json").write_text(json.dumps({"since": since}), encoding="utf-8")
    data = Data(cfg)
    con = sqlite3.connect(cfg.trading.db_path)
    assert con.execute("SELECT COUNT(*) FROM orders").fetchone()[0] > 0
    assert con.execute("SELECT COUNT(*) FROM trades WHERE status='CLOSED'").fetchone()[0] > 0
    assert data.orders(10) == []
    assert data.trades(10) == []
    assert data.equity(7) == []
    assert all(row["trades"] == 0 and row["pnl"] == 0 for row in data.strategies())
    order_id = con.execute("SELECT o.order_id FROM orders o JOIN trades t ON t.id=o.trade_id "
                           "WHERE t.strategy='runner_overnight' LIMIT 1").fetchone()[0]
    with con:
        con.execute("UPDATE orders SET ts=?, updated=? WHERE order_id=?", (since, since, order_id))
        con.execute("UPDATE trades SET exit_ts=? WHERE id=(SELECT MIN(id) FROM trades WHERE status='CLOSED')", (since,))
    con.close()
    assert data.orders(10)[0]["order_id"] == order_id
    assert data.trades(10)[0]["exit_ts"] == since


def test_strategy_today_pnl_uses_korean_calendar_day_across_us_trade_dates(tmp_path):
    from pennybot.dashboard.server import Data

    cfg = Config()
    cfg.trading.db_path, cfg.collector.db_path = build(tmp_path)
    cfg.state_dir = str(tmp_path)
    kst = timezone(timedelta(hours=9))
    day = datetime.now(kst).date()
    midnight = datetime.combine(day, datetime.min.time(), tzinfo=kst)
    (tmp_path / "dashboard_view.json").write_text(json.dumps({"since": midnight.isoformat()}), encoding="utf-8")
    con = sqlite3.connect(cfg.trading.db_path)
    with con:
        con.executemany(
            "INSERT INTO trades(strategy,symbol,mode,trade_date,status,pnl,pnl_pct,exit_ts) "
            "VALUES ('runner_overnight',?,'paper',?,'CLOSED',?,0,?)",
            [("EARLY", (day - timedelta(days=1)).isoformat(), 1.0,
              (midnight + timedelta(hours=1)).astimezone(timezone.utc).isoformat()),
             ("LATE", day.isoformat(), 2.0,
              (midnight + timedelta(hours=10)).astimezone(timezone.utc).isoformat())],
        )
    con.close()
    row = next(x for x in Data(cfg).strategies() if x["strategy"] == "runner_overnight")
    assert row["today_trades"] == 2
    assert row["today_pnl"] == 3.0


def test_symbol_detail_reads_paper_trades_from_trading_db(server):
    base, _ = server
    detail = get(base + "/api/symbol_extra?s=PENX&paper_detail_test=1")
    assert detail["paper_trades"]
    assert any(r["strat"] == "runner_overnight" for r in detail["paper_trades"])


def test_signal_reasons_counts_latest_symbol_strategy_only(server):
    base, state = server
    before = get(base + "/api/signal_reasons?hours=6&test=before")
    now = datetime.now(timezone.utc)
    con = sqlite3.connect(state / "trading.db")
    with con:
        con.executemany(
            "INSERT INTO signals (ts,trade_date,session,strategy,symbol,ok,reason,score,price) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            [
                ((now - timedelta(seconds=3)).isoformat(), "2026-11-20", "REGULAR", "dedup", "DUPX", 0,
                 "호가 없음", 0, 1),
                ((now - timedelta(seconds=2)).isoformat(), "2026-11-20", "REGULAR", "dedup", "DUPX", 0,
                 "스프레드 9%", 0, 1),
                ((now - timedelta(seconds=1)).isoformat(), "2026-11-20", "REGULAR", "dedup", "DUPX", 1,
                 "통과", 1, 1),
                (now.isoformat(), "2026-11-20", "REGULAR", "dedup", "LASTX", 0,
                 "호가 없음", 0, 1),
            ],
        )
    con.close()
    after = get(base + "/api/signal_reasons?hours=6&test=after")
    assert after["total_rejected"] == before["total_rejected"] + 1
    assert after["total_scans"] == before["total_scans"] + 2


def test_password_required_when_set(tmp_path):
    import base64
    cfg = Config()
    cfg.trading.db_path, cfg.collector.db_path = build(tmp_path)
    cfg.state_dir = str(tmp_path)
    cfg.dashboard_password = "s3cret"
    srv = make_server(cfg, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        for auth in (None, "Basic " + base64.b64encode(b"x:wrong").decode(), "Basic !!!"):
            req = urllib.request.Request(base + "/api/overview", headers={"Authorization": auth} if auth else {})
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(req)
            assert e.value.code == 401 and "Basic" in e.value.headers["WWW-Authenticate"]
        ok = {"Authorization": "Basic " + base64.b64encode(b"me:s3cret").decode()}
        with urllib.request.urlopen(urllib.request.Request(base + "/api/overview", headers=ok)) as r:
            assert json.loads(r.read())["mode"] == "paper"
        req = urllib.request.Request(base + "/api/control", data=b'{"kill": true}', method="POST",
                                     headers={"Content-Type": "application/json", "Origin": base})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == 401
    finally:
        srv.shutdown()


def test_external_host_is_read_only_without_password(server):
    base, _ = server
    ext = "https://abc.trycloudflare.com"
    req = urllib.request.Request(base + "/api/overview", headers={"Host": "abc.trycloudflare.com"})
    with urllib.request.urlopen(req) as r:                                   # 보기는 된다
        ov = json.loads(r.read())
        assert ov["mode"] == "paper" and ov["readonly"] is True
    assert get(base + "/api/overview")["readonly"] is False                  # 이 컴퓨터에서는 조작 가능
    req = urllib.request.Request(base + "/api/control", data=b'{"kill": true}', method="POST",
                                 headers={"Content-Type": "application/json", "Host": "abc.trycloudflare.com",
                                          "Origin": ext})
    with pytest.raises(urllib.error.HTTPError) as e:                          # 조작은 막힌다
        urllib.request.urlopen(req)
    assert e.value.code == 403 and "보기만" in json.loads(e.value.read())["error"]


def test_control_requires_same_origin(server):
    base, tmp = server
    with pytest.raises(urllib.error.HTTPError) as e:
        post(base + "/api/control", {"kill": True}, origin="https://evil.example")
    assert e.value.code == 403
    with pytest.raises(urllib.error.HTTPError):
        post(base + "/api/control", {"kill": True}, origin=None)
    d = post(base + "/api/control", {"kill": True}, origin=base)
    assert d["kill"] is True
    d = post(base + "/api/control", {"strategy": "day_dip", "enabled": False}, origin=base)
    assert d["strategies"]["day_dip"] is False
    assert json.loads((tmp / "control.json").read_text())["kill"] is True
    with pytest.raises(urllib.error.HTTPError) as e:
        post(base + "/api/control", {"strategy": "nope", "enabled": True}, origin=base)
    assert e.value.code == 400


def test_trading_limits_control_validates_and_reports_effective_values(server):
    base, tmp = server
    d = post(base + "/api/control", {"limits": {
        "max_open_positions": 4,
        "strategy_max_positions": {"alert_limit": 2, "model_pick": 3, "runner_overnight": 1},
        "cash_per_trade_pct": 15.0,
        "max_position_pct": 25.0,
        "max_position_usd": 500.0,
        "risk_per_trade_pct": 2.0,
    }}, origin=base)
    assert d["limits"]["max_open_positions"] == 4
    overview = get(base + "/api/overview")
    assert overview["limits"]["cash_per_trade_pct"] == 15.0
    assert overview["limits"]["risk_per_trade_pct"] == 2.0
    by = {r["strategy"]: r for r in get(base + "/api/strategies")}
    assert by["runner_overnight"]["max_positions"] == 1
    with pytest.raises(urllib.error.HTTPError) as e:
        post(base + "/api/control", {"limits": {"cash_per_trade_pct": 150}}, origin=base)
    assert e.value.code == 400
    assert json.loads((tmp / "control.json").read_text())["limits"]["cash_per_trade_pct"] == 15.0


def test_dashboard_rejects_live_mode_when_ls_only_supplies_market_data(server, monkeypatch):
    base, _ = server
    monkeypatch.setenv("LS_APP_KEY", "test-key")
    monkeypatch.setenv("LS_APP_SECRET", "test-secret")
    monkeypatch.delenv("PENNYBOT_DATA_SOURCE", raising=False)
    monkeypatch.setenv("PENNYBOT_LIVE_CONFIRM", "YES")
    assert get(base + "/api/overview")["live_supported"] is False
    with pytest.raises(urllib.error.HTTPError) as e:
        post(base + "/api/mode", {"mode": "live"}, origin=base)
    assert e.value.code == 400


def test_symbol_extra(server):
    base, _ = server
    x = get(base + "/api/symbol_extra?s=penx")
    assert x["symbol"] == "PENX"
    assert set(x) >= {"shares", "events", "paper_trades"}               # 데모 DB 에 없는 항목(일봉 점수 등)은 빠질 뿐 오류 없음
    assert isinstance(x["events"], list) and isinstance(x["paper_trades"], list)
    assert get(base + "/api/symbol_extra?s=NOPE")["events"] == []


def test_settings_endpoint(server):
    base, tmp = server
    assert get(base + "/api/settings")["strategies"]["S1"] is True
    out = post(base + "/api/settings", {"strategies": {"S2": False}}, base)
    assert out["strategies"]["S2"] is False and get(base + "/api/settings")["strategies"]["S2"] is False
    with pytest.raises(urllib.error.HTTPError):
        post(base + "/api/settings", {"realtime": False}, "http://evil.example")   # 다른 출처 거부


def test_detections_track_outcome(server):
    import sqlite3
    from datetime import datetime, timedelta, timezone
    base, tmp = server
    db = sqlite3.connect(get_market_path(tmp))
    t0 = (datetime.now(timezone.utc) - timedelta(days=3)).replace(hour=14, minute=0, second=0, microsecond=0)   # 10:00 ET 정규장
    iso = lambda m: (t0 + timedelta(minutes=m)).isoformat()
    d = (t0 - timedelta(hours=4)).date().isoformat()
    prev = (t0 - timedelta(days=1)).date().isoformat()
    db.execute("INSERT OR REPLACE INTO daily_bars (symbol, d, open, high, low, close, volume) VALUES ('WINR', ?, 1, 1, 1, 1.0, 1)", (prev,))
    db.execute("INSERT OR REPLACE INTO daily_bars (symbol, d, open, high, low, close, volume) VALUES ('LOSR', ?, 1, 1, 1, 1.0, 1)", (prev,))
    for sym, path in (("WINR", [(0, 1.10, 1.10, 1.10), (5, 1.35, 1.08, 1.30)]), ("LOSR", [(0, 1.10, 1.10, 1.10), (5, 1.12, 0.95, 0.96)])):
        for m, hi, lo, c in path:
            db.execute("INSERT OR REPLACE INTO bars_1m (symbol, ts, trade_date, session, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?,?,1000)",
                       (sym, iso(m), d, "REGULAR", c, hi, lo, c))
        db.execute("INSERT OR REPLACE INTO events (ts, symbol, kind, text, value) VALUES (?,?,'burst','📶 test',10)", (iso(0), sym))
    db.commit()
    rows = {r["symbol"]: r for r in get(base + "/api/detections?hours=200")["rows"]}
    w, lz = rows["WINR"], rows["LOSR"]
    assert w["price"] == 1.10 and w["result"] == "win" and w["hit30"] and abs(w["chg_at"] - 10) < 1e-6
    assert lz["result"] == "loss" and not lz["hit30"]
    st = get(base + "/api/detections?hours=200")["stats"]
    assert st["burst"]["n"] >= 2 and st["burst"]["win"] >= 1 and st["burst"]["loss"] >= 1


def get_market_path(tmp):
    import glob
    return [p for p in glob.glob(str(tmp / "*.db")) if "market" in p][0]


def test_briefing_account_overhead_endpoints(server):
    base, tmp = server
    b = get(base + "/api/briefing")
    assert set(b) >= {"ts", "date", "d0"}
    assert get(base + "/api/account") == {}                         # 수집기가 아직 안 남김
    (tmp / "account.json").write_text(json.dumps({"ts": "2026-10-24T00:00:00+00:00", "holdings": {"items": []}}), encoding="utf-8")
    assert get(base + "/api/account")["holdings"] == {"items": []}
    assert "overhead" in get(base + "/api/symbol_extra?s=PENX")


def test_live_stream_pushes_changed_prices(server):
    """실시간 가격 밀어 주기: 수집기가 price_now·live_tick 에 쓴 가격이 1초 안에 이벤트로 온다."""
    import sqlite3, time as _t
    from datetime import datetime, timezone
    from pennybot.dashboard.server import Handler
    base, tmp = server
    Handler.LIVE_MAX_SEC = 3
    db = get_market_path(tmp)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE IF NOT EXISTS price_now (symbol TEXT PRIMARY KEY, ts TEXT, last REAL)")
    con.execute("INSERT OR REPLACE INTO price_now VALUES ('ABCD', ?, 1.23)", (datetime.now(timezone.utc).isoformat(),))
    con.commit(); con.close()
    with urllib.request.urlopen(base + "/api/live", timeout=10) as r:
        assert r.headers["Content-Type"].startswith("text/event-stream")
        line = r.readline().decode()
    assert line.startswith("data: ") and json.loads(line[6:])["ABCD"][0] == 1.23


def test_live_stream_pushes_volume_change_at_same_price(server):
    from pennybot.dashboard.server import Handler
    base, tmp = server
    Handler.LIVE_MAX_SEC = 5
    db = get_market_path(tmp)
    now = datetime.now(timezone.utc).isoformat()
    con = sqlite3.connect(db)
    con.execute("INSERT OR REPLACE INTO live_tick(symbol,ts,last,vol_60s,amt_60s,vol_180s,amt_180s) VALUES(?,?,?,?,?,?,?)",
                ("FLOW", now, 1.23, 100, 123, 300, 369))
    con.commit(); con.close()

    def next_flow(stream):
        while True:
            line = stream.readline().decode()
            if line.startswith("data: "):
                data = json.loads(line[6:])
                if "FLOW" in data:
                    return data["FLOW"]

    with urllib.request.urlopen(base + "/api/live", timeout=8) as stream:
        first = next_flow(stream)
        con = sqlite3.connect(db)
        con.execute("UPDATE live_tick SET vol_60s=120, amt_60s=147.6, vol_180s=320, amt_180s=393.6 WHERE symbol='FLOW'")
        con.commit(); con.close()
        second = next_flow(stream)
    assert first[2:4] == [100, 123]
    assert second[0] == 1.23 and second[2:4] == [120, 147.6]
    assert first[5:7] == [300, 369]
    assert second[5:7] == [320, 393.6]
