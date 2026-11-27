from datetime import datetime, timedelta, timezone

from pennybot.services.market_scan import FlowWindows


T0 = datetime(2026, 11, 20, 20, 0, tzinfo=timezone.utc)


def test_three_minute_flow_uses_cumulative_differences_and_share_count():
    flow = FlowWindows()
    assert flow.observe("ABC", T0, "AFTER", volume=1000, dollars=2000, shares=10000) is None
    row = flow.observe("ABC", T0 + timedelta(minutes=3), "AFTER", volume=1400, dollars=3200, shares=10000)
    assert row["volume_3m"] == 400
    assert row["dollar_3m"] == 1200
    assert row["turnover_3m"] == 4.0
    assert row["metric_ts"] == (T0 + timedelta(minutes=3)).isoformat()


def test_session_rollover_or_counter_reset_discards_old_window():
    flow = FlowWindows()
    flow.observe("ABC", T0, "REGULAR", 1000, 10000, 10000)
    assert flow.observe("ABC", T0 + timedelta(minutes=3), "AFTER", 20, 200, 10000) is None
    assert flow.observe("ABC", T0 + timedelta(minutes=6), "AFTER", 10, 100, 10000) is None
    row = flow.observe("ABC", T0 + timedelta(minutes=9), "AFTER", 30, 300, 10000)
    assert row["volume_3m"] == 20


def test_missing_or_invalid_share_count_cannot_publish_turnover():
    flow = FlowWindows()
    flow.observe("ABC", T0, "REGULAR", 100, 1000, None)
    assert flow.observe("ABC", T0 + timedelta(minutes=3), "REGULAR", 200, 2000, None) is None


def test_new_trade_date_resets_even_when_session_and_counters_increase():
    flow = FlowWindows()
    flow.observe("ABC", T0, "AFTER", 100, 1000, 10000, trade_date="2026-11-20")
    assert flow.observe("ABC", T0 + timedelta(days=1, minutes=3), "AFTER", 500, 5000, 10000,
                        trade_date="2026-11-21") is None
