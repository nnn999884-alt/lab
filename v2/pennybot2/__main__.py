"""사용법: python -m pennybot2 <역할>
  run                 허브 + 일꾼 전부를 따로 켜고 감시 (죽은 것만 재시작, 반복해서 죽으면 알림)
  status              지금 상태: 허브·일꾼 응답, 마지막 체결, 종목 수
  monitor             일꾼 응답 없음·시세 끊김 감시 (run 에 포함)
  hub                 데이터가 모이는 허브
  ls                  LS증권 실시간 시세 일꾼 (.env 에 LS_APP_KEY, LS_APP_SECRET 필요)
  grade DB경로        저장된 알림을 1분봉으로 채점 (등급·종류·세션별, 대조군·기간 3등분 비교). 기존 pennybot 의 market.db 도 읽음
  daily               일봉(야후) 동기화 + 전날 밤 점수로 오늘 급등 후보 선정
  surge | notify      감지 일꾼 / 알림 일꾼
  replay [종목 ...]   저장된 1분봉을 허브로 재생 (허브·감지·알림이 켜져 있어야 함)"""
from __future__ import annotations

import logging
import sys

from .core import config
from .core.lock import ALREADY_RUNNING, acquire


def _status(cfg) -> int:
    import json
    import socket
    try:
        s = socket.create_connection(("127.0.0.1", cfg.hub_port), timeout=3)
        s.sendall(b'{"op":"status"}\n')
        st = json.loads(s.makefile("rb").readline())
    except (OSError, ValueError):
        print(f"허브에 연결할 수 없음 (포트 {cfg.hub_port}) — 켜져 있나요? `python -m pennybot2 run`")
        return 1
    ta = st["last_tick_age"]
    print(f"허브 가동 {st['uptime']:.0f}초 · 종목 {st['symbols']}개 · 접속 {st['clients']} · 버린 메시지 {st['dropped']}")
    print("마지막 체결: " + ("없음" if ta is None else f"{ta:.0f}초 전"))
    for w, v in sorted(st["workers"].items()):
        flag = "✅" if v["age"] < 15 else "🛑"
        print(f"  {flag} {w:<8} 응답 {v['age']:.0f}초 전  pid {v['pid']}  {v['info'] or ''}")
    return 0


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    role = argv[0] if argv else ""
    cfg = config.load()
    if role in ("run", "hub", "ls", "daily", "surge", "notify", "monitor") and not acquire(cfg.state_dir, role):
        print(f"'{role}' 은(는) 이미 실행 중입니다 (state/locks/{role}.lock). 중복 실행을 막기 위해 종료합니다.")
        return ALREADY_RUNNING
    if role == "status":
        return _status(cfg)
    if role == "run":
        from .supervisor import supervise
        supervise(cfg)
    elif role == "hub":
        from .hub.server import Hub
        Hub(cfg.db_path, port=cfg.hub_port).serve_forever()
    elif role == "monitor":
        from .workers import monitor
        monitor.run(cfg)
    elif role == "surge":
        from .workers import surge
        surge.run(cfg)
    elif role == "notify":
        from .workers import notify
        notify.run(cfg)
    elif role == "ls":
        from .workers import ls_feed
        ls_feed.run(cfg)
    elif role == "grade":
        from .grading import run as grading
        return grading.main(argv[1:])
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
