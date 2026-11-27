# Separate Runtime Design

Date: 2026-11-21
Status: Design revision requested by user
Supersedes: The single-process work-lane topology in `2026-11-20-live-pipeline-reliability-design.md`. Its durability, deduplication, source-readiness, and compatibility requirements still apply.

## Goal

Each independent responsibility runs in its own supervised process. A slow SEC request, a blocked database write, a Telegram retry, or a dashboard request must not delay prices, rankings, alerts, or order management.

## Process boundaries

| Process | Sole responsibility | Input | Output it owns |
| --- | --- | --- | --- |
| `market-feed` | LS/Toss trades, quotes, REST fallback, and session clocks | Broker streams and REST | `market.db` live price/quote tables and durable price events |
| `market-scan` | TradingView scans, volume, turnover, and watch candidates | TV scan and normalized market prices | `scan.db` snapshots and candidate events |
| `filing-feed` | SEC filings and filing-derived dilution facts | SEC HTTP | `filings.db` and filing events |
| `news-feed` | Company news | News HTTP | `news.db` and news events |
| `market-aux` | Short volume, borrow, SSR, and halts | External HTTP | `market_aux.db` and auxiliary events |
| `history-worker` | Candles, daily bars, backfills, model training | Bounded durable jobs | Historical/model stores and completion events |
| `ranker` | Watch order, priority four, and dashboard candidate projection | Price, scan, corporate snapshots | `rank.db` with timestamped rank versions |
| `signal-engine` | Thresholds, waves, deduplication, and alert creation | Durable price/rank/enrichment events | `signals.db` and notification outbox entries |
| `trade-engine` | Entry/exit decisions, broker orders, fills, risk, and position state | Price and signal events | `trading.db` and trade notification events |
| `notifier` | Telegram/webhook delivery and retry | Durable outbox | `notify.db` delivery receipts |
| `dashboard` | Read-only views, live stream, and controls | Published snapshots and health | HTTP responses; no collector work |
| `supervisor` | Start, restart, heartbeat, and incident reporting | Process health | Runtime status and bounded restarts |

The broker adapter is used by `market-feed` for market data and by `trade-engine` for orders. Neither process calls methods on the other. Any service can be restarted without restarting its peers. A process owns writes to its domain database; cross-process data moves through durable events or read-only snapshots, never through shared Python objects.

## Event contract

Every event includes `event_id`, `kind`, `symbol`, `trade_date`, `session`, `source`, provider event time, collection time, sequence, schema version, and payload. Prices include raw price, previous-close reference, and reference date; change percent is derived from that explicit reference. Volume/turnover includes window start/end and share-count source. Consumers checkpoint by event ID/sequence. Restart replays from the last acknowledged event. Coalescing is permitted for superseded price snapshots; fills, closes, alerts, and risk events remain durable until acknowledged.

The shared queue uses short SQLite WAL transactions with busy retry and a bounded spool for urgent events. It is transport, not a domain database. Each process writes its own tables and never holds a transaction during HTTP calls. Schema versions allow an old and a new consumer to run side by side during migration.

## Freshness and precedence

Each published price has a provider timestamp, collection timestamp, and source. A newer provider event wins. If the provider has no event timestamp, collection time is used and the source is shown as such. The dashboard receives price and derived change percent from the same versioned snapshot. It shows source/freshness when updates stop rather than repeatedly restamping an unchanged stale price. LS websocket readiness requires a confirmed frame; REST stays active through disconnects and silent subscriptions. The priority four are ranked from validated recent turnover and dollar volume; their price freshness is checked separately from their ranking freshness.

## Failure isolation

- `filing-feed`, `news-feed`, `market-aux`, and `history-worker` have their own HTTP budgets and cannot occupy market-feed or trade-engine workers.
- Notification failures stay in `notify.db`; they do not block signal generation or trading.
- Dashboard reads bounded materialized snapshots. Slow clients cannot block writers.
- A failed process is restarted by the supervisor with backoff. Its queue lag and last successful event time are visible independently.
- Queue backlog never changes the meaning of a trade or alert. Stale scan/rank work may be replaced; trade state transitions may not.

## Migration

1. Add event envelopes, queue, heartbeat, and side-by-side comparison without replacing the live paths.
2. Extract market-feed, filing-feed, news-feed, and market-aux first; compare their snapshots against the current collector for a full session.
3. Extract market-scan, ranker, and signal-engine; compare watch order, priority four, and alert event keys. Keep notifications in dry-run until parity is verified.
4. Move notification delivery and trading to their owned processes with durable source events and receipts. Trading remains in the existing mode until order/fill reconciliation passes.
5. Switch the dashboard to published projections and enable supervisor-controlled process restarts. Retire the old collector only after live freshness, queue lag, and event parity gates pass.

The existing database and dashboard remain available during migration. Every cutover has a rollback switch. No broker order is generated by a shadow process.

## Acceptance

- Blocking SEC or Telegram for two minutes does not increase market-price, watch-rank, trade-risk, or dashboard freshness lag beyond their configured budgets.
- Killing each process separately does not kill its peers; after restart, it resumes from its durable checkpoint without missing or multiplying fills, closes, or alerts.
- The price and displayed change percent share one snapshot version and an explicit previous-close date.
- Priority-four ordering changes when validated turnover and dollar volume change, with source timestamps visible.
- Every process reports last input, last output, queue depth, oldest pending age, and last error.
