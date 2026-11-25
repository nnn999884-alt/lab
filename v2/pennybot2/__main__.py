"""사용법: python -m pennybot2 replay [종목 ...]   저장된 1분봉을 다시 흘려 신호를 확인"""
from __future__ import annotations

import sys

from .app import build
from .core import config
from .core.store import Store
from .feeds.replay import ReplayFeed


def main(argv: list[str]) -> int:
    if not argv or argv[0] != "replay":
        print(__doc__)
        return 2
    cfg = config.load()
    bus, console = build(cfg)
    ReplayFeed(Store(cfg.db_path), argv[1:] or None).run(bus)
    print(f"신호 {len(console.signals)}건")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
