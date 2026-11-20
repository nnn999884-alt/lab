# Live Pipeline Reliability Redesign

Date: 2026-11-20
Status: Approved

## Goal

Eliminate avoidable alert latency and message loss while the current bot remains live, then move the collector toward independently recoverable ingestion, detection, and delivery stages. The redesign must preserve existing signal semantics and message formatting unless a reliability requirement explicitly changes them.

## Success criteria

- Alert, fill, close, and risk jobs are never queued behind candle seeding or other remote-data enrichment work.
- A transient SQLite lock, HTTP failure, Telegram 429, or process restart does not silently consume an alert.
- Exact-event deduplication becomes effective after an atomic pending claim; the user-visible cooldown starts only after at least one configured destination has completely delivered the message.
- The five-second collector tick does not execute long candle sync, filing-detail backfill, or full raw-price aggregation inline.
- REST polling remains active for a symbol until the stream has confirmed a usable subscription or delivered a fresh frame for it.
- Runtime status exposes queue depth, oldest-job age, retries, failures, and stream readiness.
- Existing alert calculations and live trading behavior remain compatible, with regression tests covering failure and reconnect paths.

## Chosen approach

Use a staged reliability redesign rather than a one-shot rewrite.

1. Stabilize the current process by separating urgent and enrichment work, bounding queues, and removing raw-table scans.
2. Add a durable SQLite event outbox and atomic claims so notification work survives retries and restarts.
3. Move slow sync/backfill jobs behind bounded background schedulers with checkpoints.
4. Make stream readiness explicit and keep polling as a safety net through reconnects.
5. Run compatibility tests and deploy with a staged restart.

A full replacement process topology is intentionally deferred until the new boundaries have production metrics. The interfaces introduced here make later extraction into separate processes possible without changing signal code again.

## Architecture

### 1. Work lanes

The collector will use three lanes:

- `control`: virtual fill/close, risk actions, and watchdog events. A dedicated worker executes only local bounded work; it performs no remote lookup.
- `signal`: alert creation, approach, burst, absorb, and signal-evaluation continuations that use already-available data. A small bounded worker pool drains this lane. Any missing remote data is requested through enrichment and resumes as a new signal job rather than blocking a signal worker.
- `enrichment`: candle seeding, historical candle synchronization, filing details, and other remote lookups. One or more bounded workers drain this lane. Duplicate seed jobs for a symbol coalesce.

Websocket callbacks perform no SQLite or network calls and do not set fired/fill/close markers. A callback serializes control and signal jobs to a dedicated append-only local spool, then publishes their offsets to an in-memory lane. Records are length-prefixed JSON with version, a UUID `job_id` assigned before either persistence path, monotonically increasing spool offset, and CRC32. The spool append is flushed and fsynced before durable acceptance; writes are grouped for at most 10 ms so callbacks wait on one group-commit event rather than performing independent fsyncs. A write that misses the bounded group-commit deadline falls back to an unbounded emergency in-memory queue, increments visible `spool_failures`/`unpersisted_jobs`, and raises a local critical log; it is never reported as durably accepted.

A spool importer writes `urgent_jobs(job_id UNIQUE, spool_offset UNIQUE, priority, kind, payload, state, attempt, next_attempt_at, lease_owner, lease_until, last_error, completed_at)` using states `pending`, `leased`, `retry`, `done`, and `failed`. The emergency queue carries the same `job_id`; import and execution insert/claim by `job_id`, so a late successful fsync after a group-commit timeout cannot execute a second copy. Every control side effect and derived source event also records that `job_id` as an idempotency key. The importer advances the checkpoint only after every offset through that point exists in `urgent_jobs`, independent of whether processing has begun. Workers atomically lease rows; expired leases return to `retry`, so a stop after import or during processing is recoverable. Rotation occurs only after every record in a segment is imported, the checkpoint file is atomically replaced and directory metadata flushed where supported. Startup ignores/truncates only an incomplete final record; a checksum failure before the tail quarantines the segment and raises critical health instead of skipping data.

Control workers always outrank signal workers. Signal work has strict local execution deadlines; remote calls are forbidden in both lanes and are delegated to enrichment. Enrichment jobs may be coalesced or dropped when stale; control/signal jobs may not be silently dropped.

Job age limits are type-specific. Seed and scan jobs become stale; fills, closes, warnings, and already-created alert events remain recoverable until handled.

### 2. Durable alert outbox and atomic claims

Add an `alert_outbox` table containing:

- stable event key and category;
- message payload and optional buttons;
- creation time, next-attempt time, attempt count, and last error;
- delivery state and delivered time.

Creating an alert and claiming its exact deduplication key happens in one source-database transaction. `notification_events.event_key UNIQUE` in the collector or trading source DB is the authoritative claim; a unique event key prevents the main loop and realtime worker from creating duplicates. The claim has a lease while the event is pending; lease expiry permits recovery by another worker, not creation of another row. Network delivery occurs outside the transaction after relay to the shared outbox.

Pending-claim suppression and delivered cooldown are separate:

- An exact event key remains unique permanently, including after terminal failure.
- While any destination is pending/retrying, its pending claim suppresses concurrent creation of that same event.
- The broader symbol/alert-type cooldown starts only when every chunk for at least one configured destination is delivered.
- If one destination completes and another permanently fails, the cooldown starts, while the failed destination remains visible and manually retryable.
- If all configured destinations permanently fail, no broader cooldown starts. The exact failed event remains deduplicated, but a later event with a new event key may be generated and attempted.

Retryable failures remain pending with exponential backoff and jitter; Telegram `Retry-After` is honored. Permanent authentication/configuration failures are retained as failed and surfaced in status.

Signal producers use two keys. `reservation_key` coalesces in-flight work and has a short renewable lease; it is released on filter rejection or exhausted processing failure. `event_key` identifies an emitted logical event permanently. `cooldown_key` enforces the existing repeat interval only after durable delivery. All paths call the same canonical key builder:

| Family | Canonical event key | Reservation/cooldown scope |
|---|---|---|
| virtual fill/close/risk | trade date + transition kind + order/trade id + transition version | order/trade id + transition kind |
| threshold/fast/slow signal | trade date + session + normalized kind + symbol + threshold/wave id | trade date + normalized kind + symbol |
| approach/reacceleration | trade date + normalized kind + symbol + wave number + stage | trade date + normalized kind + symbol + stage |
| absorb/burst/window event | trade date + session + normalized kind + symbol + provider event time floored to 10 seconds | trade date + normalized kind + symbol |
| operational warning | warning kind + affected process + incident id | warning kind + affected process |

Provider event time is used when present; otherwise collection time floored to the same boundary is used. Main-loop and realtime aliases map to one normalized kind. Existing configured cooldown durations remain authoritative. The old pre-queue `_rt_fired`, `_slow_sent`, `_fast_sent`, `_rb_sent`, and `_ab_fired` writes are replaced by reservations; they are never treated as delivered cooldowns.

Wave numbers and transition versions are source-database state, never process-local counters. The source DB atomically increments `(trade_date, symbol, normalized_kind)` in `event_sequences` when a new wave/transition is confirmed and stores that assigned value with the reservation/source event in the same transaction. Startup restores the latest sequence. If a wave is derived from already-persisted extrema, its deterministic extrema IDs are stored and reused rather than incrementing again.

The existing `Notifier.send` API remains usable. It returns whether the event was durably accepted and routes enabled live notifications through the outbox dispatcher. Logging-only mode remains immediate.

Delivery is explicitly **at least once**, because a process can stop after the remote service accepts a request but before the local success commit. `alert_delivery` records destination and chunk index with independent state, attempt count, next attempt, and remote response metadata. A retry resumes only incomplete chunks for that destination. Duplicate delivery is therefore limited to the one in-flight chunk around a crash; exactly-once delivery is not claimed. Logical-event success and cooldown follow the destination policy above.

All processes share `state/notify.db`. Multiple notifier instances may dispatch, but every destination/chunk row is protected by an atomic lease (`lease_owner`, `lease_until`), so only one dispatcher owns an attempt at a time. There is no process-local-only notification queue.

Cross-database state changes use a bidirectional source-event relay. Collector signal creation writes `notification_events` in `market.db` in the same transaction as its reservation/source state. Trader fill, close, and risk transitions write the same ledger shape in the trading journal transaction. A forward relay copies source events idempotently into `notify.db` by global event key and records relay acknowledgement back in the source database. A reverse receipt relay copies destination completion/terminal-failure state back to the authoritative source row by event key. The source DB activates `delivered_cooldowns` idempotently only when the receipt says at least one configured destination completed every chunk; all-destination failure releases the broader reservation without activating cooldown. Startup and periodic reconciliation scan both unacknowledged source events and unapplied delivery receipts. For legacy trade/order rows created before the ledger exists, reconciliation derives transition event keys from stable trade/order IDs and status timestamps. Thus a process stop in either direction cannot lose the notification or falsely activate cooldown.

### 3. Database contention policy

All new state transitions use short transactions and a shared bounded retry helper for SQLite `busy`/`locked` errors. Remote requests never run inside a database transaction.

Long background operations checkpoint in small batches. Existing raw event tables remain append-oriented; live queries use compact summary tables and indexed lookups.

### 4. Compact live summaries

Extend compact price state so after-hours mover detection does not calculate `MAX(ts)` across the full `prices` table. `session_price_latest` has primary key `(trade_date, session, symbol)` and columns `event_ts`, `collected_ts`, `source_rank`, `last`, and the fields needed by mover detection. `event_ts` is provider price time when available, otherwise `collected_ts`; neither stored ordering field is nullable. `put_prices` updates only when `(excluded.event_ts, excluded.collected_ts, excluded.source_rank)` is lexicographically greater than the stored tuple. Exact ties keep the existing row. Therefore out-of-order input, equal provider times, and historical backfill cannot overwrite a newer live collection. `day_high` continues to store the daily maximum.

The AFTER mover contract compares the same trade date's latest AFTER row with that date's REGULAR close from `session_price_latest`. To preserve current behavior, a symbol without that same-date REGULAR row is skipped. Queries read only compact rows and preserve the existing threshold/dedup semantics.

Both summaries are updated transactionally with `put_prices`. Upgrade backfill is incremental and resumable: current-day rows are populated in bounded batches, then recent retained days are filled during closed/off-peak sessions. Live correctness does not wait for historical backfill.

### 5. Slow job scheduler

`sync_candles` and `backfill_details` leave the main tick. The tick only schedules idempotent jobs in `background_jobs`. Each row has a unique job key, type, payload, state (`pending`, `leased`, `retry`, `done`, `failed`), cursor, attempt count, `next_attempt_at`, `lease_owner`, `lease_until`, and last error. Each worker owns an independent SQLite connection.

- Workers atomically lease one due job. An expired lease is recoverable after restart; a unique job key prevents duplicate active runs.
- Candle sync processes at most 10 symbols or 20 API pages per lease and saves its symbol/page cursor before yielding.
- Filing detail backfill processes and commits at most 10 filings per lease and saves its accession cursor, preventing watchdog restart loops.
- Session-transition work that depends on completion observes job state rather than blocking the tick.
- Queue capacity and per-host concurrency prevent remote calls from starving realtime processing.
- API calls use a priority-aware budget shared by each provider. Realtime REST fallback and live price polling reserve the highest-priority tokens. Enrichment uses a separate low-priority limiter and pauses automatically while fallback is active, main-tick age exceeds 10 seconds, or the reserved live budget is below its floor. Enrichment cannot borrow reserved live tokens.

### 6. Stream readiness and polling fallback

Connection state and subscription readiness are separate. Toss and LS adapters expose one thread-safe contract per channel: `desired(channel)`, `declared(channel)`, `ready(channel)`, `rejected(channel)`, and `last_frame(channel, symbol)`. Returned collections are snapshots guarded by each adapter's lock.

- A reconnect clears declared/ready/last-frame state while preserving desired symbols.
- Subscription declarations are deterministic and remain pending until a subscription response confirms them or a fresh trade frame proves readiness.
- Toss uses its subscription response or first channel frame as confirmation. Because LS does not provide equivalent positive acknowledgement, LS declaration alone is never readiness; the first trade/orderbook frame confirms that symbol/channel.
- Rejections remove only rejected symbols from the declaration and trigger a new declaration.
- `_rt_covers(symbol)` requires a connected stream, ready status for the trade channel, and a per-symbol trade frame no older than 30 seconds (configurable).
- REST polling continues during declaration, reconnect, stale-frame, and partial-subscription gaps.

Daily bar/session flushes first serialize the completed-date snapshot into the durable spool/background job ledger. Markers advance only after durable bar persistence succeeds. Startup replays unfinished snapshot jobs, including snapshots captured before a date roll, so LS in-memory state cannot disappear between date detection and asynchronous flush.

### 7. Watchdog shutdown

The watchdog writes its restart warning into the durable outbox before terminating. It waits only for durable commit, not network delivery. The restarted process dispatches the warning, so daemon-thread shutdown cannot lose it.

### 8. Observability

`collector_status.json` gains:

- control/signal/enrichment queue depth and oldest age, plus spool checkpoint lag, failures, and unpersisted count;
- pending/retrying/failed outbox counts and oldest pending age;
- SQLite retry and permanent-failure counters;
- desired, declared, ready, stale, and rejected stream symbol counts;
- background job state, cursor, last success, and last error.

The dashboard renders these as health indicators without changing its existing trading controls.

## Failure behavior

- SQLite lock: retry a bounded number of times; retain the event/job and expose failure if exhausted.
- Telegram/webhook timeout or 5xx: retry with capped exponential backoff.
- Telegram 429: honor server retry delay.
- Telegram 4xx authentication/configuration error: stop rapid retry, retain failure, and surface prominently.
- Queue pressure: coalesce stale enrichment jobs; persist urgent overflow.
- Stream reconnect or stale frame: treat realtime coverage as unavailable and use REST polling.
- Process restart: pending outbox and checkpointed slow jobs resume.

## Compatibility and rollout

Schema changes use `CREATE TABLE IF NOT EXISTS` and additive indexes. Existing message text, alert toggles, and signal thresholds remain unchanged.

Rollout is staged, with a target collector restart gap under 90 seconds:

1. Run schema migration, `PRAGMA integrity_check`, disk-space checks, configuration validation, and a SQLite online backup before changing the live process.
2. Run targeted regression and concurrency tests against temporary databases.
3. Start a preflight dispatcher against a temporary outbox and verify one dry-run delivery lifecycle without contacting remote destinations.
4. Restart the collector once. Within two minutes it must report tick cadence below 10 seconds, connected or explicit REST fallback, no unpersisted control jobs, a running outbox dispatcher, and bounded queue ages.
5. If the health gate fails, stop new producers, let the compatibility outbox dispatcher drain already-accepted events, then restore the prior collector code/config. Additive tables remain readable and no pending event is abandoned.
6. Keep REST fallback active while comparing realtime and polling paths.
7. Enable incremental historical summary backfill only after live health is stable.

Rollback does not revert the outbox schema/dispatcher compatibility layer while pending rows exist. It disables the new producer/scheduler paths, drains or exports pending rows, then returns to the prior synchronous producer interfaces. This ensures old code is never started while accepted messages have no compatible dispatcher.

## Test strategy

Tests will cover:

- urgent work overtaking a blocked seed job;
- control fill/close work overtaking blocked signal and enrichment jobs;
- spool replay, checkpointing, rotation, write-failure accounting, and restart recovery;
- late spool commit versus emergency-queue deduplication and control-side-effect idempotency;
- bounded/coalesced enrichment queues and stale-job handling;
- atomic duplicate claims from concurrent producers;
- canonical key equivalence between main/realtime aliases, reservation release, and delivered-only cooldown activation;
- persisted wave/transition sequencing across process restarts and bidirectional delivery-receipt reconciliation;
- lock failure before claim, after claim, and during delivery-state update;
- notifier timeout, 5xx, 429 with `Retry-After`, permanent 4xx, destination/chunk partial success, cross-process lease exclusion, source-relay reconciliation, crash-window duplicate, restart recovery, and queue pressure;
- no cooldown activation for an event that was not durably accepted;
- after-hours mover queries using compact summaries rather than raw-price aggregation;
- current-day summary migration/backfill and resumed batches;
- Toss and LS reconnect gaps, partial subscription confirmation, rejected symbols, channel-specific readiness, thread-safe snapshots, and stale frames;
- session flush retry and watchdog warning recovery;
- live REST budget priority and enrichment starvation prevention while fallback is active;
- existing collector, stream, alert formatting, virtual trading, and dashboard regressions.

## Non-goals for this phase

- Changing signal thresholds, scoring, or alert wording.
- Replacing SQLite with a network database.
- Splitting the bot into separately deployed services before the new interfaces have live metrics.
- Reworking unrelated dashboard or trading-strategy changes already present in the worktree.
