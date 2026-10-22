"""실행:
  python -m pennybot screen [-v]          # 장 전: Coil & Catalyst 스크리닝 → state/watchlist.json
  python -m pennybot run [--once] [-v]    # 장중: 매매 루프
"""
from __future__ import annotations

import argparse
import logging

from .brokers import PaperBroker, TossBroker
from .config import Config, load_config
from .data import YahooFeed
from .engine import Engine


def _feed(cfg: Config):
    """토스 키가 있으면 토스 시세, 없으면 공개 피드."""
    return TossBroker(cfg.toss) if cfg.toss.client_id else YahooFeed()


def cmd_screen(cfg: Config) -> None:
    from .screener import run_screen
    from .screener.edgar import EdgarClient

    edgar = EdgarClient(cfg.sec_user_agent) if cfg.sec_user_agent else None
    if edgar is None:
        logging.warning("SEC_USER_AGENT 미설정 → 공시(희석/촉매) 레이어 없이 차트 점수만 사용합니다.")
    picks = run_screen(cfg.screener, _feed(cfg), edgar, cfg.state_dir)
    print(f"\n{'순위':>4} {'종목':<6} {'총점':>6} {'응축':>6} {'공시':>6} {'현재가':>8} {'피벗':>8}  사유")
    for i, p in enumerate(picks, 1):
        print(f"{i:>4} {p.symbol:<6} {p.score:>6.1f} {p.coil:>6.1f} {p.catalyst:>+6.1f} "
              f"{p.price:>8.4f} {p.pivot:>8.4f}  {', '.join(p.reasons)}")


def cmd_run(cfg: Config, once: bool) -> None:
    if cfg.is_live:
        broker = TossBroker(cfg.toss)
        logging.warning("⚠️  실거래 모드: 토스증권 계좌로 실제 주문이 나갑니다.")
    else:
        broker = PaperBroker(_feed(cfg), cash=cfg.paper_cash)
    engine = Engine(cfg, broker)
    if once:
        engine.tick()
    else:
        engine.run()


def main() -> None:
    ap = argparse.ArgumentParser(description="미국 페니주 자동매매 (토스증권)")
    ap.add_argument("command", nargs="?", default="run", choices=["run", "screen"])
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--once", action="store_true", help="run: 한 틱만 실행 (장 시간 무시)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    cfg = load_config(args.config)
    if args.command == "screen":
        cmd_screen(cfg)
    else:
        cmd_run(cfg, args.once)


if __name__ == "__main__":
    main()
