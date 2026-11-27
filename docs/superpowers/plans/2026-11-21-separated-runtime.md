# Separate Runtime Implementation Plan

> **For agentic workers:** Use `superpowers:subagent-driven-development` task by task. Verify each phase before switching live traffic.

**Goal:** Run market data, scans, corporate data, history, ranking, signals, trading, notifications, dashboard, and supervision as independently restartable services.

**Architecture:** Services exchange versioned durable events and read-only published snapshots. Each service owns its database writes. A supervisor restarts failed processes and reports per-service freshness and lag.

**Tech Stack:** Python 3.12, SQLite WAL, existing LS/Toss adapters, TradingView scanner, HTTP dashboard, pytest.

**Spec:** `docs/superpowers/specs/2026-11-21-separated-runtime-design.md`

---

## Phase 1: Shared event contract and health

**Files:** Create `pennybot/runtime/events.py`, `pennybot/runtime/queue.py`, `pennybot/runtime/health.py`; add `tests/test_runtime_events.py`, `tests/test_runtime_queue.py`.

- [ ] Test event schema validation, monotonic source sequence, source timestamps, replay from checkpoint, and duplicate event IDs.
- [ ] Test urgent spool durability and bounded SQLite lock retry; a failed write must be reported and recoverable.
- [ ] Implement versioned event envelope, durable queue, consumer acknowledgements, and per-service heartbeat.
- [ ] Run the new tests and the existing collector/trading/notification tests. Deploy only the passive queue and health status.

## Phase 2: Market and corporate sources

**Files:** Create `pennybot/services/market_feed.py`, `pennybot/services/market_scan.py`, `pennybot/services/filing_feed.py`, `pennybot/services/news_feed.py`, `pennybot/services/market_aux.py`, `pennybot/services/history_worker.py`; modify `pennybot/__main__.py`, `pennybot/ls_api.py`, `pennybot/tvscan.py`, `pennybot/collector/runner.py`; add service tests.

- [ ] Test that a blocked SEC request does not delay market snapshots and that a blocked history request does not delay either source.
- [ ] Test LS silent subscription and reconnect fallback, price source precedence, and previous-close reference date.
- [ ] Test scanner dollar/turnover windows and source timestamps independently of prices.
- [ ] Move each source into its own process with one database writer and event publisher; publish shadow output alongside the existing collector.
- [ ] Compare shadow and live prices, change percent, scan values, and corporate events for a full trading session before switching reads.

## Phase 3: Ranker and signal engine

**Files:** Create `pennybot/services/ranker.py`, `pennybot/services/signal_engine.py`; extract ranking/signal methods from `pennybot/collector/runner.py`; add ranking and signal parity tests.

- [ ] Test priority-four ordering using validated one-minute/three-minute turnover and dollar volume, including missing or stale data.
- [ ] Test rank changes from incoming events and versioned dashboard projection.
- [ ] Test alert keys, wave numbers, cooldown start after delivery, replay, and duplicate suppression.
- [ ] Run both services in shadow mode. Compare watch order and alert keys against the live collector; correct mismatches before cutover.

## Phase 4: Notification and trade ownership

**Files:** Create `pennybot/services/notifier.py`, `pennybot/services/trade_engine.py`; modify `pennybot/trading/notify.py`, `pennybot/trading/trader.py`, `pennybot/trading/journal.py`; add durable outbox and trade reconciliation tests.

- [ ] Test Telegram/webhook retry, 429 delay, partial destination completion, and restart recovery.
- [ ] Test exactly one active order owner, order/fill reconciliation, and idempotent fill/close/risk transitions.
- [ ] Move notification delivery to a separate outbox worker. Keep its receipts independent of signal evaluation.
- [ ] Start the new trade engine in read-only shadow mode; compare decisions with the existing trader before transferring order ownership.

## Phase 5: Dashboard and supervisor cutover

**Files:** Create `pennybot/services/supervisor.py`; modify `pennybot/dashboard/server.py`, `pennybot/dashboard/static/index.html`, launch scripts, and dashboard tests.

- [ ] Test that the dashboard reads only published projections and displays source/freshness for price, change percent, turnover, and ranks.
- [ ] Test separate process restart, backoff, lag indicators, and shutdown without losing acknowledged events.
- [ ] Switch one consumer at a time with a rollback flag; leave the old collector live until every parity and freshness gate passes.
- [ ] Verify the complete live path through API, database snapshots, Telegram dry-run, and process health. Retire the old collector after cutover.

## Live gates

For each phase, require passing targeted tests, a clean restart, bounded queue age, fresh market data, no added alert/order duplicates, and documented rollback. Never connect a shadow trade engine to order placement.
