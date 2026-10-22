# 토스증권 Open API 정리 (명세 v1.2.17)

원본: [docs/toss-openapi-1.2.17.json](toss-openapi-1.2.17.json). 공식 문서 <https://developers.tossinvest.com/docs>,
최신 명세 `https://openapi.tossinvest.com/openapi-docs/latest/openapi.json`.
이 사본은 npm 패키지 `tossinvest-openapi-mcp@0.2.0`(MIT)에 번들된 공식 명세에서 가져왔습니다.
실거래 전에 최신본과 비교하세요.

## 우리 봇에 중요한 것

| 항목 | 명세 내용 | 봇에서 쓰는 방식 |
| --- | --- | --- |
| 인증 | `POST /oauth2/token` (form, client_credentials). refresh 없음. **client 당 유효 토큰 1개, 재발급 시 이전 토큰 즉시 무효** | `state/toss_token.json`으로 수집기·봇이 토큰 공유 |
| 응답 | 성공 `{"result": ...}`, 실패 `{"error": {requestId, code, message, data}}` | `TossApiError(code)` |
| 계좌 | `GET /api/v1/accounts` → `accountSeq`를 헤더 `X-Tossinvest-Account`에 | `.env`의 `TOSS_ACCOUNT` |
| 한도 | API 그룹별 토큰 버킷. 429 + `Retry-After`, `X-RateLimit-Limit/Remaining/Reset` (예시 10/초) | 그룹별 스로틀, 헤더 값의 90%로 자동 조정 |
| 세션 | `GET /api/v1/market-calendar/US`: 전일·당일·익일 영업일의 `dayMarket/preMarket/regularMarket/afterMarket` 시작·종료(KST), 휴장 세션은 null | `pennybot/sessions.py`의 정본 |
| 현재가 | `GET /api/v1/prices?symbols=` 최대 200개. **lastPrice만** (거래량·호가 없음) | 유니버스 전체를 1분마다 |
| 호가 | `GET /api/v1/orderbook?symbol=` 매수/매도 호가·잔량 | 감시 종목 1분마다 |
| 캔들 | `GET /api/v1/candles` `1m`/`1d`, 최대 200개, 최신순, `before`/`nextBefore` 페이지. 1분봉 timestamp는 **봉 종료 시각** | 봉 중간 시각으로 세션 판정 |
| 랭킹 | `GET /api/v1/rankings` 시장·토스 거래대금/거래량, 급등/급락 상위 100 | 주목도 기록 (개장 프리미엄 후보) |
| 유니버스 | `GET /api/v1/stocks/all?market=` 토스에서 거래 가능한 종목 전체 (NASDAQ 약 2,800) | 하루 1번 |
| 주문 | `POST /api/v1/orders` `symbol, side, orderType(LIMIT/MARKET), quantity, price, timeInForce, clientOrderId` | 지정가만 사용 |
| 주문 세션 | **세션 지정 파라미터 없음**. 접수 시점 세션에서 체결. `DAY` 미체결분은 정규장 종료 시 자동 취소 | 세션이 바뀌면 봇이 직접 취소·재주문 |
| 장 개시/마감 주문 | `OPG`는 현재 국내 전용, `CLS`(LOC)는 미국 LIMIT만 | 미국 개장가 주문(MOO)은 불가 |
| 가격 단위 | $1 미만 소수 4자리, $1 이상 2자리 (초과 절삭) | `us_price_str()` |
| 멱등성 | `clientOrderId` 최대 36자, 10분 유효 | 주문마다 생성, 5xx에도 주문 재전송 안 함 |
| 취소 | `POST /api/v1/orders/{id}/cancel` → 새 orderId 발급 | |
| 실시간 | 웹소켓 `wss://openapi-ws.tossinvest.com/ws/v1` (AsyncAPI 명세 별도) | 매매 봇이 1개 연결: `orderbook:us`·`trade:us`(보유·후보 종목), `personal:order`(실거래). `pennybot/stream.py` |

## 아직 실제 호출로 확인해야 하는 것

- 1분봉에 데이·프리·애프터마켓 봉이 들어 있는지, 과거 몇 일까지 페이지로 거슬러 올라갈 수 있는지.
  `python -m pennybot check`가 최근 200봉의 세션 분포를 출력합니다.
  들어 있다면 데이마켓 과거 데이터를 토스에서 바로 받을 수 있습니다.
- 그룹별 실제 초당 한도 (응답 헤더로 확인).
- 데이마켓 중 `/orderbook`이 데이마켓 호가를 주는지.

## 웹소켓 (pennybot/stream.py)

공식 AsyncAPI 명세는 프록시에 막혀 직접 받지 못했고, 공개 SDK(ongjin/tossinvest)의 정리본 기준입니다. 실제 연결로 확인 필요.

- 핸드셰이크 `Authorization: Bearer <REST 토큰>`. 401 이면 다음 연결에서 토큰 재발급, 403 은 허용 IP 미등록
- 선언형 구독: JSON 배열 1개가 전체 구독 (`[]` = 전부 해제). 연결당 100건(채널×종목) → 종목 49개(호가+체결) + 내 주문 1건
- 선언은 초당 5회 제한 → 2초에 한 번만, `rate-limit-exceeded` 면 ~1초 뒤 재선언, 거부된 종목은 빼고 재선언
- 클라이언트가 180초 동안 아무것도 안 보내면 서버가 끊음 → 60초마다 텍스트 `PING`
- 계정당 동시 2개, 넘으면 가장 오래된 연결이 끊김 → 수집기는 쓰지 않고 매매 봇만 1개
- 시세는 유실 가능 → 5초보다 오래된 호가는 쓰지 않고 REST 로. 주문 이벤트는 연결 안에서만 무손실 → 재연결 뒤 열린 주문 전부 REST 재조회,
  연결 중에도 30초마다 REST 확인
