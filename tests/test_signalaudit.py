from pennybot.collector import Store
from pennybot.signalaudit import independent, report, text
from pennybot.trading import Journal


def _bar(ts, high, low, close=1.0, symbol="PEN"):
    return {"symbol": symbol, "ts": ts, "trade_date": "2026-10-21", "session": "REGULAR",
            "open": 1.0, "high": high, "low": low, "close": close, "volume": 1000.0}


def _sig(ts, symbol="PEN", price=1.0, strategy="test"):
    return {"ts": ts, "trade_date": "2026-10-21", "session": "REGULAR", "strategy": strategy,
            "symbol": symbol, "ok": 1, "reason": "x", "score": 1.0, "price": price}


def test_independent_collapses_scan_repeats():
    rows = [_sig("2026-10-21T10:00:00+00:00"), _sig("2026-10-21T10:00:05+00:00"),
            _sig("2026-10-21T10:00:10+00:00"), _sig("2026-10-21T11:00:00+00:00")]
    assert len(independent(rows, gap_minutes=30)) == 2


def test_report_uses_independent_signals_and_forward_prices(tmp_path):
    store = Store(tmp_path / "m.db")
    store.put_bars([
        _bar("2026-10-21T10:01:00+00:00", 1.06, 0.99),
        _bar("2026-10-21T10:02:00+00:00", 1.02, 0.94),
        _bar("2026-10-21T11:01:00+00:00", 1.01, 0.93),
    ])
    journal = Journal(tmp_path / "t.db")
    journal.log_signals([
        _sig("2026-10-21T10:00:00+00:00"), _sig("2026-10-21T10:00:05+00:00"),
        _sig("2026-10-21T10:00:10+00:00"), _sig("2026-10-21T11:00:00+00:00"),
    ])
    rep = report(journal.path, tmp_path / "m.db", days=3650)
    assert rep["raw"] == 4 and rep["independent"] == 2
    s = rep["groups"]["test|REGULAR"]
    assert s["n"] == 2 and s["win"] == 50.0 and s["loss"] == 50.0
    assert "신호 감사" in text(rep, 30)


def test_report_excludes_outlier_prices(tmp_path):
    store = Store(tmp_path / "m.db")
    store.put_bars([_bar("2026-10-21T12:01:00+00:00", 6.0, 0.9, symbol="BAD")])
    journal = Journal(tmp_path / "t.db")
    journal.log_signals([_sig("2026-10-21T12:00:00+00:00", symbol="BAD")])
    rep = report(journal.path, tmp_path / "m.db", days=3650)
    assert rep["independent"] == 1 and rep["suspect"] == 1 and not rep["groups"]
