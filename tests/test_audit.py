"""청산 감사: '청산 뒤 1시간 가격'으로 조기 청산을 가려낸다."""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from pennybot.audit import report, text


def _make(tmp_path, exit_reason="수급 없는 하락 청산", after=(1.05, 1.10), status="CLOSED"):
    exit_at = datetime(2026, 10, 30, 20, 0, tzinfo=timezone.utc)
    t = sqlite3.connect(tmp_path / "trading.db")
    t.execute("""CREATE TABLE trades (symbol TEXT, trade_date TEXT, entry_price REAL, exit_price REAL,
                                     exit_ts TEXT, exit_reason TEXT, pnl_pct REAL, high_water REAL, status TEXT)""")
    t.execute("INSERT INTO trades VALUES ('FLO', '2026-10-30', 1.00, 1.00, ?, ?, -5.0, 1.05, ?)",
              (exit_at.isoformat(), exit_reason, status))
    t.commit()
    t.close()
    m = sqlite3.connect(tmp_path / "market.db")
    m.execute("""CREATE TABLE bars_1m (symbol TEXT, trade_date TEXT, session TEXT, open REAL, high REAL,
                                       low REAL, close REAL, volume REAL, ts TEXT)""")
    for minutes, high in zip((5, 30, 60, 90, 120), after):
        m.execute("INSERT INTO bars_1m VALUES ('FLO', '2026-10-30', 'AFTER', 1, ?, 1, ?, 100, ?)",
                  (high, high, (exit_at + timedelta(minutes=minutes)).isoformat()))
    m.commit()
    m.close()
    return str(tmp_path / "trading.db"), str(tmp_path / "market.db"), exit_at


def test_flags_a_rule_that_sells_before_the_price_recovers(tmp_path):
    trading, market, exit_at = _make(tmp_path)
    rows = report(trading, market, days=400, now=exit_at + timedelta(days=1))
    assert rows[0]["reason"] == "수급 없는 하락 청산"
    assert rows[0]["after1h"] == pytest.approx(10.0)      # 1시간 뒤 +10%
    assert rows[0]["early"] is True


def test_does_not_flag_exits_that_were_right(tmp_path):
    trading, market, exit_at = _make(tmp_path, exit_reason="고점 대비 −2% 익절", after=(0.98, 0.95))
    rows = report(trading, market, days=400, now=exit_at + timedelta(days=1))
    assert rows[0]["early"] is False
    assert "조기 청산 의심" not in text(rows, 400)


def test_forced_exits_are_never_flagged(tmp_path):
    trading, market, exit_at = _make(tmp_path, exit_reason="강제 청산 시각", after=(1.20, 1.30))
    rows = report(trading, market, days=400, now=exit_at + timedelta(days=1))
    assert rows[0]["early"] is False


def test_trailing_stop_details_are_grouped_together(tmp_path):
    trading, market, exit_at = _make(tmp_path, exit_reason="익절(분할) + 트레일링 스톱 (고점 $0.5419 −5%)")
    rows = report(trading, market, days=400, now=exit_at + timedelta(days=1))
    assert rows[0]["reason"] == "익절(분할) + 트레일링 스톱"
