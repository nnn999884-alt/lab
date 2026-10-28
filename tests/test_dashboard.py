import json
import threading
import urllib.error
import urllib.request

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
    assert set(st) == {"runner_overnight", "dilution_catalyst", "day_ignition", "day_dip", "regular_momentum",
                       "scalp", "steady_trend"}
    assert st["day_ignition"]["trades"] == 12 and st["day_dip"]["config_enabled"] is False
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
        assert b"Pennybot" in r.read()


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


def test_symbol_extra(server):
    base, _ = server
    x = get(base + "/api/symbol_extra?s=penx")
    assert x["symbol"] == "PENX"
    assert set(x) >= {"shares", "events", "vsignals"}               # 데모 DB 에 없는 항목(일봉 점수 등)은 빠질 뿐 오류 없음
    assert isinstance(x["events"], list) and isinstance(x["vsignals"], list)
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
    assert set(b) >= {"date", "picks", "night", "halts", "filings"}
    assert get(base + "/api/account") == {}                         # 수집기가 아직 안 남김
    (tmp / "account.json").write_text(json.dumps({"ts": "2026-10-24T00:00:00+00:00", "holdings": {"items": []}}), encoding="utf-8")
    assert get(base + "/api/account")["holdings"] == {"items": []}
    assert "overhead" in get(base + "/api/symbol_extra?s=PENX")
