# pennybot — 미국 페니주 자동매매 (토스증권 Open API)

갭업한 미국 페니주($0.5~$5)가 **개장 후 15분 고가를 거래량과 함께 돌파**하면 매수하고,
손절·익절·트레일링 스탑·장마감 전 청산으로 정리하는 데이트레이딩 봇입니다.

> ⚠️ 페니주는 변동성·스프레드·거래정지·상장폐지 위험이 매우 큽니다.
> 이 코드는 수익을 보장하지 않으며, 반드시 **모의매매(paper)로 충분히 검증한 뒤** 소액으로 시작하세요.

## 구조

```
pennybot/
  brokers/toss.py   토스증권 Open API 클라이언트 (OAuth2, Bearer, X-Tossinvest-Account)
  brokers/paper.py  모의 브로커 (실제 주문 없음)
  data.py           토스 키 없이 쓰는 공개 시세 피드 (Yahoo)
  strategy.py       갭업 + 시가범위 돌파 전략, 청산 규칙
  risk.py           포지션 크기 / 일일 손실한도 / 거래횟수 / 킬스위치
  engine.py         매매 루프 (지정가 주문 → 15초 미체결 시 취소)
```

## 시작하기

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml   # 감시종목, 전략/리스크 값 수정
cp .env.example .env                 # 토스 API 키 입력
python -m pytest                     # 테스트
python -m pennybot -v                # 모의매매 실행 (장중에만 동작)
python -m pennybot --once -v         # 한 번만 실행해서 동작 확인
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

## 감시종목 고르기

장 전(프리마켓)에 갭업 상위 페니주를 `watchlist` 에 넣으세요.
토스 Open API에 종목 스크리너가 있다면 `engine.py` 에서 watchlist를 자동으로 채우도록 확장할 수 있습니다.
