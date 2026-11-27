# Next-Day Day-Market Exit Design

## Goal

Remove intraday strategy timeouts and the 08:20 KST blanket liquidation. A normal non-swing position may remain open through the current U.S. trading day, but must be sold before the following day-market session starts.

## Chosen behavior

- Keep price- and risk-driven exits unchanged: stop loss, take profit, trailing stop, negative filing, manual flatten, and kill-switch behavior.
- Add one earlier patience exit: at or after five hours from the weighted-average fill time, close a position only when it has remained genuinely flat and has no supporting flow. Use the current executable bid and weighted-average `entry_price`; current absolute return must be at most 3%, inclusive. For bars from `entry_ts` through now, `(max(high) - min(low)) / entry_price * 100` must be at most 5%, inclusive.
- Supporting flow has two independent hold conditions. First, any `burst`, `flow2`, `after_move`, `bottom_bounce`, `early20`, `steady`, or `spike_up` event for the symbol in the latest 60 minutes means hold. A flow-query error also means hold. Second, compare `now-30m < ts <= now` with `now-90m < ts <= now-30m`: require at least 15 and 30 unique minute bars respectively, recent dollar total of at least $30,000, recent average volume per observed bar at least 0.5 times baseline, and recent average `close*volume` per observed bar at least 0.5 times baseline. Both pace ratios and the dollar floor are required. A zero baseline gives an infinite ratio only when the corresponding recent value is positive; zero versus zero does not qualify.
- Fewer than 120 unique post-entry minute bars, a latest bar older than five minutes, insufficient comparison-window coverage, or any bar-query error suppresses the flat exit. At 4:59 it cannot fire; at exactly 5:00 it may fire when every condition is met.
- For every newly opened non-swing position, replace strategy-specific hold deadlines with the last sellable session before the calendar's next DAY session. Normally this is the current trade date's after-hours session.
- Start an orderly exit 30 minutes before that session ends, make the exit aggressive 15 minutes before it ends, and reserve the remaining session for existing cancel/reprice/final-slippage handling. With the normal Toss calendar these times are 19:20 ET and 19:35 ET; after-hours ends at 19:50 ET and the next DAY session begins at 20:00 ET on an ordinary weekday.
- Resolve the next DAY start from all future `SessionCalendar` session bounds, then choose the latest sellable session end before it. This makes Friday close precede Sunday DAY, and follows holidays, daylight saving time, and early closes. If calendar coverage is missing, scan `static_day()` for the next seven dates; only then fall back to the current trade date's 19:50 ET after-hours end.
- Preserve strategies marked `SWING`; their explicit multi-day deadlines remain authoritative.
- Disable `flatten_kst`, `entry_cutoff_kst`, and the `morning_flatten_et` fallback path through an explicit `next_day_exit` mode; blank strings alone are not used as the switch. Block all new non-swing exposure once the orderly-exit window begins, including initial entries, add-ons, pending entry orders, and entry repricing.
- While `next_day_exit` mode is enabled, bypass the legacy moving-reference `stale_exit_hours` rule and the timed `no_rebound_at` branch. The new five-hour flat rule is the only non-final time-based early exit; untimed dumping/decline logic remains active.
- Apply the new effective cutoff at runtime to `OPEN` and already `EXITING` non-swing trades; do not rewrite persisted historical deadlines. This is restart-safe because every tick derives the same cutoff from `trade_date` and the calendar. Cancel pending non-swing entry orders in the cutoff window. Closed trades are untouched.
- The loaded strategy class is authoritative for `SWING`. Unknown, renamed, or unavailable strategies default to non-swing for safety. The trade's persisted strategy name is never inferred from display text.

## Data flow

1. Resolve the next calendar DAY start and the latest sellable session end before it.
2. Derive `sell_from = sellable_end - 30 minutes` and `deadline = sellable_end - 15 minutes`.
3. Normalize generated non-swing plans before risk sizing and order submission.
4. Override the effective times of persisted open/exiting non-swing trades before exit evaluation, without rewriting their rows.
5. At `sell_from`, submit the normal patient exit. At `deadline`, reuse the existing aggressive forced-exit/repricing path.
6. From exactly five hours after entry until the final window, evaluate the flat-position exit. It returns `5시간 변동 없음 청산` only when both price-flat tests pass, data coverage is sufficient, no qualifying 60-minute event exists, and the recent volume+dollar-flow test fails.

## Failure handling

- Missing calendar data uses static NYSE calendar coverage and finally an ET fallback instead of leaving a position without a deadline.
- Missing quotes, rejected orders, and unfilled orders continue through the existing retry/reprice path and trigger an urgent stale-exit alert. If a position is still open when the sellable session ends, keep new entries blocked and submit at the first quote of the next session. No broker can guarantee a fill when the market has no quote; the alert must state that the pre-DAY exit failed rather than silently claiming success.
- Entry orders still pending when the orderly-exit window starts are cancelled and may not be repriced. Add-ons use the same gate.
- Manual flatten, bad-news exits, and hard stops can still exit earlier.
- A transient data gap must not be interpreted as “no volume”; insufficient bars suppress the five-hour flat exit.

## Verification

- A DAY, PRE, REGULAR, and AFTER non-swing plan receives the same last-sellable-session cutoff before the next DAY session.
- Strategy-specific 20/45/60/240-minute deadlines no longer shorten a non-swing holding.
- A SWING plan retains its original deadline.
- Initial entries, add-ons, pending orders, and entry repricing are blocked/cancelled during the 30-minute liquidation window.
- Existing OPEN/EXITING positions use the effective cutoff without changing closed trades or persisted history.
- Unknown strategies default to non-swing; known SWING strategies retain their deadlines.
- DST, Friday/weekend, holiday, early-close, stale-calendar, and already-past-cutoff fixtures resolve deterministically.
- Missing quote and unfilled-exit tests assert urgent notification, continued entry blocking, and retry at the first next-session quote.
- Five-hour tests cover 4:59/exactly 5:00, equality at the 3%/5%/$30,000/0.5 thresholds, only-volume and only-dollar failures, zero baselines, gapped/stale/insufficient bars, every qualifying event kind, flow-query failure, meaningful-flow hold, prior large excursion hold, SWING exclusion, and coexistence with both disabled legacy timers.
- Calendar tests explicitly cover DST, Friday/weekend, holiday, early close, already-past-cutoff, stale coverage, and static fallback.
