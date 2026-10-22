"""리스크: 진입 허용 여부와 수량.

  - 킬스위치 / 전략 끄기 (control.json) / 성적 나쁜 전략 자동 정지
  - 영업일 손실 한도, 전체·전략별 동시 보유 수, 같은 종목 하루 1회
  - 수량 = min(전략 예산 / 전략 최대 보유 수, 계좌 × 종목 비중, 종목당 상한, 현금, 손절 시 손실 한도)
"""
from __future__ import annotations

from dataclasses import dataclass

from .control import Control
from .journal import Journal


@dataclass
class Account:
    cash: float
    equity: float


class Risk:
    def __init__(self, tcfg, journal: Journal, control: Control):
        self.t, self.j, self.ctl = tcfg, journal, control

    def strategy_enabled(self, name: str) -> tuple[bool, str]:
        params = self.t.strategies.get(name, {})
        if not params.get("enabled", True):
            return False, "설정에서 꺼짐"
        if not self.ctl.strategy_on(name):
            return False, "대시보드에서 꺼짐"
        n = self.t.auto_disable_trades
        st = self.j.strategy_stats(name, last_n=n)
        if st and st[0]["trades"] >= n and st[0]["expectancy"] < 0:
            return False, f"자동 정지: 최근 {n}회 평균 손익 ${st[0]['expectancy']:.2f}"
        return True, ""

    def can_enter(self, strategy: str, symbol: str, trade_date: str, open_trades: list[dict]) -> str | None:
        c = self.ctl.load()
        if c["kill"]:
            return "킬스위치"
        if c["flatten"]:
            return "전량 청산 중"
        ok, why = self.strategy_enabled(strategy)
        if not ok:
            return why
        if self.j.realized_pnl(trade_date) <= -self.t.max_daily_loss_usd:
            return "영업일 손실 한도 도달"
        if len(open_trades) >= self.t.max_open_positions:
            return "전체 동시 보유 한도"
        if sum(1 for t in open_trades if t["strategy"] == strategy) >= self.t.strategies[strategy]["max_positions"]:
            return "전략 동시 보유 한도"
        if any(t["symbol"] == symbol for t in open_trades) or self.j.traded_today(symbol, trade_date):
            return "이미 보유/오늘 매매한 종목"
        return None

    def size(self, strategy: str, price: float, stop: float, acct: Account) -> int:
        p = self.t.strategies[strategy]
        budget = acct.equity * p["budget_pct"] / p["max_positions"]
        dollars = min(budget, acct.equity * self.t.max_position_pct, self.t.max_position_usd, acct.cash * 0.98)
        qty = int(dollars // price) if price > 0 else 0
        risk = price - stop
        if risk > 0:
            remaining_loss = self.t.max_daily_loss_usd  # 손절 1번이 계좌의 risk_per_trade 를 넘지 않게
            qty = min(qty, int(min(acct.equity * self.t.risk_per_trade_pct, remaining_loss) // risk))
        return max(qty, 0)
