# Next-Day Day-Market Exit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let normal positions run until the last sellable session before the next U.S. DAY session while retaining all price/risk exits and preventing new exposure during liquidation.

**Architecture:** Add one calendar-aware policy helper that returns the orderly-exit and aggressive-deadline timestamps. Apply it centrally to non-swing plans and open trades in `Trader`, instead of editing every strategy's local timeout. Add a dedicated configuration switch so the old KST/09:00 ET blanket flatten path is bypassed without changing SWING strategies.

**Tech Stack:** Python 3.11+, `datetime`/`zoneinfo`, existing `SessionCalendar`, SQLite journal, pytest.

---

### Task 1: Calendar-aware exit policy

**Files:**
- Modify: `pennybot/trading/strategies.py`
- Test: `tests/test_trading.py`

- [ ] **Step 1: Write failing calendar-policy tests**

Add tests that build calendar fixtures and assert a helper returns `(after_end - 30m, after_end - 15m)` for a normal day, a Friday whose next DAY starts Sunday, an early close, and missing API coverage using `static_day()` fallback.

```python
sell_from, deadline = next_day_exit_window(now, cal, trade_date)
assert sell_from == after_end - timedelta(minutes=30)
assert deadline == after_end - timedelta(minutes=15)
assert deadline < next_day_start
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `pytest -q tests/test_trading.py -k "next_day_exit_window"`

Expected: FAIL because `next_day_exit_window` does not exist.

- [ ] **Step 3: Implement the focused helper**

In `pennybot/trading/strategies.py`, add `next_day_exit_window(ref, cal, trade_date)`. It must:

1. refresh/inspect calendar days;
2. find the first DAY start strictly after `ref` that belongs to a later market cycle;
3. find the latest PRE/REGULAR/AFTER end before that DAY start, preferring the supplied `trade_date`'s AFTER end;
4. fall back through `static_day()` for up to seven dates and finally ET 19:50;
5. return 30-minute orderly and 15-minute aggressive offsets.

- [ ] **Step 4: Run tests and confirm GREEN**

Run: `pytest -q tests/test_trading.py -k "next_day_exit_window"`

Expected: all selected tests PASS.

- [ ] **Step 5: Commit only Task 1 files**

```powershell
git add -- pennybot/trading/strategies.py tests/test_trading.py
git commit -m "feat: calculate next-day market exit window"
```

### Task 2: Apply policy to plans, holdings, and entry orders

**Files:**
- Modify: `pennybot/config.py`
- Modify: `pennybot/trading/trader.py`
- Modify: `config.yaml`
- Test: `tests/test_trading.py`

- [ ] **Step 1: Write failing behavior tests**

Cover these cases independently:

- non-swing 20/45/240-minute strategy deadlines are replaced with the policy window;
- known SWING deadlines remain unchanged;
- unknown strategy names default to non-swing;
- runtime exit evaluation uses the effective deadline for existing OPEN and EXITING rows without changing persisted closed rows;
- scanner and `_add_ons` stop in the orderly-exit window;
- pending entry orders are cancelled and cannot be repriced after the window starts;
- old `_morning_cutoff` is bypassed when `next_day_exit` is enabled;
- a position still unfilled after the sellable session keeps retrying, blocks entries, and emits the urgent warning.

- [ ] **Step 2: Run focused tests and confirm RED**

Run: `pytest -q tests/test_trading.py -k "next_day_exit or exit_window or pending_entry_cutoff"`

Expected: new tests FAIL while existing tests remain unchanged.

- [ ] **Step 3: Add the configuration switch**

Add `next_day_exit: bool = True` to `TradingConfig` and `next_day_exit: true` to `config.yaml`. Set `flatten_kst: ""` and `entry_cutoff_kst: ""`. The switch, not blank strings, must bypass `_morning_cutoff`.

- [ ] **Step 4: Centralize policy application in `Trader`**

Add small helpers:

```python
def _is_swing(self, strategy: str) -> bool:
    return bool(getattr(self.strategies.get(strategy), "SWING", False))

def _next_day_window(self, now, trade_date):
    return next_day_exit_window(now, self.cal, trade_date)
```

Before order submission, overwrite `sell_from` and `deadline` only for non-swing plans. Before `_exit_reason`, compare non-swing OPEN/EXITING trades against the effective window rather than the persisted short timeout. Unknown strategies use the non-swing path.

- [ ] **Step 5: Gate every entry path**

At the orderly-exit boundary:

- `scan_and_enter` excludes non-swing plans;
- `_add_ons` returns before placing orders;
- working non-swing entry orders are cancelled;
- polling/repricing does not replace cancelled entries;
- the entry gate remains active while a required exit is unfilled.

Keep manual flatten, bad-news, stop, target, trail, and SWING behavior unchanged.

- [ ] **Step 6: Run focused and module tests**

Run: `pytest -q tests/test_trading.py tests/test_setups.py tests/test_rules7.py`

Expected: all selected tests PASS.

- [ ] **Step 7: Commit only Task 2 files**

```powershell
git add -- pennybot/config.py pennybot/trading/trader.py config.yaml tests/test_trading.py
git commit -m "feat: hold positions until next day-market cutoff"
```

### Task 3: Five-hour flat-position exit

**Files:**
- Modify: `pennybot/config.py`
- Modify: `pennybot/trading/trader.py`
- Modify: `config.yaml`
- Test: `tests/test_trading.py`

- [ ] **Step 1: Write failing tests for the five-hour rule**

Create independent fixtures for 4:59 versus exactly 5:00; equality at 3%, 5%, $30,000 and both 0.5 ratios; only-volume and only-dollar failures; zero baselines; gapped, stale, insufficient, and query-error bars; each qualifying event kind and flow-query failure; a prior wider move; SWING exclusion; and both legacy timers being bypassed in `next_day_exit` mode.

- [ ] **Step 2: Run tests and confirm RED**

Run: `pytest -q tests/test_trading.py -k "five_hour_flat"`

Expected: FAIL because the new rule is absent.

- [ ] **Step 3: Implement `_five_hour_flat_exit`**

Use the executable bid and weighted-average entry price. Require age `>= flat_exit_hours`, `abs(bid / entry_price - 1) * 100 <= 3`, and `(max(high)-min(low))/entry_price*100 <= 5` over post-entry bars. Require at least 120 unique bars and a latest bar age `<= 5m`.

Accumulation is any of `burst`, `flow2`, `after_move`, `bottom_bounce`, `early20`, `steady`, or `spike_up` in `(now-60m, now]`; query errors hold. For volume support, use recent `(now-30m, now]` and baseline `(now-90m, now-30m]`, with at least 15/30 unique bars. Require recent dollars `>= 30000`, average volume per observed bar ratio `>= 0.5`, and average `close*volume` ratio `>= 0.5`. Define zero/zero as not qualifying and positive/zero as infinite. Any insufficient/query-error data holds.

- [ ] **Step 4: Add configuration defaults**

Add the five named settings to `TradingConfig` and `config.yaml`. In `next_day_exit` mode bypass both `_stale_exit` and the timed `no_rebound_at` branch so the new rule is the sole early time exit. Leave untimed dumping/decline exits intact.

- [ ] **Step 5: Run focused tests and confirm GREEN**

Run: `pytest -q tests/test_trading.py -k "five_hour_flat"`

Expected: all selected tests PASS.

- [ ] **Step 6: Commit the scoped rule**

```powershell
git add -- pennybot/config.py pennybot/trading/trader.py config.yaml tests/test_trading.py
git commit -m "feat: exit flat positions after five hours"
```

### Task 4: Regression and live-safe rollout

**Files:**
- Modify only if verification finds a scoped defect.

- [ ] **Step 1: Run trading and calendar regression suites**

Run: `pytest -q tests/test_trading.py tests/test_setups.py tests/test_rules7.py tests/test_sessions.py tests/test_ops.py`

Expected: all tests PASS, aside from already documented unrelated baseline failures.

- [ ] **Step 2: Inspect effective live configuration without placing orders**

Load `config.yaml`, instantiate the policy with the current calendar, and print the next orderly/aggressive timestamps. Confirm they precede the next DAY start and that `flatten_kst`/`entry_cutoff_kst` are blank.

- [ ] **Step 3: Check the dirty worktree narrowly**

Run: `git diff --check -- pennybot/config.py pennybot/trading/strategies.py pennybot/trading/trader.py config.yaml tests/test_trading.py`

Expected: no whitespace errors and no unrelated files staged.

- [ ] **Step 4: Restart only the trading process if it is currently running**

Use the repository's existing supervisor/restart flag. Do not restart the collector or dashboard. Verify heartbeat and logs show the new effective cutoff; do not claim rollout success without this evidence.

- [ ] **Step 5: Final commit if verification required fixes**

Commit only the scoped files with a message describing the verified defect.
