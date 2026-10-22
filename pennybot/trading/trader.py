"""세션 매매 엔진: 데이마켓에 사고 그날 밤(프리마켓~정규장 개장 직후)에 판다.

매 틱(기본 5초):
  1. 주문 상태 확인 → 체결을 trades 에 반영, 매수 미체결 TTL 취소, 매도 재호가
  2. 세션이 바뀌면 걸려 있는 주문을 모두 취소 (토스 DAY 주문은 세션을 넘어 이어지므로 직접 정리)
  3. 보유 종목 청산 판단: 강제 청산 시각 > 손절 > 익절 > 매도 시작 시각
  4. 데이마켓이면 전략 스캔 → 리스크 → 지정가 매수
  5. 계좌 스냅샷, 하트비트, 잔고 대조
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..sessions import CLOSED, DAY, REGULAR, SessionCalendar
from .control import Control
from .exchange import TERMINAL, Exchange
from .journal import Journal
from .notify import Notifier
from .risk import Account, Risk
from .strategies import ALL, FINANCING_WORDS, Ctx, events_of

# 들고 있는 동안 이런 공시가 나오면 즉시 청산 (급등주 최대 위험: 밤사이 증자 발표)
HOLD_ALARM = FINANCING_WORDS | {"DELISTING_NOTICE", "OFFERING_FILED", "SHELF_FILED", "WARRANT_INDUCEMENT",
                                "WARRANT_REPRICE", "CONVERTIBLE"}

log = logging.getLogger(__name__)


@dataclass
class Working:
    """하나의 주문 '줄기'. 토스는 정정할 때마다 새 orderId 를 주므로 ids 에 순서대로 쌓는다.
    체결은 각 id 에 남는다 (원주문 체결분 + 정정 후 새 주문 체결분)."""
    order_id: str               # 줄기의 첫 id (working 딕셔너리 키)
    trade_id: int
    purpose: str                # entry / exit
    symbol: str
    side: str
    placed_at: datetime         # 마지막 주문/정정 시각
    filled: int = 0             # 줄기 전체 누적 체결
    avg: float = 0.0
    note: str = ""
    cancel_sent: bool = False   # 취소 요청을 이미 보냄 (처리 중에는 다시 보내지 않는다)
    cancel_id: str | None = None
    cancel_at: datetime | None = None
    aggressive: bool = False
    ids: list = field(default_factory=list)
    fills: dict = field(default_factory=dict)   # id → (filled, avg)

    @property
    def current(self) -> str:
        return self.ids[-1] if self.ids else self.order_id


@dataclass
class Trader:
    ex: Exchange
    journal: Journal
    cal: SessionCalendar
    mv: object
    tcfg: object
    control: Control
    notifier: Notifier = field(default_factory=Notifier)
    clock: object = field(default=lambda: datetime.now(timezone.utc))
    fund: object = None          # Fundamentals: 희석 프로필·펌프 신호 (없으면 수집기 공시만으로 판단)

    def __post_init__(self):
        self.risk = Risk(self.tcfg, self.journal, self.control)
        self.strategies = {n: cls(self.tcfg.strategies[n]) for n, cls in ALL.items() if n in self.tcfg.strategies}
        self.working: dict[str, Working] = {}
        self.exit_reprices: dict[int, int] = {}
        self.last_session = None
        self._next: dict[str, datetime] = {}
        self.status: dict = {}
        self._stale_alerted: set[int] = set()
        self._recover()

    # ---------- 공통 ----------
    def _recover(self) -> None:
        """재시작 시 아직 열려 있는 주문을 다시 추적한다 (같은 모드의 주문만: 모의 주문을 실계좌에 조회하지 않게)."""
        for o in self.journal.q("""SELECT o.* FROM orders o JOIN trades t ON o.trade_id = t.id
                                   WHERE o.status='OPEN' AND t.mode=?""", (self.journal.mode,)):
            w = Working(o["order_id"], o["trade_id"], o["purpose"], o["symbol"], o["side"],
                        datetime.fromisoformat(o["updated"] or o["ts"]), o["filled"] or 0, o["avg_price"] or 0.0,
                        ids=[o["order_id"]], fills={o["order_id"]: (o["filled"] or 0, o["avg_price"] or 0.0)})
            self.working[o["order_id"]] = w

    def _due(self, name: str, now: datetime, every: timedelta) -> bool:
        if now >= self._next.get(name, datetime.min.replace(tzinfo=timezone.utc)):
            self._next[name] = now + every
            return True
        return False

    # ---------- 틱 ----------
    def tick(self) -> None:
        now = self.clock()
        trade_date, session = self.cal.classify(now)
        self.ex.sync()
        self.poll_orders(now, session)

        if self.last_session is not None and session != self.last_session:
            log.info("세션 변경 %s → %s: 걸린 주문 모두 취소", self.last_session, session)
            self.cancel_all(now, "세션 변경")
            self.poll_orders(now, session)
        self.last_session = session
        self.status = {"session": session, "trade_date": str(trade_date) if trade_date else None,
                       "mode": self.journal.mode, "working": len(self.working)}

        if session != CLOSED:
            ctl = self.control.load()
            if ctl["flatten"]:
                self.flatten(now, session)
            self.manage_exits(now, session)
            if session == DAY and not ctl["kill"] and self._entry_window(now, trade_date):
                if self._due("scan", now, timedelta(minutes=self.tcfg.scan_minutes)):
                    self.scan_and_enter(now, trade_date, session)
        if self._due("equity", now, timedelta(minutes=5)):
            self.snapshot(now, trade_date)
        if self._due("reconcile", now, timedelta(minutes=15)):
            self.reconcile()
        self.journal.beat("trader", self.status, now)

    def run(self, sleep=time.sleep) -> None:
        log.info("매매 엔진 시작 (mode=%s)", self.journal.mode)
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("틱 실패")
                self.notifier.send("⚠️ 매매 엔진 오류 (로그 확인)")
            sleep(5)

    def _entry_window(self, now: datetime, trade_date) -> bool:
        b = self.cal.bounds(trade_date, DAY)
        return bool(b) and now < b[1] - timedelta(minutes=self.tcfg.entry_cutoff_minutes)

    # ---------- 주문 추적 ----------
    def poll_orders(self, now: datetime, session: str) -> None:
        for key, w in list(self.working.items()):
            try:
                cur = self.ex.order(w.current)
            except Exception as e:
                log.warning("주문 조회 실패 %s: %s", w.current, e)
                continue
            w.fills[w.current] = (cur["filled"], cur["avg_price"] or 0.0)
            total = sum(f for f, _ in w.fills.values())
            delta = total - w.filled
            if delta > 0:
                value = sum(f * a for f, a in w.fills.values())
                px = (value - w.avg * w.filled) / delta
                w.filled, w.avg = total, value / total
                self._on_fill(w, delta, px, session, now)
            self.journal.update_order(cur, now)

            done = cur["status"] in TERMINAL
            if not done and w.cancel_sent and w.cancel_id and w.cancel_at and \
                    (now - w.cancel_at).total_seconds() > 60:
                # 취소가 1분 넘게 처리 중이면 취소 주문 자체의 상태를 본다
                try:
                    c = self.ex.order(w.cancel_id)
                    if c["status"] == "CANCELED":
                        done = True
                    elif c["status"] == "REJECTED":
                        log.warning("취소 거부 %s → 다시 시도", w.current)
                        w.cancel_sent, w.cancel_id = False, None
                except Exception as e:
                    log.debug("취소 주문 조회 실패 %s: %s", w.cancel_id, e)
            if done:
                self.working.pop(key)
                if w.purpose == "entry":
                    self.journal.entry_finished(w.trade_id)
                else:
                    self.journal.exit_unfilled(w.trade_id)
                continue
            if cur["status"] == "REPLACED" or w.cancel_sent:
                continue          # 정정·취소 처리 중
            age = (now - w.placed_at).total_seconds()
            if w.purpose == "entry" and age > self.tcfg.entry_ttl_seconds:
                self._cancel(key, "매수 미체결 TTL", now)
            elif w.purpose == "exit" and age > self.tcfg.exit_reprice_seconds:
                self._reprice(key, w, now, session)

    def _on_fill(self, w: Working, delta: int, px: float, session: str, now: datetime) -> None:
        if w.purpose == "entry":
            self.journal.record_entry_fill(w.trade_id, delta, px, session, now)
            self.notifier.send(f"🟢 매수 체결 {w.symbol} {delta}주 @ ${px:.4f}")
            return
        t = self.journal.record_exit_fill(w.trade_id, delta, px, session, now)
        if t["status"] == "CLOSED":
            self.exit_reprices.pop(w.trade_id, None)
            self.notifier.send(f"🔵 청산 {t['symbol']} ({t['exit_reason']}) "
                               f"{t['pnl']:+.2f}$ ({t['pnl_pct']:+.1f}%)")

    def _exit_price(self, b, aggressive: bool, exhausted: bool, session: str) -> tuple[bool, float | None]:
        """(낼 수 있는지, 가격). 가격 None = 시장가. 호가가 없으면 정규장의 공격적 청산만 시장가."""
        if b.has_quote:
            slip = self.tcfg.final_exit_slippage_pct if aggressive and exhausted else self.tcfg.exit_slippage_pct
            return True, (b.bid * (1 - slip / 100) if aggressive else b.bid)
        if session == REGULAR and aggressive:
            return True, None
        return False, None

    def _reprice(self, key: str, w: Working, now: datetime, session: str) -> None:
        """매도 재호가: 취소 후 재주문 대신 가격 정정 (미국 주식은 가격만 정정 가능). 실패하면 취소."""
        count = self.exit_reprices.get(w.trade_id, 0) + 1
        try:
            b = self.ex.book(w.symbol)
        except Exception as e:
            log.warning("재호가용 호가 실패 %s: %s", w.symbol, e)
            return
        ok, price = self._exit_price(b, w.aggressive, count >= self.tcfg.max_exit_reprices, session)
        if not ok:
            w.placed_at = now          # 호가가 없으면 기다렸다가 다시
            return
        self.exit_reprices[w.trade_id] = count
        try:
            new_id = self.ex.modify(w.current, price)
        except Exception as e:
            log.info("정정 실패 %s (%s) → 취소 후 재주문", w.current, e)
            self._cancel(key, "매도 재호가", now)
            return
        old = w.current
        w.ids.append(new_id)
        w.placed_at = now
        try:
            self.journal.update_order(self.ex.order(old), now)   # 원주문은 REPLACED 로 기록
        except Exception:
            pass
        left = max(self.journal.trade(w.trade_id)["remaining"], 0)
        self.journal.log_order({"order_id": new_id, "symbol": w.symbol, "side": w.side,
                                "type": "MARKET" if price is None else "LIMIT", "qty": left, "price": price,
                                "status": "OPEN", "filled": 0, "avg_price": None},
                               trade_id=w.trade_id, purpose=w.purpose, session=session, now=now,
                               note=f"정정 {count}회 ({w.current if len(w.ids) < 2 else w.ids[-2]} → {new_id})")
        log.info("매도 정정 %s %s → %s", w.symbol, "시장가" if price is None else f"${price:.4f}", new_id)

    def _cancel(self, key: str, why: str, now: datetime | None = None) -> None:
        w = self.working.get(key)
        if w and w.cancel_sent:
            return
        oid = w.current if w else key
        try:
            cid = self.ex.cancel(oid)
            if w:
                w.cancel_sent, w.cancel_id, w.cancel_at = True, cid, now or self.clock()
            log.info("주문 취소 %s (%s)", oid, why)
        except Exception as e:
            log.warning("취소 실패 %s: %s", oid, e)

    def cancel_all(self, now: datetime, why: str) -> None:
        for key in list(self.working):
            self._cancel(key, why, now)

    # ---------- 청산 ----------
    def _bad_news(self, t: dict) -> str | None:
        """매수 이후 접수된 악재 공시(증자·상폐 등)."""
        since = datetime.fromisoformat(t["entry_ts"]) if t.get("entry_ts") else None
        if not since:
            return None
        try:
            filings = self.mv.filings_for(t["symbol"], 3, self.clock())
        except Exception:
            return None
        for f in filings:
            hit = events_of(f) & HOLD_ALARM
            if hit and datetime.fromisoformat(f["accepted"]) >= since:
                return "보유 중 악재 공시: " + ",".join(sorted(hit))
        return None

    def _exit_reason(self, t: dict, bid: float, now: datetime) -> tuple[str, bool] | None:
        """(사유, 공격적 매도 여부)"""
        if now >= datetime.fromisoformat(t["deadline"]):
            return "강제 청산 시각", True
        bad = self._bad_news(t)
        if bad:
            return bad, True
        if bid > 0 and bid <= t["stop"]:
            return "손절", True
        if bid > 0 and t["target"] and bid >= t["target"]:
            return "익절", False
        if now >= datetime.fromisoformat(t["sell_from"]):
            return "매도 구간", False
        return None

    def manage_exits(self, now: datetime, session: str, force_reason: str | None = None) -> None:
        exiting = {w.trade_id for w in self.working.values() if w.purpose == "exit"}
        for t in self.journal.open_trades():
            if t["status"] != "OPEN" or t["remaining"] <= 0 or t["id"] in exiting:
                continue
            late = (now - datetime.fromisoformat(t["deadline"])).total_seconds() / 60
            if late > self.tcfg.stale_alert_minutes and t["id"] not in self._stale_alerted:
                self._stale_alerted.add(t["id"])
                self.notifier.send(f"⚠️ {t['symbol']} 강제 청산 시각이 {late:.0f}분 지났는데 {t['remaining']}주 남음 "
                                   f"(호가 없음/거래정지 가능성) — 직접 확인하세요")
            try:
                b = self.ex.book(t["symbol"])
            except Exception as e:
                log.warning("호가 실패 %s: %s", t["symbol"], e)
                continue
            r = (force_reason, True) if force_reason else self._exit_reason(t, b.bid if b.has_quote else 0, now)
            if not r:
                continue
            reason, aggressive = r
            exhausted = self.exit_reprices.get(t["id"], 0) >= self.tcfg.max_exit_reprices
            ok, price = self._exit_price(b, aggressive, exhausted, session)
            if not ok:
                log.info("%s 호가 없음 → 청산 보류 (%s)", t["symbol"], session)
                continue
            # 같은 매매의 매수 미체결분은 먼저 취소 (팔면서 사지 않게)
            for key, w in list(self.working.items()):
                if w.trade_id == t["id"] and w.purpose == "entry":
                    self._cancel(key, "청산 시작", now)
            self.journal.set_exiting(t["id"], reason)
            self._submit(t["id"], t["symbol"], "SELL", t["remaining"], price, "exit", session, now, reason,
                         aggressive=aggressive)

    def flatten(self, now: datetime, session: str) -> None:
        self.manage_exits(now, session, force_reason="수동 전량 청산")
        if not self.journal.open_trades() and not self.working:
            self.control.update(flatten=False)

    # ---------- 진입 ----------
    def _regime_ok(self, ctx: Ctx) -> tuple[bool, str]:
        sym = self.tcfg.regime_symbol
        try:
            pc = self.mv.prev_close(sym, ctx.trade_date)
            b = self.ex.book(sym)
        except Exception as e:
            return True, f"시장 상태 확인 실패({e}) → 통과"
        if pc and b.mid and (b.mid / pc - 1) * 100 <= -self.tcfg.regime_drop_pct:
            return False, f"{sym} 데이마켓 {(b.mid / pc - 1) * 100:+.1f}%"
        return True, ""

    def _unrealized(self, open_trades: list[dict]) -> float:
        total = 0.0
        for t in open_trades:
            if not t["remaining"] or not t["entry_price"]:
                continue
            try:
                b = self.ex.book(t["symbol"])
                px = b.bid if b.has_quote else b.last
            except Exception:
                continue
            if px:
                total += (px - t["entry_price"]) * t["remaining"]
        return total

    def scan_and_enter(self, now: datetime, trade_date, session: str) -> None:
        day = self.cal.bounds(trade_date, DAY)
        if day and now < day[0] + timedelta(minutes=self.tcfg.day_open_wait_minutes):
            return    # 데이마켓 개장 직후는 호가가 얇다
        open_trades = self.journal.open_trades()
        ctx = Ctx(now, trade_date, session, self.cal, self.mv, self.tcfg, fund=self.fund,
                  held={t["symbol"] for t in open_trades})
        ok, why = self._regime_ok(ctx)
        self.status["regime"] = why or "정상"
        if not ok:
            log.info("신규 매수 중단: %s", why)
            return
        plans = []
        for name, strat in self.strategies.items():
            enabled, why = self.risk.strategy_enabled(name)
            if not enabled:
                continue
            try:
                plans += strat.scan(ctx)
            except Exception:
                log.exception("%s 스캔 실패", name)
        def row(p, ok, reason):
            return {"ts": now.isoformat(timespec="seconds"), "trade_date": str(trade_date), "session": session,
                    "strategy": p.strategy, "symbol": p.symbol, "ok": int(ok), "reason": reason,
                    "score": p.score, "price": p.price}

        rows = [row(p, p.ok, p.reason) for p in plans]
        acct = self.account()
        unrealized = self._unrealized(open_trades) if any(p.ok for p in plans) else 0.0
        for p in sorted((p for p in plans if p.ok), key=lambda p: p.score, reverse=True):
            block = self.risk.can_enter(p.strategy, p.symbol, str(trade_date), open_trades, unrealized)
            qty = 0 if block else self.risk.size(p.strategy, p.price, p.stop, acct)
            if not block and qty > 0:
                # 호가 잔량이 얇으면 수량을 줄인다: 최우선 매도 잔량 ≥ 주문의 min_book_ratio
                try:
                    b = self.ex.book(p.symbol)
                    cap = int(b.ask_size / self.tcfg.min_book_ratio) if b.ask_size else 0
                    qty = min(qty, cap)
                except Exception:
                    pass
            if block or qty <= 0:
                rows.append(row(p, False, f"리스크: {block or '수량 0 (호가 잔량 부족 등)'}"))
                continue
            tid = self.journal.open_trade(strategy=p.strategy, symbol=p.symbol, trade_date=str(trade_date),
                                          planned_qty=qty, stop=p.stop, target=p.target,
                                          sell_from=p.sell_from, deadline=p.deadline, reason=p.reason, now=now)
            self._submit(tid, p.symbol, "BUY", qty, p.price, "entry", session, now, p.reason)
            open_trades = self.journal.open_trades()
            acct.cash -= qty * p.price
        self.journal.log_signals(rows)
        self.status["last_scan"] = now.isoformat(timespec="seconds")
        self.status["candidates"] = sum(1 for p in plans if p.ok)

    def _submit(self, trade_id, symbol, side, qty, price, purpose, session, now, note, aggressive=False) -> None:
        try:
            o = self.ex.place(symbol, side, qty, price)
        except Exception as e:
            log.warning("주문 실패 %s %s: %s", side, symbol, e)
            if purpose == "entry":
                self.journal.entry_finished(trade_id)
            else:
                self.journal.exit_unfilled(trade_id)
            return
        self.journal.log_order(o, trade_id=trade_id, purpose=purpose, session=session, now=now, note=note)
        if o["status"] in TERMINAL and o["filled"] == 0:
            if purpose == "entry":
                self.journal.entry_finished(trade_id)
            else:
                self.journal.exit_unfilled(trade_id)
            return
        self.working[o["order_id"]] = Working(o["order_id"], trade_id, purpose, symbol, side, now, note=note,
                                              aggressive=aggressive, ids=[o["order_id"]])
        self.poll_orders(now, session)   # 즉시 체결분 반영

    # ---------- 계좌 ----------
    def account(self) -> Account:
        cash = self.ex.cash()
        value = sum(q * avg for q, avg in self.ex.positions().values())
        return Account(cash=cash, equity=cash + value)

    def snapshot(self, now: datetime, trade_date) -> None:
        try:
            cash = self.ex.cash()
            value = 0.0
            for sym, (q, avg) in self.ex.positions().items():
                try:
                    b = self.ex.book(sym)
                    value += q * (b.bid if b.has_quote else b.last or avg)
                except Exception:
                    value += q * avg
            realized = self.journal.realized_pnl(str(trade_date)) if trade_date else 0.0
            self.journal.snapshot_equity(cash, value, realized, now)
        except Exception as e:
            log.warning("계좌 스냅샷 실패: %s", e)

    def reconcile(self) -> None:
        try:
            pos = self.ex.positions()
        except Exception as e:
            log.warning("잔고 대조 실패: %s", e)
            return
        mine: dict[str, int] = {}
        for t in self.journal.open_trades():
            mine[t["symbol"]] = mine.get(t["symbol"], 0) + t["remaining"]
        diff = {s: (mine.get(s, 0), pos.get(s, (0, 0))[0]) for s in set(mine) | set(pos)
                if mine.get(s, 0) != pos.get(s, (0, 0))[0]}
        self.status["mismatch"] = {s: {"bot": a, "account": b} for s, (a, b) in diff.items()}
        if diff:
            log.warning("잔고 불일치 (봇 기록, 실제 계좌): %s", diff)
