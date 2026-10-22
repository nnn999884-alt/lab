"""설정 로딩: config.yaml + 환경변수(.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
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
    min_volume_pace: float = 2.0     # 피벗 돌파: 당일 거래량이 평소 페이스의 N배
    pivot_stop_buffer_pct: float = 3.0   # 피벗 아래 3% 이탈 시 손절 (돌파 실패)
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
class ScreenerConfig:
    universe_file: str = ""          # 비우면 SEC 상장종목 전체 (OTC 제외)
    min_price: float = 0.50
    max_price: float = 5.00
    min_dollar_vol_20: float = 300_000   # 터지기 전이라 장중 기준보다 낮게
    max_ret_5: float = 0.50              # 5일 +50% 넘으면 이미 군중 진입
    min_ret_120: float = -0.85           # 6개월 -85% 이하 = 상습 희석 의심
    min_dist_to_pivot: float = -0.02     # 피벗 -2% ~ +15% 아래
    max_dist_to_pivot: float = 0.15
    catalyst_top_k: int = 60             # 공시 조회할 상위 후보 수
    top_n: int = 15                      # 최종 감시종목 수
    workers: int = 4
    weights: dict = field(default_factory=dict)   # scorer.DEFAULT_WEIGHTS 덮어쓰기
    catalyst: dict = field(default_factory=dict)  # edgar.evaluate 기간 설정 덮어쓰기

    def filter_dict(self) -> dict:
        return {k: getattr(self, k) for k in (
            "min_price", "max_price", "min_dollar_vol_20", "max_ret_5", "min_ret_120",
            "min_dist_to_pivot", "max_dist_to_pivot")}


@dataclass
class TossConfig:
    base_url: str = "https://openapi.tossinvest.com"
    token_path: str = "state/toss_token.json"   # 프로세스 간 토큰 공유 (client 당 유효 토큰 1개)
    client_id: str = ""
    client_secret: str = ""
    account: str = ""                           # accountSeq (GET /api/v1/accounts)


@dataclass
class CollectorConfig:
    db_path: str = "state/market.db"
    markets: list = field(default_factory=lambda: ["NASDAQ", "NYSE", "AMEX"])
    min_price: float = 0.20          # 수집은 매매 범위보다 넓게
    max_price: float = 10.0
    price_poll_seconds: int = 60     # 유니버스 전체 현재가 (200개씩 묶음 조회)
    watch_size: int = 120            # 호가·1분봉을 촘촘히 받을 종목 수
    orderbook_poll_seconds: int = 60
    candle_sync_minutes: int = 30    # 감시 종목 1분봉 동기화 주기 (세션 종료 시에도 실행)
    ranking_poll_minutes: int = 10
    edgar_poll_minutes: int = 5
    universe_refresh_hours: int = 24


@dataclass
class Config:
    mode: str = "paper"              # paper | live
    watchlist: list[str] = field(default_factory=list)
    poll_seconds: int = 20
    paper_cash: float = 2000.0
    state_dir: str = "state"
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    screener: ScreenerConfig = field(default_factory=ScreenerConfig)
    collector: CollectorConfig = field(default_factory=CollectorConfig)
    toss: TossConfig = field(default_factory=TossConfig)
    sec_user_agent: str = ""

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    @property
    def auto_watchlist(self) -> bool:
        return self.watchlist == ["AUTO"]


def _known(cls, d: dict):
    """예전 설정 파일의 사라진 키(엔드포인트 경로 등)는 무시한다."""
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in d.items() if k in names})


def _watchlist(v) -> list[str]:
    """'auto' → 스크리너 결과 사용, 리스트 → 수동 감시종목."""
    if isinstance(v, str):
        return ["AUTO"] if v.lower() == "auto" else [v.upper()]
    return [s.upper() for s in v]


def load_config(path: str | Path = "config.yaml") -> Config:
    load_dotenv()
    raw: dict = {}
    p = Path(path)
    if p.exists():
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    cfg = Config(
        mode=raw.get("mode", "paper"),
        watchlist=_watchlist(raw.get("watchlist", "auto")),
        poll_seconds=int(raw.get("poll_seconds", 20)),
        paper_cash=float(raw.get("paper_cash", 2000.0)),
        state_dir=raw.get("state_dir", "state"),
        strategy=StrategyConfig(**raw.get("strategy", {})),
        risk=RiskConfig(**raw.get("risk", {})),
        screener=ScreenerConfig(**raw.get("screener", {})),
        collector=CollectorConfig(**raw.get("collector", {})),
        toss=_known(TossConfig, raw.get("toss", {})),
    )
    # 비밀값은 환경변수로만 받는다 (yaml에 커밋 금지)
    cfg.toss.client_id = os.getenv("TOSS_CLIENT_ID", "")
    cfg.toss.client_secret = os.getenv("TOSS_CLIENT_SECRET", "")
    cfg.toss.account = os.getenv("TOSS_ACCOUNT", "")
    cfg.sec_user_agent = os.getenv("SEC_USER_AGENT", "")

    # 실거래는 yaml + 환경변수 이중 확인
    if cfg.is_live and os.getenv("PENNYBOT_LIVE_CONFIRM") != "YES":
        raise SystemExit(
            "mode=live 이지만 PENNYBOT_LIVE_CONFIRM=YES 가 설정되지 않았습니다. 실거래를 중단합니다."
        )
    return cfg
