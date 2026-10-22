# pennybot — 미국 페니주 자동매매 (토스증권 Open API)

토스증권 데이마켓(한국 낮)에 사서 그날 밤 미국 프리마켓~정규장 개장 직후에 파는 페니주 봇입니다.
전략 계획서: 공유 문서 "미국 페니주 자동매매 전략 계획서".

> ⚠️ 페니주는 변동성·스프레드·거래정지·상장폐지 위험이 매우 큽니다.
> 이 코드는 수익을 보장하지 않으며, 반드시 **모의매매(paper)로 충분히 검증한 뒤** 소액으로 시작하세요.

## 지금 단계: 세션별 데이터 수집

전략을 검증하려면 데이마켓·프리·정규·애프터 각 세션의 가격과 호가가 필요합니다.
`python -m pennybot collect`를 24시간 켜 두면 `state/market.db`(SQLite)에 쌓입니다.

| 테이블 | 내용 | 주기 |
| --- | --- | --- |
| `universe` | 토스 거래 가능 종목 중 $0.2~$10 | 하루 1번 |
| `prices` | 유니버스 전체 현재가 | 1분 |
| `quotes` | 감시 종목 최우선 호가·잔량 (빈 호가도 기록) | 1분 |
| `bars_1m` | 감시 종목 1분봉 | 30분 + 세션이 바뀔 때 |
| `rankings` | 시장·토스 거래대금, 급등·급락 상위 | 10분 |
| `filings` | SEC 공시(Form 4·8-K·증자) 접수 시각과 공개된 세션, 내부자 매수 금액 | 5분 |
| `session_summary` | 종목·영업일·세션별 OHLCV, VWAP, 스프레드 중앙값 | 애프터마켓 종료 시 |

모든 행에 `trade_date`(미국 영업일)와 `session`(DAY/PRE/REGULAR/AFTER)이 붙습니다.
데이마켓은 그 영업일 전날 밤(미국 동부 20:00~04:00)이므로, 한 영업일은 데이 → 프리 → 정규 → 애프터 순서입니다.
세션 시각은 토스 장 운영 캘린더 API를 정본으로 씁니다.

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml
cp .env.example .env            # TOSS_CLIENT_ID / TOSS_CLIENT_SECRET / SEC_USER_AGENT
python -m pennybot accounts     # accountSeq 확인 → .env 의 TOSS_ACCOUNT
python -m pennybot check        # 연결 점검: 장 캘린더, 시세, 1분봉의 세션 분포
python -m pennybot collect      # 수집 시작 (24시간)
python -m pytest
```

토스 API 사용법과 명세에서 확인한 제약은 [docs/toss_api.md](docs/toss_api.md)에 정리했습니다.

## (구) Coil & Catalyst 스크리너와 장중 매매 루프

아래는 이전 계획의 코드입니다. 며칠 보유를 전제로 해서 지금 계획(데이마켓 매수 → 당일 밤 매도)과
맞지 않습니다. 희석 필터와 공시 파싱(`screener/edgar.py`)은 새 전략에서 다시 씁니다.

### Coil & Catalyst — 남들과 다른 점

| | 일반적인 페니주 스캐너 | Coil & Catalyst |
|---|---|---|
| 언제 찾나 | 장중, 이미 +20~50% 오른 뒤 | 전날 밤, 오르기 전 |
| 무엇을 보나 | 상승률·거래량 순위 | 변동성 수축 + 조용한 매집 + 박스 상단 근접 |
| 공시 | 안 봄 (뉴스 헤드라인 정도) | SEC 공시로 **희석·상폐 위험 종목 차단**, 내부자 장내매수 가점 |
| 군중 | 군중과 같은 종목 | 5일 +50% 넘게 오른 종목은 일부러 제외 |
| 기준값 | 고정 숫자 | 그날 후보군 안에서의 **상대 순위(백분위)** |

**1단계: 응축 점수 (일봉, 0~100)**
- `atr_ratio`: 10일 ATR / 60일 ATR이 낮을수록 좋음 (스프링이 눌린 정도)
- `tightness`: 최근 15일 박스 폭
- `vol_trend`: 가격은 횡보하는데 거래량이 늘어나는지 (10일 / 60일)
- `obv_net`: 20일 동안 상승일 거래량에서 하락일 거래량을 뺀 비율 (몰래 사는 쪽이 있는지)
- `clv`: 종가가 일중 고가 쪽에서 끝나는지 (매도 물량을 누가 받아내는지)
- `dist_to_pivot`: 20일 고점(피벗)에 얼마나 가까운지, 저점이 높아지고 있으면 +5

**2단계: 공시 레이어 (SEC EDGAR, 무료)**. 응축 상위 60개만 조회합니다.
- 차단: 30일 이내 증자 공시(424B·S-1·S-3·F-1·F-3), 상장폐지 통지(8-K 3.01), 정기보고서 지연(NT 10-K/Q)
- 감점: 90일 이내 증자 공시 −25, 사모 증자(8-K 3.02) −15, 정관 변경/액면병합(8-K 5.03) −10
- 가점: 내부자 장내매수(Form 4 코드 P, 금액 비례) 최대 +20, 13D 지분 취득 +8, 중요 계약(8-K 1.01) +3

**3단계: 장중 진입 (피벗 돌파)**
- 현재가가 피벗 위에 있고, 1분봉 종가로 돌파가 확인될 것
- 당일 누적 거래량이 평소 페이스(20일 평균 × 경과시간 비율)의 2배 이상
- 피벗보다 5% 넘게 올랐으면 추격 금지, 피벗 −3%를 이탈하면 돌파 실패로 보고 손절

모든 후보의 팩터 값은 `state/screens/날짜.csv`에 쌓입니다. 이 데이터로 나중에
**어떤 팩터가 실제로 수익을 냈는지** 검증하고 가중치(`screener.weights`)를 조정할 수 있습니다.

### 구조

```
pennybot/
  toss_api.py       토스 Open API 저수준 클라이언트 (토큰 공유, 그룹별 한도, 에러 envelope)
  sessions.py       세션 판정 (토스 장 캘린더 기준, 데이/프리/정규/애프터)
  collector/        세션별 데이터 수집기 (runner.py) + SQLite 저장소 (store.py)
  brokers/toss.py   Broker 인터페이스 ↔ 토스 API 어댑터
  brokers/paper.py  모의 브로커 (실제 주문 없음)
  data.py           토스 키 없이 쓰는 공개 시세 피드 (Yahoo)
  screener/         Coil & Catalyst 스크리너
    factors.py      일봉 응축 팩터 (순수 함수)
    scorer.py       하드필터 + 백분위 점수
    edgar.py        SEC 공시 파싱: 희석 방패 / 촉매 / Form 4 내부자 매수
  strategy.py       피벗 돌파(스크리너 종목) / 갭업 ORB(수동 종목) 진입, 청산 규칙
  risk.py           포지션 크기 / 일일 손실한도 / 거래횟수 / 킬스위치
  engine.py         매매 루프 (지정가 주문 → 15초 미체결 시 취소)
```

### 시작하기

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml   # 감시종목, 전략/리스크 값 수정
cp .env.example .env                 # 토스 API 키 입력
python -m pytest                     # 테스트
python -m pennybot screen            # 장 전 스크리닝 → state/watchlist.json
python -m pennybot run -v            # 모의매매 실행 (장중에만 동작)
python -m pennybot run --once -v     # 한 번만 실행해서 동작 확인
```

### 토스증권 연결

1. 토스증권 앱/웹에서 **Open API 신청** → `client_id`, `client_secret` 발급
2. `.env` 에 `TOSS_CLIENT_ID`, `TOSS_CLIENT_SECRET`, `TOSS_ACCOUNT`(accountSeq) 입력
3. 엔드포인트와 필드는 공식 명세 v1.2.17 기준으로 `toss_api.py` / `brokers/toss.py` 에 구현돼 있습니다
4. 키가 있으면 `mode: paper` 에서도 토스 시세로 모의매매합니다 → 시세·잔고 조회가 정상인지 먼저 확인
5. 실거래: `mode: live` + 환경변수 `PENNYBOT_LIVE_CONFIRM=YES` (둘 다 있어야 실제 주문)

### 안전장치

- 지정가 주문만 사용 (매수 ask+0.5%, 매도 bid−1% 상한), 스프레드 2% 초과 종목 제외
- 종목당 최대 $200, 최대 3종목, 일일 손실 $100 도달 시 신규진입 중단
- 손절 시 손실이 남은 일일한도의 절반을 넘지 않도록 수량 제한
- 15:00(ET) 이후 신규진입 금지, 15:50 전량 청산 — 오버나이트 보유 없음
- 당일 청산한 종목 재진입 금지, 돌파 후 +5% 넘게 오른 종목 추격 금지
- `state/KILL` 파일을 만들면 즉시 신규진입 중단 (`touch state/KILL`)

### 하루 흐름

1. 전날 밤이나 장 전: `python -m pennybot screen`. 처음 실행하면 전 종목 일봉을 받느라 오래 걸리고, 같은 날 다시 돌리면 캐시를 씁니다.
   전 종목 대신 일부만 보려면 `screener.universe_file`에 종목을 한 줄에 하나씩 적으세요.
2. 장중: `python -m pennybot run`. `watchlist: auto`면 스크리너 결과를 점수 순으로 감시합니다.
3. 주말: `state/screens/*.csv`와 매매 결과를 비교해서 팩터 가중치를 조정합니다.
