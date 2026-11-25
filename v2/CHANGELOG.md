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

## 2026-11-25 (3단계: LS 시세 일꾼)

- `python -m pennybot2 ls`: LS증권 웹소켓 체결(GSC)을 받아 `tick` 을 허브로 보내고 1분이 끝나면 `bar` 도 보낸다. `run` 의 기본 일꾼에 포함.
- 구성: `workers/ls/auth.py`(토큰, 매일 07:00 KST 전 갱신·파일 재사용), `master.py`(종목 마스터 g3190 → 가격대·거래정지·ETF류 걸러 keysymbol 목록), `frames.py`(프레임 해석·구독 메시지·1분봉 조립), `workers/ls_feed.py`(연결·재연결·초당 90건씩 구독·토큰 만료 전 재연결).
- 연결이 끊기면 5초 뒤 새로 접속. `requirements.txt` 추가(websocket-client 포함). 테스트는 가짜 LS 서버로 구독→체결→허브 전달까지 확인(진짜 LS 접속은 미검증).

## 2026-11-25 (4단계: 일봉과 전날 밤 후보)

- `python -m pennybot2 daily`: 정규장 종료 20분 뒤(휴장일·조기폐장 반영) 야후에서 일봉을 받아 `daily_bars` 에 저장 → 전날 밤 점수로 오늘 급등 후보를 뽑아 `candidates` 표와 허브(`candidates` 토픽)에 올림. 30분마다 새 일봉이 필요한지 보고, 후보는 10분마다 다시 올림. `run` 기본 일꾼에 포함.
- 점수 규칙은 기존 `pennybot/nightscore.py` 를 그대로 옮김 (▲ 11개 − ▼ 4개, 5점↑ 후보). 기존 구현과 점수가 같은지 60개 모의 종목으로 비교하는 테스트 포함. 일봉 이력이 없는 종목은 6개월치, 있으면 10일치만 받음. 종목 목록은 LS 마스터(키 있을 때), 없으면 DB 의 일봉 종목.
- 허브: `candidates` 토픽은 마지막 메시지를 기억했다가 새로 구독하는 일꾼에게 바로 줌 (감지 일꾼이 나중에 켜져도 후보를 바로 앎).
- 급등 신호: 어젯밤 후보였던 종목이면 신호 글에 `★ 어젯밤 후보 N점 (그날 2배 확률 약 X%) — 이유` 를 붙임.
