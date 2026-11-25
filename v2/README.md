# pennybot2 (새 버전)

루트의 `pennybot/` 은 기존 버전이고, 이 `v2/` 폴더가 처음부터 다시 설계한 새 버전입니다. 서로 코드·테스트·기록을 공유하지 않습니다.
먼저 [LESSONS.md](LESSONS.md)(기존 버전의 주의사항)를 읽을 것.
목표: **실시간 감시·알림** (매매는 모의만), **지연 최소**.

## 구조: 허브 + 따로 도는 일꾼
```
 일꾼(프로세스 각각)                    허브 (데이터가 모이는 한 곳)                 일꾼
 ls 시세 ─┐                           ┌─ 메모리에 최신값 보관                    ┌─ surge 감지 ─▶ signal ─┐
 일봉   ──┼─ tick/bar (TCP, JSON줄) ─▶ ├─ 받은 줄을 구독자에게 그대로 전달 ───────┼─ notify 알림 ◀──────────┘
 공시   ──┘                           └─ 1초마다 모아서 SQLite 저장 (따로 스레드) └─ (대시보드, 모의매매…)
```
- 일꾼끼리는 서로 모르고 허브하고만 말한다. 하나가 죽거나 느려도 나머지는 그대로 (`python -m pennybot2 run` 의 감독자가 죽은 것만 재시작).
- 허브는 디스크를 기다리지 않는다(저장은 별도 스레드·배치). 느린 구독자는 그 구독자 몫만 버린다.
- 재생(`replay`)은 저장된 봉을 실시간 일꾼과 같은 통로로 흘리므로 감지기는 구분하지 못한다 = 백테스트.

폴더: `pennybot2/core`(설정·세션·저장소) · `hub`(서버·클라이언트) · `workers`(일꾼 하나 = 파일 하나) · `scoring`(후보 점수) · `grading`(알림 채점) · `supervisor.py`

## 윈도우
`setup.bat`(한 번) → `.env` 에 `LS_APP_KEY`·`LS_APP_SECRET` → `start.bat`. 상태는 `pennybot.bat status`. 같은 일꾼은 두 번 켜지지 않음(`state/locks`). (윈도우 실행은 아직 미검증)

## 실행 (v2/ 안에서)
- `python -m pytest -q` 테스트
- `python -m pennybot2 run` 허브 + 감지 + 알림을 각각 따로 켬 → 다른 창에서 `python -m pennybot2 replay` 로 저장된 1분봉 재생
- `python -m pennybot2 status` 지금 상태(일꾼 응답·마지막 체결·종목 수). 로그는 `state/logs/<일꾼>.log`
- `python -m pennybot2 backtest-ignite DB경로` 과거 1분봉으로 조기 감지를 돌려 신호 저장 → `grade DB경로 --alerts-db=신호DB` 로 채점
- `python -m pennybot2 train DB경로` 일봉으로 '오늘 급등 확률' 모델 학습·시험 (시간순 분할, 기존 규칙 점수와 비교, 통과해야 채택)
- `python -m pennybot2 featurelab DB경로` 일봉 점수 항목이 급등을 실제로 늘렸는지 검증
- `python -m pennybot2 grade DB경로` 저장된 알림을 같은 기준으로 채점 (기존 봇의 `market.db` 도 가능)
- 개별 실행: `hub` · `ls`(LS 시세) · `daily`(일봉·후보) · `ignite`(조기 감지) · `surge`(늦은 감지, 비교용) · `notify` · `replay [종목…]`

## 메시지 규칙 (허브)
JSON 한 줄. 토픽 `tick {s,ts,p,v}` · `bar {s,ts,o,h,l,c,v}` · `signal {s,ts,kind,text,value}` (ts = epoch 초), `candidates {asof,items}` (허브가 마지막 것을 기억).
새 일꾼은 이 규칙만 지키면 된다 (`hub/client.py` 의 `HubClient`).
