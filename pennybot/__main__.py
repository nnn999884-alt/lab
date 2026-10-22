"""실행: python -m pennybot [--config config.yaml] [--once] [-v]"""
from __future__ import annotations

import argparse
import logging

from .brokers import PaperBroker, TossBroker
from .config import load_config
from .data import YahooFeed
from .engine import Engine


def main() -> None:
    ap = argparse.ArgumentParser(description="미국 페니주 자동매매 (토스증권)")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--once", action="store_true", help="한 틱만 실행 (장 시간 무시)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)

    if cfg.is_live:
        broker = TossBroker(cfg.toss)
        logging.warning("⚠️  실거래 모드: 토스증권 계좌로 실제 주문이 나갑니다.")
    else:
        # 토스 키가 있으면 토스 시세로, 없으면 공개 피드로 모의매매
        feed = TossBroker(cfg.toss) if cfg.toss.client_id else YahooFeed()
        broker = PaperBroker(feed, cash=cfg.paper_cash)

    engine = Engine(cfg, broker)
    if args.once:
        engine.tick()
    else:
        engine.run()


if __name__ == "__main__":
    main()
