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
from pathlib import Path
from datetime import date, datetime
from datetime import time as dtime
from datetime import timedelta, timezone

from ..sessions import AFTER, CLOSED, DAY, ET, KST, PRE, REGULAR, SessionCalendar
from .control import Control
from ..toss_api import TossApiError
from .exchange import TERMINAL, Exchange
from . import messages as msg
from .journal import Journal
from .notify import Notifier
from .risk import Account, Risk
from .strategies import ALL, FINANCING_WORDS, Ctx, Plan, events_of

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
    limit0: float | None = None                 # 처음 낸 지정가 (매수 재호가 허용폭 계산용)

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
    reports_dir: str | None = None   # 일일 리포트 파일 폴더 (None = 알림만)
    link: object = None          # () -> 대시보드 외부 주소 | None (터널이 켜져 있을 때만)

    def __post_init__(self):
        self.risk = Risk(self.tcfg, self.journal, self.control)
        self.strategies = {n: cls(self.tcfg.strategies[n]) for n, cls in ALL.items() if n in self.tcfg.strategies}
        self.working: dict[str, Working] = {}
        self.exit_reprices: dict[int, int] = {}
        self.entry_reprices: dict[int, int] = {}   # 매수 재호가 횟수 (거래 id → 횟수)
        self._fill_buf: dict[int, dict] = {}       # 거래 id → {qty, value, session, due} 부분 체결 알림 버퍼
        self.last_session = None
        self._next: dict[str, datetime] = {}
        # 수집기와 같이 켜면 첫 현재가 기록까지 1~2분 걸린다 → 켜자마자 "수집기 멈춤" 오경보를 내지 않게 5분 뒤부터 감시
        self._next["watchdog"] = self.clock() + timedelta(minutes=5)
        self.status: dict = {}
        self._stale_alerted: set[int] = set()
        self.mismatch: dict = {}
        self._exit_fail: dict[int, tuple[int, datetime]] = {}   # 매도 주문 실패 횟수·다음 시도 시각
        self._flat_ref: dict[int, tuple[float, datetime]] = {}  # 변동없음 청산용: (기준가, 그 가격이 된 시각)
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
            self.notifier.send(msg.warn("봇이 모르는 열린 주문",
                                        ", ".join(f"{o['symbol']} {'매수' if o['side'] == 'BUY' else '매도'} {o['qty']}주"
                                                  for o in unknown[:10]),
                                        "토스 앱에서 직접 확인하세요"))

    def _due(self, name: str, now: datetime, every: timedelta) -> bool:
        if now >= self._next.get(name, datetime.min.replace(tzinfo=timezone.utc)):
            self._next[name] = now + every
            return True
        return False

    # ---------- 틱 ----------
    def tick(self) -> None:
        flag = Path(self.journal.path).parent / "restart.flag"
        if flag.exists():
            flag.unlink(missing_ok=True)
            log.warning("설정 변경 → 봇 재시작 (run_forever)")
            import os
            os._exit(1)
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
        self._stage("fills", self.flush_entry_fills, now)
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
        if session != CLOSED and not ctl["kill"]:
            if self._due("scan", now, timedelta(seconds=getattr(self.tcfg, "scan_seconds", 0) or self.tcfg.scan_minutes * 60)):
                self._stage("scan", self.scan_and_enter, now, trade_date, session)
        if self._due("equity", now, timedelta(minutes=5)):
            self._stage("snapshot", self.snapshot, now, trade_date)
        if self._due("reconcile", now, timedelta(minutes=15)):
            self._stage("reconcile", self.reconcile)
        if self.tcfg.daily_report and self._due("report", now, timedelta(minutes=5)):
            self._stage("report", self.daily_report, now)
        if session != CLOSED and self._due("watchdog", now, timedelta(minutes=5)):
            self._stage("watchdog", self.watchdog, now, trade_date, session)
        self.status["mismatch"] = self.mismatch
        self.journal.beat("trader", self.status, now)

    def _stage(self, name: str, fn, *args):
        try:
            return fn(*args)
        except Exception as e:
            log.exception("%s 실패", name)
            self.notifier.send(msg.warn(f"매매 엔진 단계 실패 · {name}", f"{type(e).__name__}: {e}"[:300],
                                        "같은 오류는 30분에 한 번만 알립니다. 반복되면 state/logs/trade.log 확인"),
                               key=f"stage-{name}", every=1800)
            return None

    def run(self, sleep=time.sleep) -> None:
        log.info("매매 엔진 시작 (mode=%s)", self.journal.mode)
        while True:
            try:
                self.tick()
            except Exception as e:
                log.exception("틱 실패")
                self.notifier.send(msg.warn("매매 엔진 오류", f"{type(e).__name__}: {e}"[:300],
                                            "같은 오류는 30분에 한 번만 알립니다. 반복되면 state/logs/trade.log 확인"),
                                   key="tick-error", every=1800)
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
                if not self._reprice_entry(key, w, now, session):
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
        nm = self._name(w.symbol)
        if w.purpose == "entry":
            self.journal.record_entry_fill(w.trade_id, delta, px, session, now, fee=self._fee(delta, px))
            if self.tcfg.no_rebound_minutes and not (self.journal.trade(w.trade_id) or {}).get("no_rebound_at"):
                self.journal.set_no_rebound(w.trade_id, now + timedelta(minutes=self.tcfg.no_rebound_minutes))
            self._queue_entry_fill(w, delta, px, session, now)
            return
        t = self.journal.record_exit_fill(w.trade_id, delta, px, session, now, fee=self._fee(delta, px))
        if w.scale and not t["scaled"]:
            self.journal.set_scaled(w.trade_id, True, stop=t["entry_price"])   # 나머지는 본전 손절 + 트레일링
            self.notifier.send(msg.scaled_out(t, delta, px, self._info(t["symbol"]), name=nm))
        if t["status"] == "CLOSED":
            self.exit_reprices.pop(w.trade_id, None)
            self._flat_ref.pop(w.trade_id, None)
            self.notifier.send(msg.closed(self.journal.trade(w.trade_id), self._info(w.symbol), name=nm))

    def _queue_entry_fill(self, w: Working, delta: int, px: float, session: str, now: datetime) -> None:
        """부분 체결이 1주씩 나도 알림은 한 줄로: fill_flush_seconds 동안 모아서 보낸다.
        다 채워지면 기다리지 않고 바로 보낸다 (GCTK 50주 + 2주 → 알림 2개 문제)."""
        b = self._fill_buf.setdefault(w.trade_id, {"qty": 0, "value": 0.0, "session": session,
                                                   "due": now + self._flush_after()})
        b["qty"] += delta
        b["value"] += delta * px
        b["due"] = now + self._flush_after()
        if w.filled >= w.qty:
            self._flush_entry_fill(w.trade_id)

    def _flush_after(self) -> timedelta:
        return timedelta(seconds=getattr(self.tcfg, "fill_flush_seconds", 15) or 0)

    def flush_entry_fills(self, now: datetime) -> None:
        for tid, b in list(self._fill_buf.items()):
            if now >= b["due"]:
                self._flush_entry_fill(tid)

    def _flush_entry_fill(self, trade_id: int) -> None:
        b = self._fill_buf.pop(trade_id, None)
        if not b or not b["qty"]:
            return
        t = self.journal.trade(trade_id)
        avg = b["value"] / b["qty"]
        filled, planned = int(t["qty"] or 0), int(t["planned_qty"] or b["qty"])
        self.notifier.send(msg.buy_filled(t, b["qty"], avg, b["session"], self._info(t["symbol"], t.get("reason")),
                                          name=self._name(t["symbol"]), filled=filled, planned=planned))

    def _name(self, symbol: str) -> str | None:
        try:
            return self.mv.name(symbol) if hasattr(self.mv, "name") else None
        except Exception:
            return None

    def _info(self, symbol: str, reason: str | None = None) -> list[str]:
        """알림 아래 종목 정보: 오늘 등락·거래량·거래대금(토스 일봉), 공매도·대차·SSR·공시(수집기 DB),
        희석·부양 동기(이미 계산해 둔 것만 — 체결 처리 중에 SEC 를 새로 부르지 않는다). 실패해도 알림은 나간다."""
        try:
            stats = self.mv.day_stats(symbol) if hasattr(self.mv, "day_stats") else {}
            b = {}
            path = getattr(self.mv, "db_path", None)
            if path and path != ":memory:":
                from ..brief import brief
                from ..motive import _LIVE
                b = brief(path, symbol)
                hit = _LIVE.get((symbol.upper(), self.clock().date()))
                prof = self.fund._profile.get((symbol, self.clock().date())) if self.fund and hasattr(self.fund, "_profile") else None
                if hit:
                    b["filings"] = hit
                elif prof is not None:
                    b["filings"] = {"dilution_score": prof.score, "dilution_reasons": prof.reasons, "motives": []}
            return msg.info_block(stats, b, reason)
        except Exception as e:
            log.debug("알림 종목 정보 실패 %s: %s", symbol, e)
            return []

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

    def _reprice_entry(self, key: str, w: Working, now: datetime, session: str) -> bool:
        """매수 지정가가 TTL 안에 안 붙었을 때, 취소 대신 최우선 매도호가로 한 번 따라가 본다.

        프리마켓·데이마켓은 호가가 몇 초 만에 움직여서 중간가 지정가가 계속 비껴간다 (10/27 진입 8건 중 1건만 체결).
        추격은 `entry_chase_pct` 안에서만 하고, 호가가 그보다 더 뛰었거나 스프레드가 세션 상한을 넘으면 포기한다
        (급등 추격 금지·얇은 호가 회피는 그대로). 정정은 토스 규칙대로 '가격 정정' → 새 주문 id 로 줄기를 잇는다."""
        limit_n = getattr(self.tcfg, "entry_reprice_limit", 0) or 0
        if limit_n <= 0 or self.entry_reprices.get(w.trade_id, 0) >= limit_n:
            return False
        try:
            b = self.ex.book(w.symbol)
        except Exception as e:
            log.warning("매수 재호가용 호가 실패 %s: %s", w.symbol, e)
            return False
        if not b.has_quote or b.ask <= 0:
            return False
        cap = self.tcfg.spread_cap(session)
        if b.spread_pct > cap:
            log.info("매수 재호가 포기 %s: 스프레드 %.1f%% (상한 %.1f%%)", w.symbol, b.spread_pct, cap)
            return False
        chase = getattr(self.tcfg, "entry_chase_pct", 0.0) or 0.0
        if w.limit0 and b.ask > w.limit0 * (1 + chase / 100):
            log.info("매수 재호가 포기 %s: 매도호가 $%.4f > 처음 지정가 $%.4f +%.1f%%",
                     w.symbol, b.ask, w.limit0, chase)
            return False
        price = round(b.ask, 4)
        try:
            new_id = self.ex.modify(w.current, price)
        except Exception as e:
            log.warning("매수 정정 실패 %s (%s) → 취소", w.current, e)
            return False
        self.entry_reprices[w.trade_id] = self.entry_reprices.get(w.trade_id, 0) + 1
        w.ids.append(new_id)
        w.placed_at = now
        left = max(w.qty - w.filled, 0)
        self.journal.log_order({"order_id": new_id, "symbol": w.symbol, "side": w.side, "type": "LIMIT",
                                "qty": left, "price": price, "status": "OPEN", "filled": 0, "avg_price": None},
                               trade_id=w.trade_id, purpose=w.purpose, session=session, now=now,
                               note=f"매수 재호가 {self.entry_reprices[w.trade_id]}회 (매도호가 추종)")
        log.info("매수 정정 %s $%.4f → %s", w.symbol, price, new_id)
        return True

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
            r = self._exit_reason(t, b.bid if b.has_quote else 0, now, session)
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
        self.notifier.send(msg.backstop_fired(t, name=self._name(t["symbol"])))
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

    def _morning_cutoff(self, entry_ts_s: str) -> datetime:
        """한국 장 열기 전 전량 정리. flatten_kst(예: 08:50)가 있으면 그 시각(KST)까지, 없으면 예전처럼 ET 9시.
        매수가 그 시각을 이미 지난 뒤였으면 다음 날 시각 기준 (트레이드 날짜가 아니라 실제 매수 시각 기준)."""
        entry = datetime.fromisoformat(entry_ts_s)
        if getattr(self.tcfg, "flatten_kst", ""):
            h, m = (int(x) for x in self.tcfg.flatten_kst.split(":"))
            e = entry.astimezone(KST)
            cutoff = datetime.combine(e.date(), dtime(h, m), tzinfo=KST)
            return cutoff if cutoff > e else cutoff + timedelta(days=1)
        h, m = (int(x) for x in self.tcfg.morning_flatten_et.split(":"))
        e = entry.astimezone(ET)
        cutoff = datetime.combine(e.date(), dtime(h, m), tzinfo=ET)
        return cutoff if cutoff > e else cutoff + timedelta(days=1)

    def _volume_climax(self, t: dict, bid: float) -> bool:
        entry = t.get("entry_price") or 0
        if not bid or not entry or (bid / entry - 1) * 100 < 5:
            return False
        q = getattr(self.mv, "q", None)
        if q is None:
            return False
        try:
            rows = q("SELECT volume FROM bars_1m WHERE symbol=? AND trade_date=? AND ts<=? ORDER BY ts DESC LIMIT 21",
                     (t["symbol"], t["trade_date"], self.mv._asof()))
        except Exception:
            return False
        if len(rows) < 21:
            return False
        last = rows[0][0] or 0
        avg = sum(r[0] or 0 for r in rows[1:]) / 20
        return bool(avg) and last >= 10 * avg

    def _accumulating(self, symbol: str, now: datetime) -> bool:
        """최근 60분 안에 그 종목 매집·거래터짐 신호가 있었는지 (3시간 규칙의 '매집' 판정)."""
        fn = getattr(self.mv, "recent_flow", None)
        if fn is None:
            return False
        try:
            rows = fn(now - timedelta(minutes=60)) or []
        except Exception:
            return False
        return any((r.get("symbol") or symbol) == symbol for r in rows)

    def _kst_entry_blocked(self, now: datetime) -> bool:
        """08:20~08:50 KST 처럼 '청산 시간 확보' 구간에는 신규 진입을 막는다."""
        cut = getattr(self.tcfg, "entry_cutoff_kst", "")
        end_s = getattr(self.tcfg, "flatten_kst", "") or cut
        if not cut or not end_s:
            return False
        start = dtime(*(int(x) for x in cut.split(":")))
        end = dtime(*(int(x) for x in end_s.split(":")))
        t = now.astimezone(KST).time()
        return start <= t < end

    def _near_session_transition(self, now: datetime, trade_date_s: str) -> bool:
        """세션 전환 전후 몇 분은 토스 장통합(세션 간 데이터 전환) 때문에 호가가 순간적으로 튈 수 있다 —
        그 틈에 손절이 잘못 나가지 않게 이 구간에서는 손절만 보류한다 (익절·강제청산은 그대로)."""
        buf = self.tcfg.session_transition_buffer_minutes
        d = date.fromisoformat(trade_date_s)
        edges = set()
        for s in (DAY, PRE, REGULAR, AFTER):
            b = self.cal.bounds(d, s)
            if b:
                edges.update(b)
        return any(abs((now - e).total_seconds()) <= buf * 60 for e in edges)

    def _stale_exit(self, t: dict, bid: float, now: datetime, session: str) -> str | None:
        """체결가가 stale_exit_hours 동안 stale_move_pct % 안에서만 움직였으면 '변동없음'으로 정리
        (최대한 많이 매매해 보면서 어떤 전략이 되는지 보려는 실험 단계라, 안 움직이는 자리에 계속 묶여 있지 않게).
        데이마켓은 원래 몇 시간씩 조용히 가다가 프리마켓에서 움직이는 구조라(오버나이트 전략들의 전제) 그동안은
        예외 — 그 조용함까지 '변동없음'으로 잘라버리면 오버나이트 전략 자체가 성립하지 않는다."""
        if bid <= 0 or session == DAY:
            return None
        ref = self._flat_ref.get(t["id"])
        if not ref or abs(bid / ref[0] - 1) * 100 > self.tcfg.stale_move_pct:
            self._flat_ref[t["id"]] = (bid, now)
            return None
        if (now - ref[1]) >= timedelta(hours=self.tcfg.stale_exit_hours):
            return "변동없음 청산"
        return None

    def _dumping(self, t: dict) -> bool:
        """최근 5분이 '물량 던지기'(거래량·거래대금이 함께 터진 음봉)인지.

        거래량만 많고 거래대금이 안 늘면(싼 가격에 주식 수만 많은 경우) 던지기로 보지 않는다.
        거래량도 거래대금도 마른 하락은 매집으로 본다."""
        q = getattr(self.mv, "q", None)
        if q is None:
            return False
        try:
            rows = q("SELECT open, close, volume FROM bars_1m WHERE symbol=? AND trade_date=? AND ts<=? "
                     "ORDER BY ts DESC LIMIT 66", (t["symbol"], t["trade_date"], self.mv._asof()))
        except Exception:
            return False
        if len(rows) < 21:
            return False
        recent, base = rows[:5], rows[5:]
        base_vol = sum((r[2] or 0) for r in base) / len(base)
        base_dol = sum((r[1] or 0) * (r[2] or 0) for r in base) / len(base)
        now_vol = sum((r[2] or 0) for r in recent) / len(recent)
        now_dol = sum((r[1] or 0) * (r[2] or 0) for r in recent) / len(recent)
        if not base_vol or not base_dol:
            return False
        reds = sum(1 for r in recent if r[0] is not None and r[1] is not None and r[1] < r[0])
        return (now_vol >= self.tcfg.dump_vol_x * base_vol
                and now_dol >= self.tcfg.dump_dollar_x * base_dol and reds >= 3)

    def _decline_exit(self, t: dict, bid: float, now: datetime) -> str | None:
        """물량 던지기(거래량·거래대금이 함께 터진 음봉)로 밀릴 때만 청산한다.

        거래량·거래대금이 마른 하락은 매집으로 보고 들고 간다. 2026-10-28~30 실제 기록에서 이렇게 잘라낸
        22건이 전부 청산 직후 1시간 안에 반등했다 ('수급 없는 하락' 9건 평균 +10.9%, '반등·매집 없음' 13건 평균 +2.1%).
        낙폭이 dump_min_dd_pct 보다 작으면(고점 근처 횡보) 하락으로 보지 않는다."""
        if bid <= 0:
            return None
        hw = max(t.get("high_water") or 0, bid)
        if hw and (bid / hw - 1) * 100 > -self.tcfg.dump_min_dd_pct:
            return None                                # 고점 근처 — 하락이 아니다
        if self._accumulating(t["symbol"], now):
            return None                                # 수급이 붙는 중
        if not self._dumping(t):
            return None                                # 거래량·대금 마른 하락 = 매집
        return "물량 던지기 청산"

    def _exit_reason(self, t: dict, bid: float, now: datetime, session: str) -> tuple[str, bool, int] | None:
        """(사유, 공격적 매도 여부, 수량). 수량은 보통 남은 전량, 분할 익절이면 일부."""
        left = t["remaining"]
        if now >= datetime.fromisoformat(t["deadline"]):
            return "강제 청산 시각", True, left
        bad = self._bad_news(t)
        if bad:
            return bad, True, left
        scaled, trail = bool(t.get("scaled")), t.get("trail_pct") or 0
        if bid > 0 and bid <= t["stop"] and not self._near_session_transition(now, t["trade_date"]):
            return ("본전 손절" if scaled else "손절"), True, left
        th = getattr(self.tcfg, "trail_from_high_pct", 0) or 0
        if bid > 0 and th and (t.get("entry_price") or 0):
            hw = max(t.get("high_water") or 0, bid)
            min_gain = getattr(self.tcfg, "trail_min_gain_pct", 0) or 0
            armed = hw >= t["entry_price"] * (1 + min_gain / 100)
            if armed and bid <= hw * (1 - th / 100):
                return f"고점 대비 −{th:g}% 익절", False, left
        skip_day = getattr(self.tcfg, "no_rebound_skip_day", False) and t.get("entry_session") == DAY
        if self._volume_climax(t, bid):
            return "거래량 클라이맥스", False, left
        if bid > 0 and t.get("no_rebound_at") and now >= datetime.fromisoformat(t["no_rebound_at"]) and not skip_day:
            entry = t.get("entry_price") or 0
            hw = max(t.get("high_water") or 0, bid)
            # 시간이 지났어도 '거래량·거래대금이 실린 던지기'일 때만 자른다 — 마른 하락은 매집이라 들고 간다
            if (entry and hw < entry * (1 + self.tcfg.no_rebound_high_pct / 100)
                    and not self._accumulating(t["symbol"], now) and self._dumping(t)):
                return f"반등·매집 없음 ({(self.tcfg.no_rebound_minutes or 0) / 60:g}시간)", False, left
        hw = max(t.get("high_water") or 0, bid)
        if scaled and trail and bid > 0 and bid <= hw * (1 - trail / 100):
            return f"트레일링 스톱 (고점 ${hw:.4f} −{trail:g}%)", True, left
        if not scaled and bid > 0 and t["target"] and bid >= t["target"]:
            q = int(t["qty"] * (t.get("scale_out") or 1.0))
            if 0 < q < left:
                return SCALE_REASON, False, q
            return "익절", False, left
        stale = self._stale_exit(t, bid, now, session)
        if stale:
            return stale, False, left
        decline = self._decline_exit(t, bid, now)
        if decline:
            return decline, False, left
        # 분할 익절 후 나머지는 트레일링으로 강제 청산 시각까지 들고 간다 (매도 구간·9시 정리 둘 다 안 먹힘)
        if not (scaled and trail):
            if now >= datetime.fromisoformat(t["sell_from"]):
                return "매도 구간", False, left
            if now >= self._morning_cutoff(t["entry_ts"]):
                return "장 마감 전 정리", False, left   # 전략 매도 시각이 늦게 잡혀도 이 시각엔 무조건 정리
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
                self.notifier.send(msg.warn(f"청산 지연 · {t['symbol']}",
                                            f"강제 청산 시각이 {late:.0f}분 지났는데 {t['remaining']}주 남음 (호가 없음·거래정지 가능성)",
                                            "토스 앱에서 직접 확인하세요"))
            try:
                b = self.ex.book(t["symbol"])
            except Exception as e:
                log.warning("호가 실패 %s: %s", t["symbol"], e)
                continue
            if b.has_quote and b.bid > (t["high_water"] or 0):
                self.journal.update_high(t["id"], b.bid)
            r = (force_reason, True, t["remaining"]) if force_reason else \
                self._exit_reason(t, b.bid if b.has_quote else 0, now, session)
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

    def _add_ons(self, now: datetime, trade_date, session: str) -> None:
        if not getattr(self.tcfg, "add_on", False):
            return
        ctl = self.control.load()
        if ctl["kill"] or ctl["flatten"]:
            return
        window = int(getattr(self.tcfg, "add_on_window_minutes", 10))
        hot = {e["symbol"] for e in self.mv.recent_flow(now - timedelta(minutes=window))}
        if not hot:
            return
        acct = self.account()
        trades = self.journal.open_trades()
        counts: dict[str, int] = {}
        for t in trades:
            counts[t["symbol"]] = counts.get(t["symbol"], 0) + 1
        for t in trades:
            sym = t["symbol"]
            if t["status"] != "OPEN" or (t["remaining"] or 0) <= 0 or sym not in hot:
                continue
            if counts.get(sym, 0) > int(getattr(self.tcfg, "add_on_max_per_symbol", 1)):
                continue
            if sym in (ctl.get("blocked_symbols") or []):
                continue
            ok, _ = self.risk.strategy_enabled(t["strategy"])
            if not ok:
                continue
            try:
                b = self.ex.book(sym)
            except Exception:
                continue
            if not b.has_quote or not t["entry_price"]:
                continue
            gain = (b.mid / t["entry_price"] - 1) * 100
            if gain < self.tcfg.add_on_min_gain_pct:
                continue
            price = b.ask if b.spread_pct <= 1.0 else min(b.ask, b.mid * 1.002)
            qty = int((t["qty"] or 0) * self.tcfg.add_on_qty_ratio)
            if price > 0:
                qty = min(qty, int(acct.cash // price))
            if qty <= 0:
                continue
            sl = getattr(self.tcfg, "stop_loss_pct_override", 0) or 50.0
            tp = getattr(self.tcfg, "take_profit_pct_override", 0) or 20.0
            reason = f"추가매수 (기존 +{gain:.1f}%)"
            tid = self.journal.open_trade(strategy=t["strategy"], symbol=sym, trade_date=str(trade_date),
                                          planned_qty=qty, stop=price * (1 - sl / 100), target=price * (1 + tp / 100),
                                          sell_from=datetime.fromisoformat(t["sell_from"]),
                                          deadline=datetime.fromisoformat(t["deadline"]), reason=reason, now=now,
                                          scale_out=t.get("scale_out") or 1.0, trail_pct=t.get("trail_pct") or 0.0,
                                          session=session, context={"add_on_of": t["id"]})
            self._submit(tid, sym, "BUY", qty, price, "entry", session, now, reason)
            acct.cash -= qty * price
            counts[sym] = counts.get(sym, 0) + 1

    def scan_and_enter(self, now: datetime, trade_date, session: str) -> None:
        if self._kst_entry_blocked(now):
            self.status["last_scan"] = now.isoformat(timespec="seconds")
            return
        day = self.cal.bounds(trade_date, DAY)
        if day and now < day[0] + timedelta(minutes=self.tcfg.day_open_wait_minutes):
            return    # 데이마켓 개장 직후는 호가가 얇다
        open_trades = self.journal.open_trades()
        ctx = Ctx(now, trade_date, session, self.cal, self.mv, self.tcfg, fund=self.fund,
                  held={t["symbol"] for t in open_trades})
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
        # 전략마다 매수 가능 세션이 다르다 (runner_overnight 류는 데이마켓에만 사서 그날 밤 판다 — 다른 세션에
        # 사면 보유 기간 가정이 안 맞음; scalp 는 세션 상관없이 실시간 수급 신호로 바로 들어갔다 바로 턴다).
        # 지금 세션에 못 사는 전략의 후보는 스캔은 계속 돌리되(=`지금 봇이 보는 것` 실시간 유지) '매수 시간 아님'으로만 표시.
        def buy_window_ok(strategy_name: str) -> bool:
            strat = self.strategies.get(strategy_name)
            if not strat or session not in strat.SESSIONS:
                return False
            return self._entry_window(now, trade_date) if session == DAY else True

        blocked_by_session = {(p.strategy, p.symbol) for p in plans if p.ok and not buy_window_ok(p.strategy)}
        if blocked_by_session:
            from .strategies import SESSION_KO
            rows = [row(p, False, f"{p.reason} — 매수 시간 아님 ({SESSION_KO.get(session, session)})")
                   if (p.strategy, p.symbol) in blocked_by_session else row(p, p.ok, p.reason) for p in plans]
            plans = [p for p in plans if (p.strategy, p.symbol) not in blocked_by_session]
        best_by_symbol: dict[str, Plan] = {}
        for p in plans:
            if p.ok and (p.symbol not in best_by_symbol or p.score > best_by_symbol[p.symbol].score):
                best_by_symbol[p.symbol] = p
        losers = {id(p) for p in plans if p.ok and best_by_symbol[p.symbol] is not p}
        if losers:
            rows = [row(p, False, f"{p.reason} — 같은 종목은 {best_by_symbol[p.symbol].strategy} 전략이 선택")
                    if id(p) in losers else row(p, p.ok, p.reason) for p in plans]
            plans = [p for p in plans if id(p) not in losers]
        if not any(p.ok for p in plans):
            self._add_ons(now, trade_date, session)
            self.journal.log_signals(rows)
            self.status["last_scan"] = now.isoformat(timespec="seconds")
            self.status["candidates"] = sum(1 for p in plans if p.ok)
            return
        acct = self.account()
        unrealized = self._unrealized(open_trades) if any(p.ok for p in plans) else 0.0
        for p in sorted((p for p in plans if p.ok), key=lambda p: p.score, reverse=True):
            sp = self.tcfg.strategies.get(p.strategy) or {}
            sl = sp.get("stop_loss_pct_override") or getattr(self.tcfg, "stop_loss_pct_override", 0)
            tp = sp.get("take_profit_pct_override") or getattr(self.tcfg, "take_profit_pct_override", 0)
            if sl:
                p.stop = p.price * (1 - sl / 100)
            if tp:
                p.target = p.price * (1 + tp / 100)
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
            if getattr(self.tcfg, "signal_alerts", True):
                self.notifier.send(msg.signal(p.strategy, p.symbol, p.price, p.reason, session, p.score,
                                              name=self._name(p.symbol)),
                                   key=f"sig-{p.strategy}-{p.symbol}", every=self.tcfg.signal_alert_minutes * 60)
            tid = self.journal.open_trade(strategy=p.strategy, symbol=p.symbol, trade_date=str(trade_date),
                                          planned_qty=qty, stop=p.stop, target=p.target, session=session,
                                          sell_from=p.sell_from, deadline=p.deadline, reason=p.reason, now=now,
                                          scale_out=p.scale_out, trail_pct=p.trail_pct,
                                          context={**p.context, "planned_qty": qty, "entry_limit": p.price})
            self._submit(tid, p.symbol, "BUY", qty, p.price, "entry", session, now, p.reason)
            open_trades = self.journal.open_trades()
            acct.cash -= qty * p.price
        self._add_ons(now, trade_date, session)
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
                                              aggressive=aggressive, qty=qty, scale=scale, ids=[o["order_id"]],
                                              limit0=price)
        self.poll_orders(now, session)   # 즉시 체결분 반영

    def _order_failed(self, trade_id: int, purpose: str, symbol: str, now: datetime, why: str) -> None:
        if purpose == "entry":
            self.journal.entry_finished(trade_id)
            return
        self.journal.exit_unfilled(trade_id)
        n = self._exit_fail.get(trade_id, (0, now))[0] + 1
        self._exit_fail[trade_id] = (n, now + timedelta(seconds=min(30 * 2 ** (n - 1), 600)))
        if n >= 3:
            self.notifier.send(msg.warn(f"매도 실패 · {symbol}", f"{n}번 연속 실패 ({why})",
                                        "잔고·거래정지를 확인하세요. 봇은 간격을 늘려 계속 시도합니다"),
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
            self.notifier.send(msg.warn("수집기 멈춤", f"수집기가 {what}습니다. 전략이 오래된 데이터로 판단하게 됩니다",
                                        "pennybot 수집기 창을 확인하세요 (꺼졌으면 start.bat)"),
                               key="collector-stale", every=3600)

    def dashboard_url(self) -> str | None:
        try:
            return self.link() if self.link else None
        except Exception:
            return None

    def status_text(self) -> str:
        from .strategies import SESSION_KO
        st = self.status
        mode = "실거래" if st.get("mode") == "live" else "모의매매"
        lines = [f"📋 상태 · {mode} · {SESSION_KO.get(st.get('session'), st.get('session') or '-')}",
                 f"영업일 {st.get('trade_date') or '-'} · 걸린 주문 {st.get('working', 0)}건"]
        if self.control.load()["kill"]:
            lines.append("⛔ 신규 매수 중단 상태 (/재개 로 켜기)")
        held = self.journal.open_trades()
        status_ko = {"PENDING": "매수 주문 중", "OPEN": "보유", "EXITING": "매도 주문 중"}
        for t in held:
            lines.append(f"• {t['symbol']} {status_ko.get(t['status'], t['status'])} {t['remaining']}주 @ {msg.px(t['entry_price'])}"
                         f" · 손절 {msg.px(t['stop'])}" + (" · 안전망" if t["backstop_id"] else ""))
        if not held:
            lines.append("보유 종목 없음")
        return msg.with_link("\n".join(lines), self.dashboard_url())

    def _completed_day(self, now: datetime):
        """정규장이 이미 끝난 가장 최근 영업일 (그날 매매가 끝난 날)."""
        done = [d for d, day in self.cal.days.items() if REGULAR in day.sessions and day.sessions[REGULAR][1] <= now]
        return max(done, default=None)

    def daily_report(self, now: datetime) -> None:
        """영업일이 끝났고 아직 안 보냈으면 리포트 (봇이 그 시각에 꺼져 있었어도 켜지면 보낸다)."""
        day = self._completed_day(now)
        last = self.journal.get_meta("last_report")
        if day is None or (last and last >= day.isoformat()):
            return
        hh, mm = (int(x) for x in self.tcfg.daily_report_time_kst.split(":"))
        end = self.cal.days[day].sessions[REGULAR][1].astimezone(KST)
        send_at = end.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if send_at < end - timedelta(hours=12):
            send_at += timedelta(days=1)
        if now.astimezone(KST) < send_at and last:       # 처음 켠 날은 기다리지 않고 바로
            return
        text = self.report_text(day)
        if self.reports_dir:
            from .report import build, save
            save(build(self.journal, day.isoformat(), now, self.mismatch), text, self.reports_dir)
        self.notifier.send(msg.with_link(text, self.dashboard_url()))
        self.journal.set_meta("last_report", day.isoformat())

    def report_text(self, trade_date=None) -> str:
        from .report import build, render
        td = trade_date or self._completed_day(self.clock())
        if td is None:
            return "아직 끝난 영업일이 없습니다."
        return render(build(self.journal, str(td), self.clock(), self.mismatch))

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
            self.notifier.send(msg.warn("잔고 불일치",
                                        "봇 기록 / 실제 계좌: " + ", ".join(f"{s} {a}/{b}주" for s, (a, b) in diff.items()),
                                        "봇 기록이 더 많은 종목이 있어 신규 매수를 멈춥니다"
                                        if any(a > b for a, b in diff.values()) else "직접 산 종목이면 무시해도 됩니다"),
                               key="mismatch-" + ",".join(sorted(diff)), every=3600)
