# pennybot — 미국 페니주 자동매매 (토스증권 Open API)

미국 페니주($0.5~$5) 데이트레이딩 봇입니다. 종목 선정은 **Coil & Catalyst** 알고리즘으로 합니다.
대부분의 스캐너처럼 '오늘 많이 오른 종목'을 쫓지 않고, **터지기 직전에 눌려 있는 종목**을 전날 밤에 골라 둔 뒤
장중에 박스 상단이 뚫리는 순간 들어갑니다.

> ⚠️ 페니주는 변동성·스프레드·거래정지·상장폐지 위험이 매우 큽니다.
> 이 코드는 수익을 보장하지 않으며, 반드시 **모의매매(paper)로 충분히 검증한 뒤** 소액으로 시작하세요.

## Coil & Catalyst — 남들과 다른 점

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

## 구조

```
pennybot/
  brokers/toss.py   토스증권 Open API 클라이언트 (OAuth2, Bearer, X-Tossinvest-Account)
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

## 시작하기

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml   # 감시종목, 전략/리스크 값 수정
cp .env.example .env                 # 토스 API 키 입력
python -m pytest                     # 테스트
python -m pennybot screen            # 장 전 스크리닝 → state/watchlist.json
python -m pennybot run -v            # 모의매매 실행 (장중에만 동작)
python -m pennybot run --once -v     # 한 번만 실행해서 동작 확인
```

## 토스증권 연결

1. 토스증권 앱/웹에서 **Open API 신청** → `client_id`, `client_secret` 발급
2. `.env` 에 `TOSS_CLIENT_ID`, `TOSS_CLIENT_SECRET`, `TOSS_ACCOUNT`(계좌 식별자) 입력
3. **공식 명세(https://developers.tossinvest.com)와 엔드포인트를 대조**해서
   `config.yaml` 의 `toss:` 경로를, 응답 필드명이 다르면 `brokers/toss.py` 의 `_pick(...)` 후보를 수정
4. 키가 있으면 `mode: paper` 에서도 토스 시세로 모의매매합니다 → 시세·잔고 조회가 정상인지 먼저 확인
5. 실거래: `mode: live` + 환경변수 `PENNYBOT_LIVE_CONFIRM=YES` (둘 다 있어야 실제 주문)

## 안전장치

- 지정가 주문만 사용 (매수 ask+0.5%, 매도 bid−1% 상한), 스프레드 2% 초과 종목 제외
- 종목당 최대 $200, 최대 3종목, 일일 손실 $100 도달 시 신규진입 중단
- 손절 시 손실이 남은 일일한도의 절반을 넘지 않도록 수량 제한
- 15:00(ET) 이후 신규진입 금지, 15:50 전량 청산 — 오버나이트 보유 없음
- 당일 청산한 종목 재진입 금지, 돌파 후 +5% 넘게 오른 종목 추격 금지
- `state/KILL` 파일을 만들면 즉시 신규진입 중단 (`touch state/KILL`)

## 하루 흐름

1. 전날 밤이나 장 전: `python -m pennybot screen`. 처음 실행하면 전 종목 일봉을 받느라 오래 걸리고, 같은 날 다시 돌리면 캐시를 씁니다.
   전 종목 대신 일부만 보려면 `screener.universe_file`에 종목을 한 줄에 하나씩 적으세요.
2. 장중: `python -m pennybot run`. `watchlist: auto`면 스크리너 결과를 점수 순으로 감시합니다.
3. 주말: `state/screens/*.csv`와 매매 결과를 비교해서 팩터 가중치를 조정합니다.
