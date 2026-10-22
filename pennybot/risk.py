"""리스크 관리: 포지션 크기, 일일 손실 한도, 거래 횟수 제한, 킬스위치."""
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

from .config import RiskConfig

log = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, cfg: RiskConfig, state_dir: str = "state"):
        self.cfg = cfg
        self.path = Path(state_dir) / "risk_state.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.kill_file = Path(state_dir) / "KILL"
        self.day = date.today().isoformat()
        self.realized_pnl = 0.0
        self.trades = 0
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            s = json.loads(self.path.read_text(encoding="utf-8"))
            if s.get("day") == self.day:
                self.realized_pnl = s.get("realized_pnl", 0.0)
                self.trades = s.get("trades", 0)

    def _save(self) -> None:
        self.path.write_text(json.dumps(
            {"day": self.day, "realized_pnl": self.realized_pnl, "trades": self.trades}), encoding="utf-8")

    def roll_day(self, today: date) -> None:
        if today.isoformat() != self.day:
            self.day, self.realized_pnl, self.trades = today.isoformat(), 0.0, 0
            self._save()

    @property
    def killed(self) -> bool:
        """state/KILL 파일이 있으면 신규진입 중단."""
        return self.kill_file.exists()

    def can_open(self, open_positions: int) -> tuple[bool, str]:
        if self.killed:
            return False, "킬스위치 활성 (state/KILL)"
        if self.realized_pnl <= -self.cfg.max_daily_loss_usd:
            return False, f"일일 손실한도 도달 ${self.realized_pnl:.2f}"
        if self.trades >= self.cfg.max_trades_per_day:
            return False, "일일 거래횟수 한도"
        if open_positions >= self.cfg.max_open_positions:
            return False, "최대 보유종목 수"
        return True, ""

    def size(self, price: float, stop_price: float, cash: float) -> int:
        """포지션 금액 상한과 손절 시 손실(남은 일일한도의 1/2)로 수량을 제한."""
        budget = min(self.cfg.max_position_usd, cash * 0.95)
        qty = int(budget // price) if price > 0 else 0
        risk_per_share = price - stop_price
        remaining = self.cfg.max_daily_loss_usd + self.realized_pnl
        if risk_per_share > 0:
            qty = min(qty, int((remaining * 0.5) // risk_per_share))
        return max(qty, 0)

    def record_entry(self) -> None:
        self.trades += 1
        self._save()

    def record_exit(self, pnl: float) -> None:
        self.realized_pnl += pnl
        self._save()
        log.info("실현손익 %+.2f → 당일 누적 $%.2f", pnl, self.realized_pnl)
