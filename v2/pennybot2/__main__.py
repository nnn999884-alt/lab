"""사용법: python -m pennybot2 <역할>
  run                 허브 + 일꾼 전부를 따로 켜고 감시 (죽은 것만 재시작)
  hub                 데이터가 모이는 허브
  ls                  LS증권 실시간 시세 일꾼 (.env 에 LS_APP_KEY, LS_APP_SECRET 필요)
  daily               일봉(야후) 동기화 + 전날 밤 점수로 오늘 급등 후보 선정
  surge | notify      감지 일꾼 / 알림 일꾼
  replay [종목 ...]   저장된 1분봉을 허브로 재생 (허브·감지·알림이 켜져 있어야 함)"""
from __future__ import annotations

import logging
import sys

from .core import config


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    role = argv[0] if argv else ""
    cfg = config.load()
    if role == "run":
        from .supervisor import supervise
        supervise()
    elif role == "hub":
        from .hub.server import Hub
        Hub(cfg.db_path, port=cfg.hub_port).serve_forever()
    elif role == "surge":
        from .workers import surge
        surge.run(cfg)
    elif role == "notify":
        from .workers import notify
        notify.run(cfg)
    elif role == "ls":
        from .workers import ls_feed
        ls_feed.run(cfg)
    elif role == "daily":
        from .workers import daily
        daily.run(cfg)
    elif role == "replay":
        from .workers import replay
        print(f"봉 {replay.run(cfg, argv[1:] or None)}개 재생")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
