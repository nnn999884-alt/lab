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
    assert set(st) == {"runner_overnight", "dilution_catalyst", "day_ignition", "day_dip"}
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
    with urllib.request.urlopen(base + "/") as r:
        assert b"Pennybot" in r.read()


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
