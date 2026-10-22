"""설정 로딩: config.yaml + 환경변수(.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv


@dataclass
class StrategyConfig:
    min_price: float = 0.50          # 너무 싼 종목(서브페니) 제외
    max_price: float = 5.00          # 페니주 상한
    min_gap_pct: float = 10.0        # 전일 대비 최소 상승률
    min_day_volume: float = 1_000_000
    min_dollar_volume: float = 1_000_000
    max_spread_pct: float = 2.0      # 호가 스프레드 상한
    opening_range_minutes: int = 15  # 개장 후 고가 돌파 기준 구간
    rvol_lookback_bars: int = 20
    min_bar_rvol: float = 2.0        # 직전 봉 거래량 / 평균
    stop_loss_pct: float = 5.0
    take_profit_pct: float = 12.0
    trailing_stop_pct: float = 4.0
    max_hold_minutes: int = 120
    no_new_entries_after: str = "15:00"   # 미 동부시간
    flatten_at: str = "15:50"             # 장 마감 전 전량 청산


@dataclass
class RiskConfig:
    max_position_usd: float = 200.0
    max_open_positions: int = 3
    max_daily_loss_usd: float = 100.0
    max_trades_per_day: int = 10
    entry_slippage_pct: float = 0.5   # 지정가 = ask * (1 + x%)
    exit_slippage_pct: float = 1.0    # 지정가 = bid * (1 - x%)


@dataclass
class TossConfig:
    base_url: str = "https://openapi.tossinvest.com"
    token_path: str = "/oauth2/token"
    quote_path: str = "/v1/quotes/{symbol}"
    candles_path: str = "/v1/candles/{symbol}"
    positions_path: str = "/v1/accounts/positions"
    cash_path: str = "/v1/accounts/balance"
    orders_path: str = "/v1/orders"
    order_path: str = "/v1/orders/{order_id}"
    market: str = "US"
    client_id: str = ""
    client_secret: str = ""
    account: str = ""


@dataclass
class Config:
    mode: str = "paper"              # paper | live
    watchlist: list[str] = field(default_factory=list)
    poll_seconds: int = 20
    paper_cash: float = 2000.0
    state_dir: str = "state"
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    toss: TossConfig = field(default_factory=TossConfig)

    @property
    def is_live(self) -> bool:
        return self.mode == "live"


def load_config(path: str | Path = "config.yaml") -> Config:
    load_dotenv()
    raw: dict = {}
    p = Path(path)
    if p.exists():
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    cfg = Config(
        mode=raw.get("mode", "paper"),
        watchlist=[s.upper() for s in raw.get("watchlist", [])],
        poll_seconds=int(raw.get("poll_seconds", 20)),
        paper_cash=float(raw.get("paper_cash", 2000.0)),
        state_dir=raw.get("state_dir", "state"),
        strategy=StrategyConfig(**raw.get("strategy", {})),
        risk=RiskConfig(**raw.get("risk", {})),
        toss=TossConfig(**raw.get("toss", {})),
    )
    # 비밀값은 환경변수로만 받는다 (yaml에 커밋 금지)
    cfg.toss.client_id = os.getenv("TOSS_CLIENT_ID", "")
    cfg.toss.client_secret = os.getenv("TOSS_CLIENT_SECRET", "")
    cfg.toss.account = os.getenv("TOSS_ACCOUNT", "")

    # 실거래는 yaml + 환경변수 이중 확인
    if cfg.is_live and os.getenv("PENNYBOT_LIVE_CONFIRM") != "YES":
        raise SystemExit(
            "mode=live 이지만 PENNYBOT_LIVE_CONFIRM=YES 가 설정되지 않았습니다. 실거래를 중단합니다."
        )
    return cfg
