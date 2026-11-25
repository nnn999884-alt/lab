from datetime import date, datetime, timezone

import pytest

from pennybot2 import config
from pennybot2.sessions import AFTER, CLOSED, DAY, ET, PRE, REGULAR, classify, early_close, nyse_holidays
from pennybot2.store import Store


def et(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=ET)


def test_sessions():
    assert classify(et(2026, 11, 25, 10, 0)) == (date(2026, 11, 25), REGULAR)
    assert classify(et(2026, 11, 25, 5, 0)) == (date(2026, 11, 25), PRE)
    assert classify(et(2026, 11, 25, 17, 0)) == (date(2026, 11, 25), AFTER)
    assert classify(et(2026, 11, 24, 21, 0)) == (date(2026, 11, 25), DAY)   # 전날 밤 = 다음 영업일


def test_closed_and_holidays():
    assert classify(et(2026, 11, 28, 10, 0))[1] == CLOSED           # 토요일
    assert date(2026, 11, 26) in nyse_holidays(2026)                 # 추수감사절
    assert classify(et(2026, 11, 26, 10, 0))[1] == CLOSED
    assert early_close(date(2026, 11, 27))
    assert classify(et(2026, 11, 27, 14, 0)) == (date(2026, 11, 27), AFTER)


def test_naive_rejected():
    with pytest.raises(ValueError):
        classify(datetime(2026, 11, 25, 10, 0))


def test_store_idempotent():
    s = Store(":memory:")
    row = ("ABC", "2026-11-25T15:00:00Z", "2026-11-25", REGULAR, 1, 2, 0.9, 1.5, 100)
    s.put_bars([row]); s.put_bars([row])
    assert len(s.bars("ABC")) == 1


def test_live_needs_both(tmp_path, monkeypatch):
    monkeypatch.delenv("PENNYBOT_LIVE_CONFIRM", raising=False)
    y = tmp_path / "c.yaml"; y.write_text("mode: live\n", encoding="utf-8")
    cfg = config.load(y, tmp_path / ".env")
    assert cfg.mode == "live" and not cfg.live
    monkeypatch.setenv("PENNYBOT_LIVE_CONFIRM", "YES")
    assert cfg.live
