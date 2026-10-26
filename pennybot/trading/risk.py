"""리스크: 진입 허용 여부와 수량.

  - 킬스위치 / 전략 끄기 (control.json) / 성적 나쁜 전략 자동 정지
  - 영업일 손실 한도, 최근 7일 손실 한도, 연속 손실 후 쉬기, 전체·전략별 동시 보유 수, 같은 종목 하루 1회
  - 수량 = min(전략 예산 / 전략 최대 보유 수, 계좌 × 종목 비중, 종목당 상한, 현금, 손절 시 손실 한도)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .control import Control
from .journal import Journal, now_iso as _utc_iso


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

    def cooldown(self, now: datetime | None) -> str | None:
        """연속 손실·주간 손실로 쉬는 중이면 사유."""
        if now is None:
            return None
        n = self.t.loss_streak_stop
        if n:
            last = self.j.q("""SELECT pnl, exit_ts FROM trades WHERE status='CLOSED' AND mode=?
                               ORDER BY exit_ts DESC LIMIT ?""", (self.j.mode, n))
            if len(last) == n and all((r["pnl"] or 0) < 0 for r in last):
                until = datetime.fromisoformat(last[0]["exit_ts"]) + timedelta(hours=self.t.loss_streak_cooldown_hours)
                if now < until:
                    return f"{n}연속 손실 → {self.t.loss_streak_cooldown_hours}시간 쉬는 중"
        if self.t.max_weekly_loss_usd:
            wk = self.j.q("""SELECT COALESCE(SUM(pnl),0) AS p FROM trades WHERE status='CLOSED' AND mode=?
                             AND exit_ts >= ?""", (self.j.mode, _utc_iso(now - timedelta(days=7))))[0]["p"]
            if wk <= -self.t.max_weekly_loss_usd:
                return f"최근 7일 손실 한도 (${wk:.0f})"
        return None

    def can_enter(self, strategy: str, symbol: str, trade_date: str, open_trades: list[dict],
                  unrealized: float = 0.0, now: datetime | None = None) -> str | None:
        c = self.ctl.load()
        if c["kill"]:
            return "킬스위치"
        if c["flatten"]:
            return "전량 청산 중"
        if symbol.upper() in c["blocked_symbols"]:
            return "종목 금지됨"
        ok, why = self.strategy_enabled(strategy)
        if not ok:
            return why
        # 실현 손실 + 보유 종목 평가 손실 (이익은 아직 내 돈이 아니므로 더하지 않는다)
        if self.j.realized_pnl(trade_date) + min(unrealized, 0.0) <= -self.t.max_daily_loss_usd:
            return "영업일 손실 한도 도달 (평가 손실 포함)"
        cd = self.cooldown(now)
        if cd:
            return cd
        if len(open_trades) >= self.t.max_open_positions:
            return "전체 동시 보유 한도"
        if sum(1 for t in open_trades if t["strategy"] == strategy) >= self.t.strategies[strategy]["max_positions"]:
            return "전략 동시 보유 한도"
        if any(t["symbol"] == symbol for t in open_trades) or self.j.traded_today(symbol, trade_date):
            return "이미 보유/오늘 매매한 종목"
        if self.t.max_unfilled_entries and self.j.unfilled_entries_today(symbol, trade_date) >= self.t.max_unfilled_entries:
            return f"오늘 {self.t.max_unfilled_entries}번 미체결(호가 얇음) → 오늘은 그만"
        return None

    def size(self, strategy: str, price: float, stop: float, acct: Account) -> int:
        p = self.t.strategies[strategy]
        budget = acct.equity * p["budget_pct"] / p["max_positions"]
        dollars = min(budget, acct.equity * self.t.max_position_pct, self.t.max_position_usd, acct.cash * 0.98)
        qty = int(dollars // price) if price > 0 else 0
        # 1주당 위험 = 손절폭과 '밤사이 최악의 갭' 중 큰 쪽. 급등주는 손절가를 건너뛰어 열리기 때문
        risk = max(price - stop, price * p.get("gap_risk_pct", 0) / 100)
        if risk > 0:
            max_loss = min(acct.equity * self.t.risk_per_trade_pct, self.t.max_daily_loss_usd)
            qty = min(qty, int(max_loss // risk))
        return max(qty, 0)
