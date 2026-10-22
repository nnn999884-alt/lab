"""메인 매매 루프."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from .brokers.base import Broker
from .config import Config
from .models import Order, OrderStatus, OrderType, Position, Side
from .risk import RiskManager
from .screener import load_watchlist
from .strategy import ET, PennyMomentumStrategy, is_market_hours

log = logging.getLogger(__name__)


class Engine:
    def __init__(self, cfg: Config, broker: Broker, clock=lambda: datetime.now(timezone.utc)):
        self.cfg = cfg
        self.broker = broker
        self.strategy = PennyMomentumStrategy(cfg.strategy)
        self.risk = RiskManager(cfg.risk, cfg.state_dir)
        self.clock = clock
        self.meta: dict[str, dict] = {}  # 심볼별 진입시각/손절가/고점 (브로커가 모르는 정보)
        self.exited_today: dict[str, str] = {}  # 당일 청산 종목 재진입 금지 {심볼: 날짜}

    # ---------- 주문 실행 ----------
    def _execute(self, order: Order, wait_sec: float = 15) -> Order:
        """지정가 주문 후 wait_sec 동안 체결 대기, 미체결분은 취소."""
        order = self.broker.place_order(order)
        deadline = time.time() + wait_sec
        while order.status in (OrderStatus.PENDING, OrderStatus.PARTIAL) and time.time() < deadline:
            time.sleep(1)
            order = self.broker.get_order(order.order_id)
        if order.status in (OrderStatus.PENDING, OrderStatus.PARTIAL):
            self.broker.cancel_order(order.order_id)
            order = self.broker.get_order(order.order_id)
        return order

    def _positions(self) -> dict[str, Position]:
        positions = self.broker.get_positions()
        for sym, p in positions.items():
            m = self.meta.setdefault(sym, {"entry_time": self.clock(), "high_water": p.avg_price})
            p.entry_time = m["entry_time"]
            p.high_water = m.get("high_water", p.avg_price)
            p.meta = m
        return positions

    # ---------- 한 틱 ----------
    def tick(self) -> None:
        now = self.clock()
        self.risk.roll_day(now.astimezone(ET).date())
        positions = self._positions()

        # 1) 청산 먼저
        for sym, pos in positions.items():
            try:
                q = self.broker.get_quote(sym)
                reason = self.strategy.check_exit(pos, q, now)
                self.meta[sym]["high_water"] = pos.high_water
                if not reason:
                    continue
                limit = q.bid * (1 - self.cfg.risk.exit_slippage_pct / 100)
                o = self._execute(Order(sym, Side.SELL, pos.qty, OrderType.LIMIT, limit))
                if o.filled_qty > 0:
                    pnl = (o.avg_fill_price - pos.avg_price) * o.filled_qty
                    self.risk.record_exit(pnl)
                    log.info("청산 %s x%d (%s) PnL %+.2f", sym, o.filled_qty, reason, pnl)
                    if o.filled_qty >= pos.qty:
                        self.meta.pop(sym, None)
                        self.exited_today[sym] = self.risk.day
            except Exception:
                log.exception("청산 처리 실패: %s", sym)

        # 2) 신규 진입 — auto 면 스크리너 결과(점수 순), 아니면 수동 목록
        positions = self._positions()
        picks = load_watchlist(self.cfg.state_dir, now.astimezone(ET).date()) \
            if self.cfg.auto_watchlist else {}
        symbols = list(picks) if self.cfg.auto_watchlist else self.cfg.watchlist
        for sym in symbols:
            if sym in positions or self.exited_today.get(sym) == self.risk.day:
                continue
            ok, why = self.risk.can_open(len(positions))
            if not ok:
                log.debug("진입 불가: %s", why)
                break
            try:
                q = self.broker.get_quote(sym)
                bars = self.broker.get_bars(sym, 1, 120)
                if sym in picks:
                    pk = picks[sym]
                    sig = self.strategy.check_pivot_entry(q, bars, now, pk["pivot"],
                                                          pk.get("avg_volume", 0.0))
                else:
                    sig = self.strategy.check_entry(q, bars, now)
                log.debug("%s: %s", sym, sig.reason)
                if not sig.ok:
                    continue
                limit = q.ask * (1 + self.cfg.risk.entry_slippage_pct / 100)
                qty = self.risk.size(limit, sig.stop_price, self.broker.get_cash())
                if qty <= 0:
                    continue
                log.info("진입 신호 %s: %s → %d주 @ ≤%.4f", sym, sig.reason, qty, limit)
                o = self._execute(Order(sym, Side.BUY, qty, OrderType.LIMIT, limit))
                if o.filled_qty > 0:
                    self.risk.record_entry()
                    self.meta[sym] = {"entry_time": now, "stop_price": sig.stop_price,
                                      "high_water": o.avg_fill_price}
                    positions = self._positions()
            except Exception:
                log.exception("진입 처리 실패: %s", sym)

    def run(self) -> None:
        src = "스크리너(auto)" if self.cfg.auto_watchlist else f"수동 {len(self.cfg.watchlist)}종목"
        log.info("봇 시작 (mode=%s, 감시종목=%s)", self.cfg.mode, src)
        while True:
            if is_market_hours(self.clock()):
                self.tick()
            else:
                log.debug("장 외 시간 대기")
            time.sleep(self.cfg.poll_seconds)
