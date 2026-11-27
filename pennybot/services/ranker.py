"""Pure ranking projection for recent, validated market-flow candidates."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Iterable


def _time(value):
    try:
        ts = datetime.fromisoformat(value)
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _positive(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def rank_candidates(candidates: Iterable[dict], now: datetime, *, top: int = 4,
                    max_metric_age_seconds: float = 30.0) -> list[dict]:
    """Balance recent 3-minute turnover and dollar flow by their percentile ranks.

    Missing, invalid, future-dated, or stale metrics do not qualify. Price freshness is
    separate: a stale price can retain a flow rank but has no displayed change percent.
    """
    valid = []
    for row in candidates:
        ts = _time(row.get("metric_ts"))
        turn = _positive(row.get("turnover_3m"))
        dollars = _positive(row.get("dollar_3m"))
        if not row.get("symbol") or ts is None or turn is None or dollars is None:
            continue
        age = (now - ts).total_seconds()
        if age < 0 or age > max_metric_age_seconds:
            continue
        valid.append((row, turn, dollars))
    if not valid:
        return []

    turns = sorted(v[1] for v in valid)
    amounts = sorted(v[2] for v in valid)
    from bisect import bisect_right

    result = []
    for row, turn, dollars in valid:
        turn_rank = bisect_right(turns, turn) / len(valid)
        dollar_rank = bisect_right(amounts, dollars) / len(valid)
        out = dict(row)
        out["flow_score"] = min(turn_rank, dollar_rank)
        out["flow_tiebreak"] = turn_rank + dollar_rank
        price = _positive(row.get("price"))
        close = _positive(row.get("prev_close"))
        price_ts = _time(row.get("price_ts"))
        fresh_price = price_ts is not None and 0 <= (now - price_ts).total_seconds() <= max_metric_age_seconds
        out["change_pct"] = (price / close - 1) * 100 if fresh_price and price and close else None
        result.append(out)
    result.sort(key=lambda r: (-r["flow_score"], -r["flow_tiebreak"], r["symbol"]))
    return result[:max(0, top)]
