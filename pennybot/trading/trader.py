"""세션 매매 엔진: 데이마켓에 사고 그날 밤(프리마켓~정규장 개장 직후)에 판다.

매 틱(기본 5초):
  1. 주문 상태 확인 → 체결을 trades 에 반영, 매수 미체결 TTL 취소, 매도 재호가
  2. 세션이 바뀌면 걸려 있는 주문을 모두 취소 (토스 DAY 주문은 세션을 넘어 이어지므로 직접 정리)
  3. 보유 종목 청산 판단: 강제 청산 시각 > 악재 공시 > 손절 > 트레일링 > 익절(분할) > 매도 시작 시각
  4. 데이마켓이면 전략 스캔 → 리스크 → 지정가 매수
  5. 계좌 스냅샷, 하트비트, 잔고 대조
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..sessions import AFTER, CLOSED, DAY, KST, REGULAR, SessionCalendar
from .control import Control
from ..toss_api import TossApiError
from .exchange import TERMINAL, Exchange
from .journal import Journal
from .notify import Notifier
from .risk import Account, Risk
from .strategies import ALL, FINANCING_WORDS, Ctx, events_of

# 들고 있는 동안 이런 공시가 나오면 즉시 청산 (급등주 최대 위험: 밤사이 증자 발표)
HOLD_ALARM = FINANCING_WORDS | {"DELISTING_NOTICE", "OFFERING_FILED", "SHELF_FILED", "WARRANT_INDUCEMENT",
                                "WARRANT_REPRICE", "CONVERTIBLE", "GOING_CONCERN"}   # 공시·보도자료 모두

SCALE_REASON = "익절(분할)"

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
    qty: int = 0                # 줄기 전체 주문 수량
    scale: bool = False         # 분할 익절 주문
    checked_at: datetime | None = None   # 마지막 REST 주문 조회
    final: set = field(default_factory=set)     # 더 바뀌지 않는 id (체결 완료·취소·정정됨)
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
    stream: object = None        # TossStream: 내 주문 이벤트가 오면 그 주문만 조회 (없거나 끊기면 매 틱 REST)

    def __post_init__(self):
        self.risk = Risk(self.tcfg, self.journal, self.control)
        self.strategies = {n: cls(self.tcfg.strategies[n]) for n, cls in ALL.items() if n in self.tcfg.strategies}
        self.working: dict[str, Working] = {}
        self.exit_reprices: dict[int, int] = {}
        self.last_session = None
        self._next: dict[str, datetime] = {}
        self.status: dict = {}
        self._stale_alerted: set[int] = set()
        self.mismatch: dict = {}
        self._exit_fail: dict[int, tuple[int, datetime]] = {}   # 매도 주문 실패 횟수·다음 시도 시각
        self._recover()

    # ---------- 공통 ----------
    def _recover(self) -> None:
        """재시작 시 아직 열려 있는 주문을 다시 추적한다 (같은 모드의 주문만: 모의 주문을 실계좌에 조회하지 않게).
        같은 매매·용도의 열린 주문은 한 줄기로 묶고(정정 이력), 주문 없이 멈춘 매매 상태는 되돌린다."""
        groups: dict[tuple[int, str], list[dict]] = {}
        for o in self.journal.q("""SELECT o.* FROM orders o JOIN trades t ON o.trade_id = t.id
                                   WHERE o.status='OPEN' AND t.mode=? ORDER BY o.ts, o.rowid""",
                                (self.journal.mode,)):
            groups.setdefault((o["trade_id"], o["purpose"]), []).append(o)
        for (tid, purpose), rows in groups.items():
            first, last = rows[0], rows[-1]
            t = self.journal.trade(tid)
            reason = t["exit_reason"] or ""
            w = Working(first["order_id"], tid, purpose, first["symbol"], first["side"],
                        datetime.fromisoformat(last["ts"]), note=first["note"] or "", qty=first["qty"] or 0,
                        scale=purpose == "exit" and SCALE_REASON in (first["note"] or ""),
                        aggressive=purpose == "exit" and not reason.startswith(("매도 구간", "익절", SCALE_REASON)),
                        ids=[r["order_id"] for r in rows])
            # 이미 매매 기록에 반영된 체결 = 이 줄기의 시작값 (다시 세지 않게)
            w.fills = {r["order_id"]: (r["filled"] or 0, r["avg_price"] or 0.0) for r in rows}
            w.filled = sum(f for f, _ in w.fills.values())
            w.avg = sum(f * a for f, a in w.fills.values()) / w.filled if w.filled else 0.0
            self.working[w.order_id] = w
            if purpose == "exit":
                n = self.journal.q("SELECT COUNT(*) AS n FROM orders WHERE trade_id=? AND purpose='exit'", (tid,))[0]["n"]
                self.exit_reprices[tid] = max(n - 1, 0)
        busy = {(w.trade_id, w.purpose) for w in self.working.values()}
        for t in self.journal.open_trades():
            if t["status"] == "EXITING" and (t["id"], "exit") not in busy:
                self.journal.exit_unfilled(t["id"])          # 매도 주문 없이 멈춤 → 다시 청산 판단
            elif t["status"] == "PENDING" and (t["id"], "entry") not in busy:
                self.journal.entry_finished(t["id"])         # 매수 주문 없이 멈춤 → 취소 처리
        try:
            tracked = {i for w in self.working.values() for i in w.ids}
            unknown = [o for o in self.ex.open_orders() if o["order_id"] not in tracked]
        except Exception as e:
            log.debug("열린 주문 조회 실패: %s", e)
            unknown = []
        if unknown:
            self.notifier.send("⚠️ 봇이 모르는 열린 주문: " + ", ".join(f"{o['symbol']} {o['side']} {o['qty']}주"
                                                                  for o in unknown[:10]) + " — 직접 확인하세요")

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
        if self.stream is not None:
            self.stream.pin({w.symbol for w in self.working.values()} |
                            {t["symbol"] for t in self.journal.open_trades()})
        self.poll_orders(now, session)

        if self.last_session is not None and session != self.last_session:
            log.info("세션 변경 %s → %s: 걸린 주문 모두 취소", self.last_session, session)
            self.cancel_all(now, "세션 변경")
            self.poll_orders(now, session)
            if session == AFTER and trade_date:
                self.notifier.send(self.report_text(trade_date))   # 정규장 마감 = 그 영업일 매매 끝
        self.last_session = session
        self.status = {"session": session, "trade_date": str(trade_date) if trade_date else None,
                       "mode": self.journal.mode, "working": len(self.working)}
        if self.stream is not None:
            self.status["stream"] = "연결" if self.stream.healthy else f"끊김 → REST ({self.stream.last_error})"[:120]

        # 단계마다 따로 예외 처리: 한 단계(안전망·스캔 등)가 계속 실패해도 청산은 돈다
        ctl = self._stage("control", self.control.load) or {"kill": True, "flatten": False}
        if session != CLOSED:
            if ctl["flatten"]:
                self._stage("flatten", self.flatten, now, session)
            self._stage("exits", self.manage_exits, now, session)
        self._stage("backstop", self.sync_backstops, now, session)
        if session == DAY and not ctl["kill"] and self._entry_window(now, trade_date):
            if self._due("scan", now, timedelta(minutes=self.tcfg.scan_minutes)):
                self._stage("scan", self.scan_and_enter, now, trade_date, session)
        if self._due("equity", now, timedelta(minutes=5)):
            self._stage("snapshot", self.snapshot, now, trade_date)
        if self._due("reconcile", now, timedelta(minutes=15)):
            self._stage("reconcile", self.reconcile)
        if session != CLOSED and self._due("watchdog", now, timedelta(minutes=5)):
            self._stage("watchdog", self.watchdog, now, trade_date, session)
        self.status["mismatch"] = self.mismatch
        self.journal.beat("trader", self.status, now)

    def _stage(self, name: str, fn, *args):
        try:
            return fn(*args)
        except Exception as e:
            log.exception("%s 실패", name)
            self.notifier.send(f"⚠️ {name} 실패: {type(e).__name__}: {e}"[:300], key=f"stage-{name}", every=1800)
            return None

    def run(self, sleep=time.sleep) -> None:
        log.info("매매 엔진 시작 (mode=%s)", self.journal.mode)
        while True:
            try:
                self.tick()
            except Exception as e:
                log.exception("틱 실패")
                self.notifier.send(f"⚠️ 매매 엔진 오류: {type(e).__name__}: {e}"[:300], key="tick-error", every=1800)
            for _ in range(10):          # 5초 간격, 주문 이벤트가 오면 바로 다음 틱
                if self.stream is not None and self.stream.has_events():
                    break
                sleep(0.5)

    def _entry_window(self, now: datetime, trade_date) -> bool:
        b = self.cal.bounds(trade_date, DAY)
        return bool(b) and now < b[1] - timedelta(minutes=self.tcfg.entry_cutoff_minutes)

    # ---------- 주문 추적 ----------
    def _changed_orders(self) -> set[str] | None:
        """웹소켓으로 이벤트가 온 주문 id. None = 전부 REST 로 확인 (스트림 없음·끊김·재연결 직후)."""
        if self.stream is None or not getattr(self.stream, "orders_healthy", self.stream.healthy) \
                or self.stream.take_resync():
            return None
        return {str(o.get("orderId")) for _, o in self.stream.order_events()}

    def _poll_due(self, w: Working, now: datetime, changed: set[str]) -> bool:
        if changed & set(w.ids + [w.order_id, w.cancel_id or ""]) or w.checked_at is None:
            return True
        if (now - w.checked_at).total_seconds() >= self.tcfg.order_poll_fallback_seconds:
            return True             # 이벤트를 놓쳤을 수도 있으니 주기적으로는 REST 로 확인
        age = (now - w.placed_at).total_seconds()
        if w.cancel_sent:
            return bool(w.cancel_at and (now - w.cancel_at).total_seconds() > 60)
        return age > (self.tcfg.entry_ttl_seconds if w.purpose == "entry" else self.tcfg.exit_reprice_seconds)

    def poll_orders(self, now: datetime, session: str) -> None:
        changed = self._changed_orders()
        for key, w in list(self.working.items()):
            if changed is not None and not self._poll_due(w, now, changed):
                continue
            w.checked_at = now
            try:
                cur, got = self._refresh(w, now)
            except Exception as e:
                log.warning("주문 조회 실패 %s: %s", w.current, e)
                continue
            total = sum(f for f, _ in w.fills.values())
            delta = total - w.filled
            if delta > 0:
                value = sum(f * a for f, a in w.fills.values())
                px = (value - w.avg * w.filled) / delta
                w.filled, w.avg = total, value / total
                self._on_fill(w, delta, px, session, now)
            for o in got.values():
                self.journal.update_order(o, now)
            if w.purpose == "exit" and cur["status"] not in TERMINAL and \
                    self.journal.trade(w.trade_id)["remaining"] <= 0:
                self._cancel(key, "이미 다 팔림", now)      # 남은 주문 정리, 더 따라가지 않는다
                self.working.pop(key, None)
                continue

            done = cur["status"] in TERMINAL
            if cur["status"] == "REPLACED" and w.current in w.final:
                # 정정은 됐는데 이어받은 주문 id 를 모름 (정정 응답 유실 등) → 열린 주문에서 찾는다
                succ = self._find_orphan(w.symbol, w.side)
                if succ:
                    log.warning("정정 후속 주문 찾음 %s → %s", w.current, succ)
                    w.ids.append(succ)
                    w.placed_at = now
                    continue
                done = True
            if not done and w.cancel_sent and w.cancel_id:
                try:
                    c = self.ex.order(w.cancel_id)
                    if c["status"] in ("OP_REJECTED", "REJECTED"):
                        log.warning("취소 거부 %s → 다시 시도", w.current)
                        w.cancel_sent, w.cancel_id = False, None
                    elif c["status"] == "CANCELED" and w.cancel_at and (now - w.cancel_at).total_seconds() > 60:
                        done = True
                except Exception as e:
                    log.debug("취소 주문 조회 실패 %s: %s", w.cancel_id, e)
            if done:
                self.working.pop(key)
                if w.purpose == "entry":
                    self.journal.entry_finished(w.trade_id)
                else:
                    self.journal.exit_unfilled(w.trade_id)
                    if w.scale:
                        self.exit_reprices.pop(w.trade_id, None)   # 나머지 물량은 재호가 횟수를 새로 센다
                continue
            if cur["status"] == "REPLACED" or w.cancel_sent:
                continue          # 정정·취소 처리 중
            age = (now - w.placed_at).total_seconds()
            if w.purpose == "entry" and age > self.tcfg.entry_ttl_seconds:
                self._cancel(key, "매수 미체결 TTL", now)
            elif w.purpose == "exit" and age > self.tcfg.exit_reprice_seconds:
                self._reprice(key, w, now, session)

    def _refresh(self, w: Working, now: datetime) -> tuple[dict, dict]:
        """줄기의 끝나지 않은 id 를 모두 조회해 체결을 갱신한다 (정정 직전 원주문 체결도 놓치지 않게).
        → (현재 주문, 조회한 주문들)"""
        got = {}
        for oid in list(w.ids):
            if oid in w.final and oid != w.current:
                continue
            o = self.ex.order(oid)
            if o["status"] == "OP_REJECTED":
                # 정정 '요청' 거부 기록: 줄기에서 빼고 원주문을 계속 따라간다
                log.warning("정정 거부 %s → 원주문 유지", oid)
                w.ids.remove(oid)
                w.fills.pop(oid, None)
                self.journal.update_order(o, now)
                continue
            got[oid] = o
            w.fills[oid] = (o["filled"], o["avg_price"] or 0.0)
            if o["status"] in TERMINAL or o["status"] == "REPLACED":
                w.final.add(oid)
        return got.get(w.current) or self.ex.order(w.current), got

    def _find_orphan(self, symbol: str, side: str) -> str | None:
        """거래소에 있는데 봇 기록에 없는 같은 종목·방향의 최근 주문 (이미 체결된 것도 포함).
        응답을 못 받은 주문·정정의 실제 주문을 찾는 데 쓴다."""
        try:
            orders = self.ex.recent_orders(symbol)
        except Exception as e:
            log.debug("최근 주문 조회 실패 %s: %s", symbol, e)
            return None
        known = {i for w in self.working.values() for i in w.ids} | \
            {r["order_id"] for r in self.journal.q("SELECT order_id FROM orders WHERE symbol=?", (symbol,))}
        cutoff = self.clock() - timedelta(minutes=10)
        cands = [o for o in orders if o["side"] == side and o["order_id"] not in known
                 and o["status"] not in ("REPLACED", "OP_REJECTED")
                 and not (o["status"] == "REJECTED" and not o["filled"])
                 and (not o.get("ts") or datetime.fromisoformat(o["ts"]) >= cutoff)]
        return cands[-1]["order_id"] if cands else None

    def _on_fill(self, w: Working, delta: int, px: float, session: str, now: datetime) -> None:
        if w.purpose == "entry":
            self.journal.record_entry_fill(w.trade_id, delta, px, session, now, fee=self._fee(delta, px))
            self.notifier.send(f"🟢 매수 체결 {w.symbol} {delta}주 @ ${px:.4f}")
            return
        t = self.journal.record_exit_fill(w.trade_id, delta, px, session, now, fee=self._fee(delta, px))
        if w.scale and not t["scaled"]:
            self.journal.set_scaled(w.trade_id, True, stop=t["entry_price"])   # 나머지는 본전 손절 + 트레일링
            self.notifier.send(f"🟡 분할 익절 {t['symbol']} {delta}주 @ ${px:.4f} → 나머지 {t['remaining']}주 트레일링")
        if t["status"] == "CLOSED":
            self.exit_reprices.pop(w.trade_id, None)
            self.notifier.send(f"🔵 청산 {t['symbol']} ({t['exit_reason']}) "
                               f"{t['pnl']:+.2f}$ ({t['pnl_pct']:+.1f}%)")

    def _fee(self, qty: int, px: float) -> float:
        return qty * px * self.tcfg.fee_pct / 100

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
        if not w.aggressive:
            # 느긋한 매도(매도 구간·익절) 중에 손절·강제 청산·악재가 오면 공격적으로 바꾼다
            t = self.journal.trade(w.trade_id)
            r = self._exit_reason(t, b.bid if b.has_quote else 0, now)
            if r and r[1]:
                w.aggressive = True
                self.journal.set_exiting(w.trade_id, r[0])
                if w.scale:      # 절반만 팔던 주문 → 취소하고 다음 틱에 전량 매도
                    self._cancel(key, f"분할 익절 중 {r[0]} → 전량 매도로 전환", now)
                    return
        ok, price = self._exit_price(b, w.aggressive, count >= self.tcfg.max_exit_reprices, session)
        if not ok:
            w.placed_at = now          # 호가가 없으면 기다렸다가 다시
            return
        self.exit_reprices[w.trade_id] = count
        try:
            new_id = self.ex.modify(w.current, price)
        except TossApiError as e:
            if e.status and 400 <= e.status < 500:
                log.info("정정 거부 %s (%s) → 취소 후 재주문", w.current, e)
                self._cancel(key, "매도 재호가", now)
            else:        # 응답을 못 받음: 정정이 됐을 수도 있다 → 다음 조회에서 REPLACED 면 후속 주문을 찾는다
                log.warning("정정 응답 없음 %s (%s)", w.current, e)
                w.placed_at = now
            return
        except Exception as e:
            log.warning("정정 응답 없음 %s (%s)", w.current, e)
            w.placed_at = now
            return
        w.ids.append(new_id)
        w.placed_at = now
        left = max(min(w.qty - w.filled, self.journal.trade(w.trade_id)["remaining"]), 0)
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

    # ---------- 서버 손절 안전망 ----------
    def _backstop_target(self, t: dict) -> tuple[int, float, float]:
        trig = t["stop"] * (1 - self.tcfg.backstop_buffer_pct / 100)
        return t["remaining"], trig, trig * (1 - self.tcfg.backstop_slippage_pct / 100)

    def sync_backstops(self, now: datetime, session: str) -> None:
        """보유 중인 매매마다 토스 조건주문(손절가 아래 매도)을 유지한다. 발동되면 그 주문을 이어받아 체결을 기록."""
        if not self.tcfg.backstop:
            return
        busy = {w.trade_id for w in self.working.values()}
        check = self._due("backstop", now, timedelta(seconds=self.tcfg.backstop_check_seconds))
        for t in self.journal.open_trades():
            sid = t["backstop_id"]
            if sid and check and not self._check_backstop(t, now, session):
                sid = None
            if t["status"] != "OPEN" or t["remaining"] <= 0 or t["id"] in busy or not t["stop"]:
                continue
            qty, trig, lim = self._backstop_target(t)
            if sid and t["backstop_qty"] == qty and abs((t["backstop_trigger"] or 0) - trig) <= trig * 0.005:
                continue
            if sid and not self._cancel_backstop(t, now, session):
                continue
            expire = (datetime.fromisoformat(t["deadline"]).astimezone(KST).date() + timedelta(days=1)).isoformat()
            try:
                new = self.ex.place_stop(t["symbol"], qty, trig, lim, expire)
            except Exception as e:
                log.warning("서버 손절 안전망 등록 실패 %s: %s", t["symbol"], e)
                continue
            self.journal.set_backstop(t["id"], new, qty, trig)
            log.info("서버 손절 안전망 %s %d주 감시가 $%.4f", t["symbol"], qty, trig)

    def _check_backstop(self, t: dict, now: datetime, session: str) -> bool:
        """→ 아직 살아 있으면 True."""
        try:
            st = self.ex.stop_status(t["backstop_id"])
        except Exception as e:
            log.debug("조건주문 조회 실패 %s: %s", t["backstop_id"], e)
            return True
        if st.get("order_id"):
            self._adopt_backstop(t, st["order_id"], now, session)
            return False
        if st.get("status") in ("EXPIRED", "CANCELED", "COMPLETED"):
            self.journal.set_backstop(t["id"], None)
            return False
        return True

    def _cancel_backstop(self, t: dict, now: datetime, session: str) -> bool:
        """→ 취소됐으면 True. 이미 발동됐으면 그 주문을 이어받고 False (봇이 또 팔지 않게)."""
        try:
            self.ex.cancel_stop(t["backstop_id"])
        except Exception as e:
            if self._check_backstop(t, now, session):
                log.warning("서버 손절 안전망 취소 실패 %s: %s", t["symbol"], e)
            return False
        self.journal.set_backstop(t["id"], None)
        return True

    def _adopt_backstop(self, t: dict, order_id: str, now: datetime, session: str) -> None:
        self.journal.set_backstop(t["id"], None)
        if any(order_id in w.ids for w in self.working.values()):
            return
        try:
            o = self.ex.order(order_id)
        except Exception:
            o = {"order_id": order_id, "symbol": t["symbol"], "side": "SELL", "type": "LIMIT",
                 "qty": t["remaining"], "price": None, "status": "OPEN", "filled": 0, "avg_price": None}
        reason = "서버 손절 안전망 발동"
        self.journal.set_exiting(t["id"], reason)
        self.journal.log_order(o, trade_id=t["id"], purpose="exit", session=session, now=now, note=reason)
        self.working[order_id] = Working(order_id, t["id"], "exit", t["symbol"], "SELL", now, note=reason,
                                         aggressive=True, qty=o["qty"], ids=[order_id])
        self.notifier.send(f"🛑 {t['symbol']} 서버 손절 안전망 발동 — 봇이 이어서 추적합니다")
        self.poll_orders(now, session)

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

    def _exit_reason(self, t: dict, bid: float, now: datetime) -> tuple[str, bool, int] | None:
        """(사유, 공격적 매도 여부, 수량). 수량은 보통 남은 전량, 분할 익절이면 일부."""
        left = t["remaining"]
        if now >= datetime.fromisoformat(t["deadline"]):
            return "강제 청산 시각", True, left
        bad = self._bad_news(t)
        if bad:
            return bad, True, left
        scaled, trail = bool(t.get("scaled")), t.get("trail_pct") or 0
        if bid > 0 and bid <= t["stop"]:
            return ("본전 손절" if scaled else "손절"), True, left
        hw = max(t.get("high_water") or 0, bid)
        if scaled and trail and bid > 0 and bid <= hw * (1 - trail / 100):
            return f"트레일링 스톱 (고점 ${hw:.4f} −{trail:g}%)", True, left
        if not scaled and bid > 0 and t["target"] and bid >= t["target"]:
            q = int(t["qty"] * (t.get("scale_out") or 1.0))
            if 0 < q < left:
                return SCALE_REASON, False, q
            return "익절", False, left
        if now >= datetime.fromisoformat(t["sell_from"]) and not (scaled and trail):
            return "매도 구간", False, left      # 분할 익절 후 나머지는 트레일링으로 강제 청산 시각까지 들고 간다
        return None

    def manage_exits(self, now: datetime, session: str, force_reason: str | None = None) -> None:
        exiting = {w.trade_id for w in self.working.values() if w.purpose == "exit"}
        for t in self.journal.open_trades():
            if t["status"] != "OPEN" or t["remaining"] <= 0 or t["id"] in exiting:
                continue
            fail = self._exit_fail.get(t["id"])
            if fail and now < fail[1] and not force_reason:
                continue                     # 매도가 계속 거부되면 잠시 쉬었다가 (주문 도배 방지)
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
            if b.has_quote and b.bid > (t["high_water"] or 0):
                self.journal.update_high(t["id"], b.bid)
            r = (force_reason, True, t["remaining"]) if force_reason else \
                self._exit_reason(t, b.bid if b.has_quote else 0, now)
            if not r:
                continue
            reason, aggressive, qty = r
            exhausted = self.exit_reprices.get(t["id"], 0) >= self.tcfg.max_exit_reprices
            ok, price = self._exit_price(b, aggressive, exhausted, session)
            if not ok:
                log.info("%s 호가 없음 → 청산 보류 (%s)", t["symbol"], session)
                continue
            if t["backstop_id"] and not self._cancel_backstop(t, now, session):
                continue                     # 안전망이 이미 발동 → 그 주문을 따라간다
            # 같은 매매의 매수 미체결분은 먼저 취소 (팔면서 사지 않게)
            for key, w in list(self.working.items()):
                if w.trade_id == t["id"] and w.purpose == "entry":
                    self._cancel(key, "청산 시작", now)
            self.journal.set_exiting(t["id"], reason)
            self._submit(t["id"], t["symbol"], "SELL", qty, price, "exit", session, now, reason,
                         aggressive=aggressive, scale=reason == SCALE_REASON)

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

    def _warning(self, symbol: str) -> str | None:
        """토스 매수 유의사항 (VI 발동·투자경고·정리매매 등). 조회 실패면 통과."""
        fn = getattr(self.mv, "warnings", None)
        if fn is None:
            return None
        try:
            w = fn(symbol)
        except Exception as e:
            log.debug("유의사항 조회 실패 %s: %s", symbol, e)
            return None
        return ("토스 매수 유의: " + ", ".join(w)) if w else None

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
                total += min((px - t["entry_price"]) * t["remaining"], 0.0)   # 다른 종목 이익으로 손실을 가리지 않게
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
        if any(v["bot"] > v["account"] for v in self.mismatch.values()):
            rows += [row(p, False, "잔고 불일치 → 신규 매수 중단") for p in plans if p.ok]
            plans = [p for p in plans if not p.ok]
        acct = self.account()
        unrealized = self._unrealized(open_trades) if any(p.ok for p in plans) else 0.0
        for p in sorted((p for p in plans if p.ok), key=lambda p: p.score, reverse=True):
            block = self.risk.can_enter(p.strategy, p.symbol, str(trade_date), open_trades, unrealized, now) \
                or self._warning(p.symbol)
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
                                          sell_from=p.sell_from, deadline=p.deadline, reason=p.reason, now=now,
                                          scale_out=p.scale_out, trail_pct=p.trail_pct)
            self._submit(tid, p.symbol, "BUY", qty, p.price, "entry", session, now, p.reason)
            open_trades = self.journal.open_trades()
            acct.cash -= qty * p.price
        self.journal.log_signals(rows)
        self.status["last_scan"] = now.isoformat(timespec="seconds")
        self.status["candidates"] = sum(1 for p in plans if p.ok)

    def _submit(self, trade_id, symbol, side, qty, price, purpose, session, now, note, aggressive=False,
                scale=False) -> None:
        try:
            o = self.ex.place(symbol, side, qty, price)
        except Exception as e:
            log.warning("주문 실패 %s %s: %s", side, symbol, e)
            # 응답만 못 받고 실제로는 접수됐을 수 있다 → 열린 주문에서 찾아 이어받는다
            oid = None if isinstance(e, TossApiError) and 400 <= (e.status or 0) < 500 else \
                self._find_orphan(symbol, side)
            if not oid:
                self._order_failed(trade_id, purpose, symbol, now, str(e))
                return
            log.warning("응답 없던 주문을 찾음 %s %s → %s", side, symbol, oid)
            o = self.ex.order(oid)
        self.journal.log_order(o, trade_id=trade_id, purpose=purpose, session=session, now=now, note=note)
        if o["status"] in TERMINAL and o["filled"] == 0:
            self._order_failed(trade_id, purpose, symbol, now, o["status"])
            return
        if purpose == "exit":
            self._exit_fail.pop(trade_id, None)
        self.working[o["order_id"]] = Working(o["order_id"], trade_id, purpose, symbol, side, now, note=note,
                                              aggressive=aggressive, qty=qty, scale=scale, ids=[o["order_id"]])
        self.poll_orders(now, session)   # 즉시 체결분 반영

    def _order_failed(self, trade_id: int, purpose: str, symbol: str, now: datetime, why: str) -> None:
        if purpose == "entry":
            self.journal.entry_finished(trade_id)
            return
        self.journal.exit_unfilled(trade_id)
        n = self._exit_fail.get(trade_id, (0, now))[0] + 1
        self._exit_fail[trade_id] = (n, now + timedelta(seconds=min(30 * 2 ** (n - 1), 600)))
        if n >= 3:
            self.notifier.send(f"⚠️ {symbol} 매도 주문이 {n}번 연속 실패 ({why}). 잔고·거래정지를 확인하세요",
                               key=f"exit-fail-{trade_id}", every=1800)

    # ---------- 감시·보고 ----------
    def watchdog(self, now: datetime, trade_date, session: str) -> None:
        """수집기가 멈추면 전략이 오래된 데이터로 판단하게 된다 → 알림."""
        b = self.cal.bounds(trade_date, session) if trade_date else None
        if not b or (now - b[0]).total_seconds() < 900:
            return                          # 세션 시작 직후는 아직 수집 전일 수 있다
        last = self.mv.last_collect_ts() if hasattr(self.mv, "last_collect_ts") else None
        age = (now - last).total_seconds() / 60 if last else None
        self.status["collector_age_min"] = round(age, 1) if age is not None else None
        if age is None or age > 10:
            what = "한 번도 기록하지 않았" if age is None else f"{age:.0f}분째 멈춰 있"
            self.notifier.send(f"⚠️ 수집기가 {what}습니다. python -m pennybot collect 창을 확인하세요.",
                               key="collector-stale", every=3600)

    def status_text(self) -> str:
        st = self.status
        lines = [f"[{st.get('mode', '-')}] {st.get('trade_date') or '-'} {st.get('session', '-')}"
                 f" · 걸린 주문 {st.get('working', 0)} · 시장 {st.get('regime', '-')}"]
        if self.control.load()["kill"]:
            lines.append("⛔ 신규 매수 중단 상태")
        held = self.journal.open_trades()
        for t in held:
            lines.append(f"{t['symbol']} {t['status']} {t['remaining']}주 @ ${t['entry_price'] or 0:.4f}"
                         f" 손절 ${t['stop']:.4f}" + (" · 안전망" if t["backstop_id"] else ""))
        if not held:
            lines.append("보유 종목 없음")
        return "\n".join(lines)

    def report_text(self, trade_date=None) -> str:
        td = str(trade_date or self.status.get("trade_date") or "")
        rows = self.journal.q("""SELECT symbol, pnl, pnl_pct, exit_reason FROM trades
                                 WHERE status='CLOSED' AND trade_date=? AND mode=? ORDER BY id""",
                              (td, self.journal.mode))
        nets = [r["pnl"] or 0 for r in rows]          # pnl 은 이미 수수료 차감
        lines = [f"📊 {td} 결과: {len(rows)}건, 승 {sum(1 for n in nets if n > 0)}, "
                 f"순손익 {sum(nets):+.2f}$ (수수료 차감)"]
        lines += [f"  {r['symbol']} {r['pnl_pct']:+.1f}% {n:+.2f}$ ({r['exit_reason']})"
                  for r, n in list(zip(rows, nets))[:15]]
        left = self.journal.open_trades()
        if left:
            lines.append("⚠️ 아직 보유: " + ", ".join(f"{t['symbol']} {t['remaining']}주" for t in left))
        return "\n".join(lines)

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
        # 봇이 최근 매매한 종목만 비교 (계좌의 다른 보유 종목은 사용자 것)
        recent = {r["symbol"] for r in self.journal.q(
            "SELECT DISTINCT symbol FROM trades WHERE mode=? AND created >= ?",
            (self.journal.mode, (self.clock() - timedelta(days=3)).astimezone(timezone.utc).isoformat(timespec="seconds")))}
        diff = {s: (mine.get(s, 0), pos.get(s, (0, 0))[0]) for s in set(mine) | (recent & set(pos))
                if mine.get(s, 0) != pos.get(s, (0, 0))[0]}
        self.mismatch = {s: {"bot": a, "account": b} for s, (a, b) in diff.items()}
        if diff:
            log.warning("잔고 불일치 (봇 기록, 실제 계좌): %s", diff)
            self.notifier.send("⚠️ 잔고 불일치 (봇 기록 / 실제 계좌): " +
                               ", ".join(f"{s} {a}/{b}주" for s, (a, b) in diff.items()) +
                               (" — 봇 기록이 더 많은 종목이 있어 신규 매수를 멈춥니다"
                                if any(a > b for a, b in diff.values()) else ""),
                               key="mismatch-" + ",".join(sorted(diff)), every=3600)
