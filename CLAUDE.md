# pennybot — Claude 작업 안내

사용자는 한국어로 대화한다. 답도 한국어로, 비개발자도 따라 할 수 있게 짧고 구체적으로.

## 이 프로젝트
토스증권 Open API 로 미국 페니주를 자동매매하는 봇. **데이마켓(한국 낮)에 사서 그날 밤 프리마켓~정규장 개장 직후에 판다.**
설명서는 `README.md`, 토스 API 정리는 `docs/toss_api.md`, 공식 명세 사본은 `docs/toss-openapi-1.2.17.json`.
코드는 클라우드 세션에서 만들었고, 그 환경은 토스 API 에 접속할 수 없어 **실제 토스 연결은 아직 한 번도 검증하지 않았다.**
이 PC 에서 돌리는 Claude 의 첫 번째 일은 그 검증이다.

## 구조
- `pennybot/toss_api.py` 토스 REST 클라이언트 (토큰 공유, 한도, 주문·조건주문)
- `pennybot/sessions.py` 세션 캘린더 (DAY/PRE/REGULAR/AFTER, 토스 장 캘린더가 정본)
- `pennybot/collector/` 세션별 데이터 수집기 → `state/market.db`
- `pennybot/trading/` 매매 엔진: `trader.py`(주문 줄기 추적·청산·서버 손절 안전망), `strategies.py`, `risk.py`,
  `exchange.py`(실거래/모의), `journal.py`(→ `state/trading.db`), `report.py`(일일 리포트), `notify.py`(웹훅·텔레그램)
- `pennybot/dilution.py`, `news.py` 공시·보도자료 판정, `pennybot/stream.py` 웹소켓, `pennybot/backtest.py` 리플레이
- `pennybot/dashboard/` http://127.0.0.1:8050
- 윈도우 스크립트: `setup.bat`, `start.bat`(3개 창 + 자동 재시작), `pennybot.bat <명령>`, `update.bat`, `autostart_on.bat`

## 명령 (윈도우는 `pennybot.bat`, 맥/리눅스는 `./pennybot.sh`)
- `accounts` 계좌번호(accountSeq) 확인 → `.env` 의 `TOSS_ACCOUNT`
- `check` 연결 점검: 장 캘린더, 시세, 1분봉의 세션 분포 (데이마켓 1분봉이 오는지 확인)
- `probe-orders --symbol <종목> --yes-real-order` 실계좌로 정정·취소 동작 1회 확인 (**실제 주문**, 체결 안 될 가격 1주)
- `collect` / `trade` / `dashboard [--demo]` / `backtest` / `telegram-setup`
- 테스트: `.venv\Scripts\python -m pytest -q` (전부 통과해야 함)

## 지금 할 일 (실제 연결 검증, 순서대로)
1. `.env` 에 `TOSS_CLIENT_ID`, `TOSS_CLIENT_SECRET`, `TOSS_ACCOUNT`, `SEC_USER_AGENT` 가 있는지 확인 (값은 출력하지 말 것)
2. `pennybot.bat check` → 403 이면 토스 허용 IP 미등록, 401 이면 키 오류. 결과를 사용자에게 쉬운 말로 설명
3. 1분봉에 DAY(데이마켓) 세션 봉이 있는지 확인. 없으면 수집기가 데이마켓을 현재가 폴링으로만 채우는지 점검
4. `pennybot.bat probe-orders` 는 **사용자에게 먼저 묻고** 실행. 결과(`state/probe_orders.json`)로 정정·취소가
   새 orderId 를 주는지, 상태값이 코드(`trading/exchange.py` 의 `_LIVE_STATUS`)와 맞는지 확인하고 다르면 고친다
5. 서버 손절 안전망(조건주문 `POST /api/v1/conditional-orders`)이 실제로 등록·조회·취소되는지 모의 거래 중에 확인
6. 웹소켓(`stream.py`) 연결·구독 응답 형식이 코드와 맞는지 확인 (`websocket-client` 필요). 명세 없이 공개 SDK 기준으로 짰음
7. 보도자료 피드 주소(`news.py` 의 `DEFAULT_FEEDS`)가 살아 있는지 확인
8. 이상 없으면 `start.bat` 으로 **모의매매**를 며칠 돌리며 대시보드·일일 리포트 확인

## 지켜야 할 것
- **동작이 바뀌는 변경은 `CHANGELOG.md` 에 날짜와 함께 적는다.** 매매 규칙, 기본 설정값, CLI 명령, 대시보드 화면,
  알림, 수집 항목, 파일 구조가 바뀌면 같은 커밋에서 README(설명이 달라진 경우)와 CHANGELOG를 함께 고친다.
  사람이 바뀌거나 AI 세션이 바뀌어도 CHANGELOG만 보면 따라잡을 수 있어야 한다.
- `.env`, `config.yaml`, `state/` 는 절대 커밋하지 않는다 (비밀값·매매 기록). 키 값을 화면이나 로그에 출력하지 않는다
- 실거래는 `config.yaml` 의 `mode: live` + `.env` 의 `PENNYBOT_LIVE_CONFIRM=YES` 둘 다 있어야 한다. 사용자가 명시적으로
  원하기 전에는 실거래 모드로 바꾸지 않는다. 실제 주문이 나가는 명령은 실행 전에 사용자에게 확인받는다
- 코드를 고치면 테스트를 돌리고, 작업 브랜치 `claude/elegant-galileo-1nmavv` 에 커밋·푸시한다
  (사용자 PC 들은 `update.bat` = `git pull` 로 받는다)
- 파일 입출력은 항상 `encoding="utf-8"` (윈도우 기본 인코딩 cp949 로 깨진 적 있음)
