from datetime import datetime, timedelta, timezone

from pennybot.services.ranker import rank_candidates


NOW = datetime(2026, 11, 20, 23, 30, tzinfo=timezone.utc)


def candidate(symbol, turnover, dollars, age=0, price=1.0, close=1.0):
    return {"symbol": symbol, "turnover_3m": turnover, "dollar_3m": dollars,
            "metric_ts": (NOW - timedelta(seconds=age)).isoformat(),
            "price": price, "price_ts": NOW.isoformat(), "prev_close": close,
            "prev_close_date": "2026-11-19"}


def test_priority_four_requires_both_recent_turnover_and_dollar_volume():
    rows = [candidate("BALANCED", 10, 100000), candidate("HIGH_DOLLAR", 0.1, 2000000),
            candidate("HIGH_TURN", 20, 1000), candidate("SECOND", 8, 90000),
            candidate("THIRD", 7, 80000), candidate("FOURTH", 6, 70000)]
    ranked = rank_candidates(rows, NOW, top=4)
    assert [r["symbol"] for r in ranked] == ["BALANCED", "SECOND", "THIRD", "FOURTH"]


def test_stale_or_invalid_metrics_cannot_enter_priority_four():
    rows = [candidate("STALE", 50, 500000, age=45), candidate("GOOD", 5, 50000),
            candidate("BAD", -1, 100000)]
    assert [r["symbol"] for r in rank_candidates(rows, NOW, top=4)] == ["GOOD"]


def test_change_percent_uses_explicit_previous_close_and_same_price_snapshot():
    ranked = rank_candidates([candidate("A", 5, 50000, price=1.2, close=1.0)], NOW)
    assert round(ranked[0]["change_pct"], 4) == 20.0
    assert ranked[0]["price_ts"] == NOW.isoformat()
    assert ranked[0]["prev_close_date"] == "2026-11-19"
