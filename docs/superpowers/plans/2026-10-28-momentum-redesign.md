# 페니 급등주 재설계 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 설계 문서(`docs/superpowers/specs/2026-10-28-momentum-redesign-design.md`)의 5개 당일 셋업·−50% 손절·3시간 규칙·08:50 KST 전량 청산을 실제 매매 엔진에 넣는다.

**Architecture:** 기존 `Trader` 루프와 `Journal`/`MarketView`는 그대로 쓰고, 신호 생성만 `strategies.py`(구 전략)에서 새 `setups.py`(5개 셋업 + 점수)로 바꾼다. 청산은 `_exit_reason()`에 규칙 3개(3시간, −50%, 08:50)를 추가해 한 곳에서 관리한다.

**Tech Stack:** Python 3.13, sqlite3, pytest, 기존 `pennybot` 모듈. 새 라이브러리 없음.

---

## 파일 구조

| 파일 | 책임 | 상태 |
|---|---|---|
| `pennybot/trading/setups.py` | 셋업 5개 감지 + 점수 → `Plan` 목록 | 신규 |
| `pennybot/trading/halts.py` | 거래정지 조회 (현재 정지 여부, 오늘 정지 이벤트) | 신규 |
| `pennybot/trading/scorecard.py` | 셋업별 성적 집계 (승률·기대값·3시간 발동) | 신규 |
| `pennybot/trading/trader.py` | 셋업 사용, 정지 시 주문취소, 청산 규칙 3개, 복구 | 수정 |
| `pennybot/trading/risk.py` | 쿨다운 무시 정책, 손절 후 60분, 종목당 하루 3회 | 수정 |
| `pennybot/trading/market.py` | 대차·SSR·정지·일중 고저·박스 지표 조회 | 수정 |
| `pennybot/trading/journal.py` | `signals.metrics`/`setup_version`, `trades.no_rebound_at` 마이그레이션 | 수정 |
| `pennybot/trading/report.py` | 일일 리포트에 셋업별 성적 섹션 | 수정 |
| `pennybot/config.py` | 새 설정값 + `setups` 기본값 | 수정 |
| `tests/test_setups.py` | 셋업 5개 단위 테스트 | 신규 |
| `tests/test_halts.py` | 정지 조회·주문취소 테스트 | 신규 |
| `tests/test_trading.py` | 청산 규칙·쿨다운·복구 테스트 추가 | 수정 |

원칙: 셋업 하나 = 클래스 하나 = 함수 하나(`scan(ctx) -> list[Plan]`). 점수 계산은 순수 함수라 테스트가 쉽다.

---

### Task 1: 설정값 추가

**Files:**
- Modify: `pennybot/config.py` (TradingConfig, `entry_ttl_seconds` 근처)
- Test: `tests/test_trading.py`

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_trading.py` 끝에 추가

```python
def test_redesign_defaults():
    from pennybot.config import TradingConfig
    t = TradingConfig()
    assert t.stop_loss_pct_default == 50.0
    assert t.no_rebound_minutes == 180
    assert t.no_rebound_high_pct == 3.0
    assert t.flatten_kst == "08:50"
    assert t.entry_cutoff_kst == "08:20"
    assert t.entry_band_min_pct == -3.0
    assert t.entry_band_max_pct == 5.0
    assert t.halt_cancel_orders is True
    assert t.ignore_cooldown_with_signal is True
    assert t.max_entries_per_symbol_day == 3
    assert t.reentry_block_minutes_after_stop == 60
```

- [ ] **Step 2: 실패 확인** — `\.venv\Scripts\python -m pytest tests/test_trading.py::test_redesign_defaults -v` → `AttributeError: stop_loss_pct_default`

- [ ] **Step 3: 구현** — `TradingConfig`에 추가

```python
    stop_loss_pct_default: float = 50.0  # 셋업이 따로 안 정하면 이만큼 (하드 손절)
    no_rebound_minutes: int = 180        # 이 시간 동안 반등·매집이 없으면 시간 손절
    no_rebound_high_pct: float = 3.0     # '반등'으로 인정하는 고점 기준 (진입가 대비 %)
    flatten_kst: str = "08:50"           # 매일 이 시각(KST)까지 전량 청산
    entry_cutoff_kst: str = "08:20"      # 이 시각부터 신규 진입 중단
    entry_band_min_pct: float = -3.0     # 기준가 대비 지정가 하한
    entry_band_max_pct: float = 5.0      # 기준가 대비 지정가 상한
    halt_cancel_orders: bool = True      # 정지 감지 시 미체결 주문 취소
    ignore_cooldown_with_signal: bool = True   # 신호가 있으면 연속손실·주간손실 쿨다운 무시
    max_entries_per_symbol_day: int = 3
    reentry_block_minutes_after_stop: int = 60
```

- [ ] **Step 4: 통과 확인** — `\.venv\Scripts\python -m pytest tests/test_trading.py::test_redesign_defaults -v` → PASS
- [ ] **Step 5: 커밋** — `git add pennybot/config.py tests/test_trading.py && git commit -m "feat(config): redesign defaults for stops, 3h rule, 08:50 KST flatten"`

---

### Task 2: DB 스키마 확장 (데이터화)

**Files:**
- Modify: `pennybot/trading/journal.py:49-63` (컬럼 마이그레이션 목록)
- Test: `tests/test_trading.py`

- [ ] **Step 1: 실패하는 테스트**

```python
def test_signal_metrics_and_rebound_columns(tmp_path):
    from pennybot.trading import Journal
    j = Journal(tmp_path / "t.db")
    j.log_signals([{"ts": "2026-10-28T00:00:00+00:00", "trade_date": "2026-10-28", "session": "DAY",
                    "strategy": "squeeze", "symbol": "PEN", "ok": 1, "reason": "r", "score": 71.0,
                    "price": 1.2, "metrics": {"short_ratio": 0.55}, "setup_version": 1}])
    row = j.q("SELECT metrics, setup_version FROM signals")[0]
    assert '"short_ratio": 0.55' in row["metrics"] and row["setup_version"] == 1
    tid = j.open_trade(strategy="squeeze", symbol="PEN", trade_date="2026-10-28", planned_qty=10,
                       stop=0.6, target=1.44, session="DAY", sell_from=None, deadline=None,
                       reason="r", now=__import__("datetime").datetime(2026, 10, 28, tzinfo=__import__("datetime").timezone.utc))
    assert j.trade(tid)["no_rebound_at"] is not None
```

- [ ] **Step 2: 실패 확인** — `\.venv\Scripts\python -m pytest tests/test_trading.py::test_signal_metrics_and_rebound_columns -v` → `no column named metrics`

- [ ] **Step 3: 구현**
  - `Journal.__init__`의 마이그레이션 목록에 추가: `("signals", "metrics", "TEXT")`, `("signals", "setup_version", "INTEGER")`, `("trades", "setup_version", "INTEGER")`, `("trades", "no_rebound_at", "TEXT")`.
  - `log_signals`: `rows` 의 `metrics`(dict)를 `json.dumps(ensure_ascii=False)` 로 바꿔 저장, 없으면 NULL.
  - `open_trade`: `no_rebound_at = entry 예정 시각 + no_rebound_minutes` 는 **체결 시점**에 정해야 하므로 여기선 NULL. `record_entry_fill` 에서 `now + timedelta(minutes=self.no_rebound_minutes)` 저장 → Journal 생성자에 `no_rebound_minutes: int = 180` 파라미터 추가.

- [ ] **Step 4: 통과 확인** — `\.venv\Scripts\python -m pytest tests/test_trading.py -q` → 기존 + 신규 통과
- [ ] **Step 5: 커밋** — `git commit -m "feat(journal): persist signal metrics, setup version, and 3h deadline"`

---

### Task 3: MarketView 조회 함수

**Files:**
- Modify: `pennybot/trading/market.py` (`short_ratio` 아래)
- Test: `tests/test_setups.py` (신규)

- [ ] **Step 1: 실패하는 테스트** — 임시 market.db 를 만들고 `borrow_fee`, `ssr_on`, `halt_today`, `intraday_range`, `box_metrics` 를 검증

```python
def test_marketview_squeeze_inputs(tmp_path):
    from pennybot.collector import Store
    from pennybot.trading.market import MarketView
    st = Store(tmp_path / "m.db")
    st.db.execute("INSERT INTO borrow (ts, symbol, fee_rate, rebate_rate, available) VALUES ('2026-10-27T20:00:00+00:00','PEN',42.0,-40.0,1000)")
    st.db.execute("INSERT INTO ssr (trade_date, symbol, trigger_time) VALUES ('2026-10-28','PEN','x')")
    st.db.execute("INSERT INTO halts (symbol, halt_ts, reason, reason_ko, resume_ts) VALUES ('PEN','2026-10-28T10:00:00-04:00','LUDP','변동성',NULL)")
    st.db.commit()
    mv = MarketView(str(tmp_path / "m.db"), client=None, calendar=None, exchange=None)
    assert mv.borrow_fee("PEN") == 42.0
    assert mv.ssr_on("PEN", "2026-10-28") is True
    assert mv.halted_now("PEN") is True
```

- [ ] **Step 2: 실패 확인** — `pytest tests/test_setups.py::test_marketview_squeeze_inputs -v` → `AttributeError: borrow_fee`
- [ ] **Step 3: 구현** — `market.py`에 5개 메서드

```python
    def borrow_fee(self, symbol: str) -> float | None:
        r = self.q("SELECT fee_rate FROM borrow WHERE symbol=? ORDER BY ts DESC LIMIT 1", (symbol,))
        return float(r[0][0]) if r and r[0][0] is not None else None

    def ssr_on(self, symbol: str, trade_date) -> bool:
        return bool(self.q("SELECT 1 FROM ssr WHERE symbol=? AND trade_date=?", (symbol, str(trade_date))))

    def halt_today(self, symbol: str, trade_date) -> dict | None:
        r = self.q("SELECT reason, halt_ts, resume_ts FROM halts WHERE symbol=? AND halt_ts>=? "
                   "ORDER BY halt_ts DESC LIMIT 1", (symbol, str(trade_date)))
        return dict(zip(("reason", "halt_ts", "resume_ts"), r[0])) if r else None

    def halted_now(self, symbol: str) -> bool:
        r = self.q("SELECT resume_ts FROM halts WHERE symbol=? ORDER BY halt_ts DESC LIMIT 1", (symbol,))
        return bool(r) and r[0][0] is None

    def box_metrics(self, symbol: str, today, days: int = 20) -> dict:
        rows = self.daily(symbol, today, max(days, 60))
        if len(rows) < days:
            return {}
        highs = [r["high"] for r in rows[-days:]]
        vols = [r["volume"] for r in rows]
        atr = lambda xs: sum(abs(xs[i] - xs[i - 1]) for i in range(1, len(xs))) / max(len(xs) - 1, 1)
        closes = [r["close"] for r in rows]
        return {"box_top": max(highs), "atr_ratio": (atr(closes[-10:]) / atr(closes[-60:])) if atr(closes[-60:]) else 9.9,
                "vol_ratio": (sum(vols[-10:]) / 10) / (sum(vols[-60:]) / 60) if sum(vols[-60:]) else 0.0,
                "close": closes[-1]}
```

`intraday_range(symbol, trade_date)` 는 `prices` 에서 `trade_date` 최고·최저·마지막을 돌려준다.

- [ ] **Step 4: 통과 확인** — `pytest tests/test_setups.py -q`
- [ ] **Step 5: 커밋** — `git commit -m "feat(market): expose borrow fee, SSR, halt state, box metrics"`

---

### Task 4: 셋업 기반 + 점수

**Files:** Create `pennybot/trading/setups.py`; Test `tests/test_setups.py`

- [ ] **Step 1: 실패하는 테스트**

```python
def test_setup_registry_and_score():
    from pennybot.trading import setups
    assert set(setups.ALL) == {"squeeze", "news", "session_gap", "accumulation", "reversal"}
    assert setups.score({"short_ratio": 0.5, "ctb": 40, "ssr": True, "dollar": 3_000_000}) >= 60
    assert setups.score({"short_ratio": 0.1, "ctb": 1, "ssr": False, "dollar": 100_000}) < 60
```

- [ ] **Step 2: 실패 확인** — `pytest tests/test_setups.py::test_setup_registry_and_score -v`
- [ ] **Step 3: 구현** — `setups.py`에 `Setup` 베이스, `Plan`(strategies 에서 import), `score(metrics)` (0~100, 지표별 구간 점수 합, 희석 점수 감점), `ALL = {cls.name: cls}`.
- [ ] **Step 4: 통과 확인**
- [ ] **Step 5: 커밋** — `git commit -m "feat(setups): base setup contract and scoring"`

---

### Task 5~9: 셋업 5개 (각각 별도 커밋)

각 셋업 공통 절차: ① 실패 테스트(FakeMV 로 지표 고정) → ② 실패 확인 → ③ 구현 → ④ 통과 → ⑤ 커밋.

| Task | 클래스 | 핵심 로직 | 테스트 이름 |
|---|---|---|---|
| 5 | `Squeeze` | `short_ratio ≥ .40`(오늘·전일), `borrow_fee ≥ 30`, `ssr_on or reg_sho`, `dollar ≥ 2e6`, `dilution < 60`, `0.5 ≤ px ≤ 5` | `test_squeeze_requires_hard_to_borrow` |
| 6 | `News` | 60분 내 호재 이벤트, 3분 거래대금 ≥ 200K, 스프레드 ≤ 2, 신호 전 고가 대비 +30% 초과면 제외 | `test_news_skips_already_pumped` |
| 7 | `SessionGap` | 데이마켓 가격 ≤ 직전 프리/정규 종가 × 0.97, 거래대금 ≥ 1M, 스프레드 ≤ 3 | `test_session_gap_needs_discount` |
| 8 | `Accumulation` | `box_top` 5% 이내, `atr_ratio < .7`, 거래량비 > 1.3, OBV 상승, 희석 없음, $1~10 | `test_accumulation_requires_tight_box` |
| 9 | `Reversal` | 당일 −35% 이하, 희석 없음, 뉴스·규제 정지 없음, 거래대금 ≥ 5M, 종가 ≥ 저가×1.15 | `test_reversal_skips_dilution` |

각 셋업은 `Plan(strategy=name, symbol=..., ok=True, score=..., price=ref, stop=ref*0.5, target=...,
deadline=next_flatten_kst(now), sell_from=...)` 를 돌려준다.

- [ ] **Step 5-1..9-5**: 셋업별 커밋 `feat(setups): add <name> setup`

---

### Task 10: 지정가 밴드 −3% ~ +5%

**Files:** Modify `pennybot/trading/setups.py` (`entry_price()` 공용 함수); Test `tests/test_setups.py`

- [ ] **Step 1: 실패하는 테스트**

```python
def test_entry_price_clamped_to_band():
    from pennybot.trading.setups import entry_price
    assert entry_price(ref=1.00, spread_pct=0.5, lo=-3, hi=5, tcfg=None) == 1.05
    assert entry_price(ref=1.00, spread_pct=6.0, lo=-3, hi=5, tcfg=None) == 0.97
```

- [ ] **Step 2: 실패 확인** → ③ `ref*(1+hi/100)` / `ref*(1+lo/100)` 로 클램프, 좁은 스프레드면 ask, 아니면 mid 기준
- [ ] **Step 4: 통과 확인** — 기존 `test_unfilled_entry_follows_ask_once_before_cancel` 도 통과해야 함
- [ ] **Step 5: 커밋** — `feat(setups): clamp entry limit to -3%..+5% of reference`

---

### Task 11: 쿨다운 정책

**Files:** Modify `pennybot/trading/risk.py:38-83`; Test `tests/test_trading.py`

- [ ] **Step 1: 실패하는 테스트** — 4연속 손실 상태에서 `ignore_cooldown_with_signal=True` 면 `can_enter` 가 `None`; 같은 종목을 손절 30분 뒤 진입하면 "손절 후 재진입 대기" 반환
- [ ] **Step 3: 구현** — `cooldown()` 은 유지하되 `can_enter` 에서 `if not self.t.ignore_cooldown_with_signal` 일 때만 적용. 신규: `Journal.last_stop_exit_ts(symbol)` 로 60분 차단, `Journal.entries_today(symbol)` 로 3회 제한
- [ ] **Step 5: 커밋** — `feat(risk): ignore loss-streak cooldown when a setup fires`

---

### Task 12: 거래정지 처리 (이력으로 막지 않음)

**Files:** Create `pennybot/trading/halts.py`; Modify `trader.py`(`poll_orders` 앞); Test `tests/test_halts.py`

- [ ] **Step 1: 실패하는 테스트** — `halted_now` 종목의 working 주문이 취소되고, 재개되면 같은 셋업이 다시 `ok=True` 로 돌아오는지
- [ ] **Step 3: 구현**
  ```python
  # halts.py
  def cancel_on_halt(trader, now) -> list[str]:
      """정지된 종목의 미체결 주문을 취소한다. 종목 자체를 블랙리스트에 넣지 않는다 (재개되면 다시 산다)."""
  ```
  `trader.tick()` 의 `exits` 앞에서 호출. 보유 중이면 서버 조건주문은 그대로 둔다.
- [ ] **Step 5: 커밋** — `feat(trader): cancel working orders on halt without blacklisting the symbol`

---

### Task 13: 청산 규칙 (08:50 / −50% / 3시간)

**Files:** Modify `pennybot/trading/trader.py:594-675`; Test `tests/test_trading.py`

- [ ] **Step 1: 실패하는 테스트 3개**
  - `test_flatten_before_kst_0850`: 09:00 KST 매수 → 다음날 08:50 KST 에 "8:50 전 정리"로 청산
  - `test_hard_stop_50_percent`: −50% 에서 손절
  - `test_no_rebound_exit_after_3h`: 고점 < +3% 이고 매집 신호 없으면 180분 뒤 "반등 없음" 청산, 매집 신호가 있으면 유지
- [ ] **Step 3: 구현**
  - `_morning_cutoff` → KST 기준 `flatten_kst` (기존 ET 필드는 폐기, `dtime`+`KST` 사용)
  - `_exit_reason` 순서: 강제 청산 → 악재 → **손절(−50% 또는 셋업 손절)** → 트레일 → 익절 → **3시간 규칙** → 매도 구간 → 8:50 정리
  - 3시간: `now >= no_rebound_at` and `high_water < entry*(1+3/100)` and `not mv.recent_flow(now-60m, kinds=("burst","steady"))`
- [ ] **Step 5: 커밋** — `feat(trader): -50% stop, 3h no-rebound exit, 08:50 KST flatten`

---

### Task 14: 재시작 복구

**Files:** Modify `trader.py` `_recover()`; Test `tests/test_trading.py`

- [ ] **Step 1: 실패하는 테스트** — 매수 후 `high_water`·`no_rebound_at` 을 남기고 새 Trader 를 만들면 값이 유지되고, `no_rebound_at` 이 이미 지났으면 첫 틱에 청산된다
- [ ] **Step 3: 구현** — `_recover()` 가 `high_water`·`no_rebound_at`·`setup_version` 을 그대로 읽고, 지난 `no_rebound_at` 은 `now` 로 당겨 즉시 판정
- [ ] **Step 5: 커밋** — `feat(trader): resume 3h rule and high-water after restart`

---

### Task 15: 성적표 (리포트·대시보드)

**Files:** Create `pennybot/trading/scorecard.py`; Modify `report.py`, `dashboard/server.py`, `static/index.html`; Test `tests/test_insights.py`

- [ ] **Step 1: 실패하는 테스트** — 셋업 2개 매매(1승 1패)를 넣으면 `scorecard.rows()` 가 승률 50%, 기대값 계산값을 돌려준다
- [ ] **Step 3: 구현** — `rows(journal, days=14)` → `[{setup, n, wins, winrate, avg_pnl, expectancy, no_rebound_exits}]`; 리포트·대시보드에 표로
- [ ] **Step 5: 커밋** — `feat(report): per-setup scorecard`

---

### Task 16: 엔진 연결 + 구 전략 끄기

**Files:** Modify `trader.py` (`self.strategies` → `setups.ALL`), `config.py` (`strategies` 기본 enabled False); Test `tests/test_trading.py`

- [ ] **Step 1: 실패하는 테스트** — 기본 설정에서 `trader.strategies` 키가 5개 셋업 이름과 같다
- [ ] **Step 3: 구현** — `Trader.__post_init__` 에서 `self.strategies = {n: cls(cfg) for n, cls in setups.ALL.items() if enabled}`, 구 `strategies.ALL` 은 import 만 남김
- [ ] **Step 5: 전체 테스트** — `\.venv\Scripts\python -m pytest -q` → 200+ 통과 (기존 실패 2건은 브리핑 관련, 별개)
- [ ] **Step 6: 커밋** — `feat(trader): run the five setups instead of legacy strategies`

---

### Task 17: 백테스트 (셋업 4·5)

**Files:** Modify `pennybot/backtest.py`; Test `tests/test_backtest.py`

- [ ] **Step 1: 실패하는 테스트** — 일봉 리플레이에서 `accumulation` 셋업이 최소 1건 신호를 낸다
- [ ] **Step 3: 구현** — 백테스트가 `setups.ALL` 을 쓰도록, 진입가·체결은 기존 보수 모델 유지
- [ ] **Step 5: 실행** — `\.venv\Scripts\python -m pennybot backtest --start 2025-01-02 --end 2026-10-27`
- [ ] **Step 6: 커밋** — `feat(backtest): replay the new setups`

---

### Task 18: 주간 자동 정지

**Files:** Modify `report.py`; Test `tests/test_insights.py`

- [ ] **Step 1: 실패하는 테스트** — 20회 중 승률 30% 인 셋업이 자동으로 `enabled=False` 가 된다
- [ ] **Step 3: 구현** — `apply_auto_disable(journal, control, tcfg)` 를 주간 리포트에서 호출
- [ ] **Step 5: 커밋** — `feat(report): auto-disable setups with negative expectancy`

---

## 검증 명령 (단계별)

```powershell
.venv\Scripts\python -m pytest tests/test_setups.py -q      # Task 3~10
.venv\Scripts\python -m pytest tests/test_trading.py -q     # Task 1,2,11,13,14,16
.venv\Scripts\python -m pytest tests/test_halts.py -q       # Task 12
.venv\Scripts\python -m pytest -q                           # 전체 (기존 실패 2건 제외)
.venv\Scripts\python -m pennybot backtest --start 2025-01-02 --end 2026-10-27   # Task 17
```

## 롤백

각 Task 가 독립 커밋이므로 `git revert <해시>` 한 번으로 되돌린다. 엔진 연결(Task 16)만 되돌리면 구 전략으로 즉시 복귀한다.

---

# 2부: 데이터 · 자동 학습 · 실시간 신호 (Tasks 19~26)

설계: `docs/superpowers/specs/2026-10-28-runner-dataset-and-learning-design.md`

### Task 19: 일봉 백필 (재개 가능)

**Files:** Create `pennybot/collector/backfill.py`; Modify `pennybot/__main__.py` (서브커맨드 `backfill-daily`); Test `tests/test_collector.py`

- [ ] **Step 1: 실패하는 테스트** — `backfill_daily(fake_client, store)` 가 200봉 넘는 종목에서 `before` 커서로 2페이지를 요청하고, `candle_jobs` 에 진행 상태를 남긴다
- [ ] **Step 2: 실패 확인** — `.venv\Scripts\python -m pytest tests/test_collector.py::test_backfill_daily_pages_and_resumes -v`
- [ ] **Step 3: 구현** — `c.candles(sym, "1d", 200, before=...)` 반복, 종목별 완료 시 `candle_jobs` 갱신, 실패는 3회 재시도 후 다음 종목
- [ ] **Step 4: 통과 확인**
- [ ] **Step 5: 실제 실행** — `.venv\Scripts\python -m pennybot backfill-daily --since 2025-01-01` (예상 8천~1만 요청, 30~60분)
- [ ] **Step 6: 커밋** — `feat(collector): resumable daily bar backfill`

### Task 20: 급등주 기록 복구 (현재 0행 버그)

**Files:** Modify `pennybot/collector/runner.py:1902-1956`; Test `tests/test_collector.py`

- [ ] **Step 1: 실패하는 테스트** — `record_runners("2026-10-27")` 후 `runners` 에 +30% 종목이 최소 1건 있다
- [ ] **Step 2: 현재 실패 원인 확인** — `.venv\Scripts\python -c "from pennybot.collector import Store; s=Store('state/market.db'); print(s.runner_candidates('2026-10-27','2026-10-26',30.0)[:5])"` → 빈 결과면 쿼리 문제, 예외면 로그 확인
- [ ] **Step 3: 구현** — 원인 수정 + `catch_up_summaries` 가 `runners` 없는 과거 영업일도 보충하도록 조건 추가
- [ ] **Step 4: 통과 확인** — `.venv\Scripts\python -m pytest tests/test_collector.py -q`
- [ ] **Step 5: 과거 보충** — `runners` 가 빈 최근 5영업일을 수동 백필
- [ ] **Step 6: 커밋** — `fix(collector): record daily runners and backfill missing days`

### Task 21: 특징·성과 테이블

**Files:** Create `pennybot/collector/runners.py`; Test `tests/test_setups.py`

- [ ] **Step 1: 실패하는 테스트** — 러너 1건에 대해 `features` 행(유통주식·ATR·랭킹·데이마켓 등락)과 `outcomes` 의 `T+1_pre` 가 계산된다
- [ ] **Step 2: 실패 확인**
- [ ] **Step 3: 구현** — `build_features(store, trade_date)` / `fill_outcomes(store, upto_trade_date)`; 스키마는 설계 2.2 그대로
- [ ] **Step 4: 통과 확인**
- [ ] **Step 5: 커밋** — `feat(collector): runner features and forward outcomes`

### Task 22: 미스 사유 분류

**Files:** Create `pennybot/collector/misses.py`; Test `tests/test_setups.py`

- [ ] **Step 1: 실패하는 테스트** — 유니버스에 없던 러너 → `not_in_universe`, 스프레드 초과 → `spread_too_wide`, 이미 +35% 오른 뒤 → `too_late`
- [ ] **Step 2: 실패 확인**
- [ ] **Step 3: 구현** — 설계 2.3 표의 순서대로 판정(첫 번째로 걸리는 사유 채택), `signals`·`universe`·`rankings`·`halts` 조인
- [ ] **Step 4: 통과 확인**
- [ ] **Step 5: 커밋** — `feat(collector): classify why each runner was missed`

### Task 23: 실시간 신호 → 매매 반응

**Files:** Modify `pennybot/collector/runner.py` (`_on_trade`), `pennybot/trading/trader.py` (`tick`), `pennybot/trading/market.py`; Test `tests/test_trading.py`

- [ ] **Step 1: 실패하는 테스트** — `events` 에 새 행이 들어오면 `scan_seconds` 가 안 지났어도 `scan_and_enter` 가 한 번 돈다
- [ ] **Step 2: 실패 확인**
- [ ] **Step 3: 구현** — `MarketView.new_events(since_ts)` (인덱스 `ix_events_ts`), `Trader.tick()` 에서 1초 폴링으로 신규 이벤트 확인 → 즉시 스캔. 중복 스캔 방지 쿨다운 1초
- [ ] **Step 4: 통과 확인** — `pytest tests/test_trading.py -q`
- [ ] **Step 5: 커밋** — `feat(trader): scan immediately on realtime events`

### Task 24: 텔레그램 실시간 푸시

**Files:** Modify `pennybot/collector/runner.py` (`_alert`), `pennybot/trading/notify.py`; Test `tests/test_insights.py`

- [ ] **Step 1: 실패하는 테스트** — 같은 종목·같은 종류 신호는 10분에 1번만 전송된다
- [ ] **Step 2: 실패 확인**
- [ ] **Step 3: 구현** — 기존 `_ev_last`/`notifier.send(..., key=..., every=600)` 활용, 신호 문구에 "근거 5줄 + 매수 준비" 포함
- [ ] **Step 4: 통과 확인**
- [ ] **Step 5: 커밋** — `feat(notify): realtime signal push with dedupe`

### Task 25: 학습기 (해석 가능 + 홀드아웃)

**Files:** Create `pennybot/learning.py`; Test `tests/test_insights.py`

- [ ] **Step 1: 실패하는 테스트** — 표본 19건이면 `learn()` 이 `{"changed": False, "reason": "표본 부족"}` 반환, 30건 이상이면 후보 규칙과 검증 성적을 돌려준다
- [ ] **Step 2: 실패 확인**
- [ ] **Step 3: 구현** — 특징별 lift → 규칙 후보(단일·2조건) → 앞 70% 적합 / 뒤 30% 검증 → `changed` 결정. 결과를 `meta` 에 JSON 저장
- [ ] **Step 4: 통과 확인**
- [ ] **Step 5: 커밋** — `feat(learning): interpretable rule learner with holdout gate`

### Task 26: 주간 리포트 + 자동 적용

**Files:** Modify `pennybot/trading/report.py`, `pennybot/trading/scorecard.py`; Test `tests/test_insights.py`

- [ ] **Step 1: 실패하는 테스트** — 학습이 채택한 규칙이 모의매매에서만 적용되고, 실거래 모드에서는 `meta` 에 제안만 남는다
- [ ] **Step 2: 실패 확인**
- [ ] **Step 3: 구현** — 일요일 리포트에 "이번 주 학습 결과 / 채택·폐기 / 다음 주 조건" 표 추가
- [ ] **Step 4: 통과 확인**
- [ ] **Step 5: 커밋** — `feat(report): weekly learning report and paper-only auto apply`

## 검증 명령 (2부)

```powershell
.venv\Scripts\python -m pytest tests/test_setups.py tests/test_insights.py -q
.venv\Scripts\python -m pennybot backfill-daily --since 2025-01-01
.venv\Scripts\python -c "import sqlite3;c=sqlite3.connect('state/market.db');print(c.execute('select count(*) from runner_events').fetchone())"
```

