# pennybot2 변경 기록

`v2/` (처음부터 다시 만드는 새 버전)의 변경만 적는 파일입니다. 기존 버전의 기록은 루트 `CHANGELOG.md` 에 따로 있습니다.
동작이 바뀌면 같은 커밋에서 날짜와 함께 여기에 적습니다. 계획은 `PLAN.md`.

## 2026-11-25

- 새로 만들기 시작. 1단계 뼈대: 설정(`config.py`), 세션 캘린더(`sessions.py`), SQLite 저장소(`store.py`), 알림 전송(`notify.py`), 테스트(`tests2/`).
- 폴더 구조 정리: 새 버전 전부를 `v2/` 안으로 (`v2/pennybot2/core`, `v2/pennybot2/alerts`, `v2/tests`, `v2/PLAN.md`, `v2/CHANGELOG.md`). 기존 버전 파일과 섞이지 않음.

## 2026-11-25 (구조 재설계 2: 허브 + 일꾼)

- 한 프로세스 안의 이벤트 버스를 버리고 **허브 + 별도 프로세스 일꾼**으로 다시 설계. 데이터는 허브(`hub/server.py`)로 모이고 일꾼은 허브하고만 통신(JSON 줄, 로컬 TCP, `hub_port` 기본 8765).
- 허브: 받은 줄을 구독자에게 재인코딩 없이 즉시 전달, 저장(bars_1m·events·price_now)은 1초 배치로 별도 스레드, 느린 구독자 메시지는 버림, 잘못된 줄·갑작스런 접속 끊김 무시.
- `hub/client.py`: 자동 재접속·재구독, 보내기는 막히지 않음(끊긴 동안은 버림).
- 일꾼: `workers/surge.py`(급등: 5분 +10%, 종목별 15분 쿨다운), `workers/notify.py`(디스코드·텔레그램), `workers/replay.py`(저장 봉 재생).
- `supervisor.py`: `python -m pennybot2 run` 이 허브·일꾼을 각각 켜고 죽은 것만 재시작(연속 실패 시 대기 늘림).
- 삭제: 이벤트 버스(`core/bus.py`, `core/events.py`), `feeds/`, `detectors/`, `sinks/`, `app.py`.
