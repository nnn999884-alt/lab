"""설정 로딩: config.yaml + 환경변수(.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml
from dotenv import load_dotenv


@dataclass
class StrategyConfig:
    min_price: float = 0.50          # 너무 싼 종목(서브페니) 제외
    max_price: float = 5.00          # 페니주 상한
    min_gap_pct: float = 10.0        # 전일 대비 최소 상승률
    min_day_volume: float = 1_000_000
    min_dollar_volume: float = 1_000_000
    max_spread_pct: float = 2.0      # 호가 스프레드 상한
    opening_range_minutes: int = 15  # 개장 후 고가 돌파 기준 구간
    rvol_lookback_bars: int = 20
    min_bar_rvol: float = 2.0        # 직전 봉 거래량 / 평균
    stop_loss_pct: float = 5.0
    take_profit_pct: float = 12.0
    trailing_stop_pct: float = 4.0
    max_hold_minutes: int = 120
    min_volume_pace: float = 2.0     # 피벗 돌파: 당일 거래량이 평소 페이스의 N배
    pivot_stop_buffer_pct: float = 3.0   # 피벗 아래 3% 이탈 시 손절 (돌파 실패)
    no_new_entries_after: str = "15:00"   # 미 동부시간
    flatten_at: str = "15:50"             # 장 마감 전 전량 청산


@dataclass
class RiskConfig:
    max_position_usd: float = 200.0
    max_open_positions: int = 3
    max_daily_loss_usd: float = 100.0
    max_trades_per_day: int = 10
    entry_slippage_pct: float = 0.5   # 지정가 = ask * (1 + x%)
    exit_slippage_pct: float = 1.0    # 지정가 = bid * (1 - x%)


@dataclass
class ScreenerConfig:
    universe_file: str = ""          # 비우면 SEC 상장종목 전체 (OTC 제외)
    min_price: float = 0.50
    max_price: float = 5.00
    min_dollar_vol_20: float = 300_000   # 터지기 전이라 장중 기준보다 낮게
    max_ret_5: float = 0.50              # 5일 +50% 넘으면 이미 군중 진입
    min_ret_120: float = -0.85           # 6개월 -85% 이하 = 상습 희석 의심
    min_dist_to_pivot: float = -0.02     # 피벗 -2% ~ +15% 아래
    max_dist_to_pivot: float = 0.15
    catalyst_top_k: int = 60             # 공시 조회할 상위 후보 수
    top_n: int = 15                      # 최종 감시종목 수
    workers: int = 4
    weights: dict = field(default_factory=dict)   # scorer.DEFAULT_WEIGHTS 덮어쓰기
    catalyst: dict = field(default_factory=dict)  # edgar.evaluate 기간 설정 덮어쓰기

    def filter_dict(self) -> dict:
        return {k: getattr(self, k) for k in (
            "min_price", "max_price", "min_dollar_vol_20", "max_ret_5", "min_ret_120",
            "min_dist_to_pivot", "max_dist_to_pivot")}


@dataclass
class TossConfig:
    base_url: str = "https://openapi.tossinvest.com"
    token_path: str = "state/toss_token.json"   # 프로세스 간 토큰 공유 (client 당 유효 토큰 1개)
    client_id: str = ""
    client_secret: str = ""
    account: str = ""                           # accountSeq (GET /api/v1/accounts)
    # 그룹별 초당 한도를 프로세스끼리 나눠 쓴다 (합이 1 이하). 둘 다 90%를 쓰면 합쳐서 한도 초과
    rate_share_collector: float = 0.55
    rate_share_trader: float = 0.35
    rate_share_other: float = 0.1


@dataclass
class CollectorConfig:
    db_path: str = "state/market.db"
    markets: list = field(default_factory=lambda: ["NASDAQ", "NYSE", "AMEX"])
    min_price: float = 0.20          # 수집은 매매 범위보다 넓게
    max_price: float = 10.0
    price_poll_seconds: int = 60     # 유니버스 전체 현재가 (200개씩 묶음 조회)
    stall_restart_minutes: int = 15  # 메인 루프가 이만큼 통째로 멈추면 프로세스 재시작 (run_forever 가 다시 켬). 0 = 끔
    watch_size: int = 120            # 호가·1분봉을 촘촘히 받을 종목 수
    orderbook_poll_seconds: int = 60
    candle_sync_minutes: int = 30    # 감시 종목 1분봉 동기화 주기 (세션 종료 시에도 실행)
    ranking_poll_minutes: int = 10   # 휴장·애프터마켓
    ranking_poll_live_seconds: int = 60   # 데이·프리·정규장: 랭킹이 계속 바뀌므로 1분마다
    edgar_poll_minutes: int = 1
    runner_min_pct: float = 30.0     # 정규장 종가 기준 이만큼 오른 종목을 급등주로 기록
    news_poll_minutes: int = 2
    precursor: bool = True           # 급등 전조 점수 (일봉: 장 마감 후 전 종목, 장중: 점수 높은 종목 1분마다)
    precursor_min_score: int = 5     # 장중 감시·알림 대상 일봉 점수 (최대 10)
    precursor_live_max: int = 80     # 장중 감시 종목 수 상한 (1분에 이만큼 1분봉 조회)
    precursor_rank_max: int = 40     # 목록 밖이라도 지금 랭킹(급등률·거래대금·거래량)에 든 종목을 이만큼 더 장중 감시
    fast_scan_seconds: int = 3      # 거래대금·거래량 실시간 랭킹을 이 간격으로 봐서 새로 든 종목을 바로 실시간 구독 (0 = 끔)
    realtime_alerts: bool = True     # 웹소켓 체결로 실시간 알림 (계정당 연결 2개 중 1개 사용, 끊기면 1분 확인으로 대체)
    halt_alerts: bool = True         # 뉴스 대기 거래정지가 뜨면 바로 알림 + 실시간 구독 (재개 순간 거래 터짐을 잡게)
    pre_open_watch: bool = True      # 03:55 ET: 데이마켓에서 움직인 종목을 '프리 개장 주목' 으로 다시 알리고 실시간 구독 고정
    rt_min_dollar_after: float = 2e4
    rt_surge_after: float = 2.5
    rt_min_dollar_3m: float = 0.0
    second_wave_min_minutes: int = 5
    second_wave_max_drop_pct: float = 8.0
    rt_cooldown_seconds: int = 180     # 같은 종목 실시간 알림 재발송 최소 간격 (기존 600초는 너무 느림)
    rt_min_dollar: float = 5e4       # 최근 60초 거래대금 이상이거나
    rt_min_turnover: float = 0.5     # 최근 60초 회전율(%) 이상이고 (이때도 거래대금은 rt_min_turn_dollar 이상)
    rt_min_turn_dollar: float = 1e4  # 발행주식이 아주 적으면 $2천으로도 회전율이 차서 — 회전율 조건의 최소 거래대금
    rt_surge: float = 3.5            # 최근 60초 거래량이 그 종목 직전 30분 분당 평균의 이만큼 배 이상이면 알림 (등락률은 안 봄)
    spike_min_dollar: float = 5000.0  # 1분 급등·급락 이벤트: 그 2분 거래대금이 이만큼은 돼야 (얇은 체결 소음 제외)
    # 애프터마켓은 거래가 느릿느릿 쌓여서 1분 급등(spike_up) 기준(+4%)에 거의 안 걸린다 — 정규장 종가 대비
    # 누적으로 이만큼 올랐으면 따로 잡는다 (early20 의 애프터마켓 버전)
    after_move_pct: float = 10.0
    # '사부작사부작' 지속 관심: 한 번 크게 터지진 않지만 거래대금 랭킹에 꾸준히 올라와 있는 종목 (poll_rankings 가
    # 이미 채우는 테이블을 재사용 — API 호출 추가 없음). 숫자는 백테스트 전 초안.
    steady_window_hours: float = 2.0     # 이 시간 동안의 랭킹 기록을 본다
    steady_rank_cutoff: int = 30         # 유니버스 내 거래대금 랭킹 상위 N위 안에 들었는지
    steady_min_ratio: float = 0.5        # 그 기간 폴링 중 이 비율 이상 랭킹에 있었으면 '꾸준함'
    steady_min_polls: int = 6           # 폴링 기록이 이보다 적으면(세션 시작 직후) 판단 보류
    steady_cooldown_hours: float = 1.0   # 같은 종목 재알림 간격
    precursor_daily_alert: int = 10  # 장 마감 뒤 점수 계산이 끝나면 상위 N종목 목록 알림 (0 = 끔)
    vsignal: bool = True             # 가상 매매 알림 (실제 주문 없음) — 전략 정의는 pennybot/vstrat.py
    keep_days: int = 20            # 고가 +keep_min_run% 급등주는 이 거래일 수만큼 계속 감시 (며칠 뒤 다시 오르는 경우)
    keep_min_run: float = 50.0
    vsignal_near_tp: float = 2.0     # 고점 수익이 (익절 − 이만큼)%p 이상 갔다가
    vsignal_near_back: float = 2.0   # 고점 수익보다 이만큼 %p 밀리면 정리 (목표 앞에서 못 뚫고 빠질 때). 0 = 끔
    vsignal_strategies: list = field(default_factory=lambda: ["S1", "S2", "S3", "S4", "S5", "S6"])
    archive_1m: bool = True          # 정규장이 끝나면 오늘 감시·매매한 종목의 하루치 1분봉을 research.db 에 저장 (나중에 검증·보완용)
    short_data: bool = True          # 공매도(FINRA)·대차(IBKR)·SSR·Reg SHO·거래정지 수집
    borrow_poll_minutes: int = 30    # IBKR 대차 파일은 15분마다 갱신
    halts_poll_minutes: int = 1
    short_daily_poll_minutes: int = 60   # FINRA·SSR·Reg SHO 일별 파일 (나오면 한 번만 저장)
    news_feeds: list = field(default_factory=list)   # 비우면 news.DEFAULT_FEEDS
    universe_refresh_hours: int = 24
    research_auto: bool = True       # 휴장 중(주말 포함) 틈틈이 python -m pennybot research 를 자동으로 다시 돌려 과거 급등주 패턴(분석 탭)을 최신화
    research_refresh_days: int = 7   # 이 날짜 간격으로 (휴장 중에만 시작, 장중엔 안 건드림)


@dataclass
class TradingConfig:
    """세션 매매 엔진 (python -m pennybot trade). 숫자는 모두 백테스트 전 초안."""
    db_path: str = "state/trading.db"
    paper_cash: float = 2000.0
    min_price: float = 0.50
    max_price: float = 5.00
    max_spread_pct: float = 3.0          # 세션별 설정에 없는 세션의 기본값
    # 세션마다 참가자·유동성이 다르다. 정규장 유동성을 100 이라 하면 프리장 80·애프터 50·데이마켓(한국 낮,
    # 미국인 거의 없이 알고리즘 위주) 30 정도로 보고, 유동성이 낮을수록 스프레드 상한을 느슨하게 (반비례).
    # 숫자는 전부 백테스트 전 초안, 조정 필요.
    max_spread_pct_by_session: dict = field(default_factory=lambda: {
        "REGULAR": 1.5, "PRE": 2.0, "AFTER": 3.0, "DAY": 5.0})
    entry_cutoff_minutes: int = 30       # 데이마켓 종료 30분 전부터 신규 매수 중단
    scan_minutes: int = 5            # (예전 설정, scan_seconds 가 우선)
    scan_seconds: int = 5           # 전략 스캔 간격 (후보 호가는 웹소켓 실시간)
    forced_exit_minutes: int = 60        # 정규장 개장 후 N분에 전량 청산
    entry_ttl_seconds: int = 180         # 매수 지정가 미체결 시 취소
    entry_reprice_limit: int = 1         # 취소 전에 최우선 매도호가로 한 번 따라가 볼 횟수 (0 = 예전처럼 바로 취소)
    entry_chase_pct: float = 2.0         # 그때 허용하는 추격폭 (처음 지정가 대비 %). 넘으면 체결 포기
    fill_flush_seconds: int = 15
    chase_guard: bool = True              # 고점 −1% 이내이면서 VWAP +3% 이상(수직 추격)이면 진입 건너뜀
    chase_guard_high_pct: float = 1.0     # '고점 근처' 기준 (%)
    chase_guard_vwap_pct: float = 3.0     # 'VWAP 위' 기준 (%)
    add_on: bool = True                   # 보유 종목이 이익 중일 때 수급 신호가 또 오면 추가매수
    add_on_min_gain_pct: float = 3.0      # 추가매수 최소 평가이익 (%)
    add_on_max_per_symbol: int = 1        # 종목당 추가매수 횟수
    add_on_window_minutes: int = 10       # 최근 이 시간 안 수급 신호만
    add_on_qty_ratio: float = 0.5         # 기존 수량의 이 비율만큼 추가
    signal_alerts: bool = True           # 후보가 잡혀 주문 직전에 텔레그램으로 '수급 신호' 알림
    signal_alert_minutes: int = 10       # 같은 종목·같은 셋업 신호는 이 간격으로만         # 부분 체결 알림을 이 시간만큼 모아 한 줄로 보낸다 (1주씩 체결되면 알림 폭주)
    stop_loss_pct_override: float = 50.0  # 0 이 아니면 셋업 손절 대신 진입가 × (1 − x%) 하드 손절 (50 = −50%)
    no_rebound_minutes: int = 0          # 0 = 끔. 체결 후 이 시간이 지나도 반등·매집이 없으면 시간 손절
    no_rebound_skip_day: bool = False    # 데이마켓 진입 건은 '반등·매집 없음' 규칙을 적용하지 않음 (낮에는 수급 신호가 없어서)
    no_rebound_high_pct: float = 3.0     # '반등'으로 인정하는 고점 기준 (진입가 대비 %)
    take_profit_pct_override: float = 0.0  # 0 이 아니면 셋업 목표가 대신 진입가 × (1 + x%)
    trail_from_high_pct: float = 0.0     # 0 이 아니면 고점 대비 이만큼 밀리면 익절 (2 = −2%)
    trail_min_gain_pct: float = 10.0      # 고점이 진입가보다 이만큼(%, +) 오른 뒤에만 트레일링 익절 (0 = 제한 없음)
    flatten_kst: str = ""                # "08:50" 이면 매일 이 시각(KST) 전량 청산 (빈 값 = morning_flatten_et 사용)
    entry_cutoff_kst: str = ""           # "08:20" 이면 이 시각~flatten 사이 신규 진입 중단
    max_unfilled_entries: int = 0        # 0 = 끔. (예전: 같은 종목 3번 미체결이면 그날 제외 — 사용자 요청으로 삭제)
    exit_reprice_seconds: int = 60       # 매도 지정가 재호가 주기
    exit_slippage_pct: float = 1.0       # 강제 청산 매도가 = bid × (1 − x%)
    max_exit_reprices: int = 3           # 정규장에서 이 횟수 넘게 안 팔리면 최종 청산가로
    final_exit_slippage_pct: float = 5.0 # 최종 청산 지정가 = bid × (1 − x%). 호가가 아예 없을 때만 시장가
    day_open_wait_minutes: int = 15      # 데이마켓 개장 직후 호가가 얇을 때는 매수하지 않음
    min_book_ratio: float = 0.3          # 최우선 매도 잔량이 주문 수량의 30% 이상일 때만 (아니면 수량 축소)
    stale_alert_minutes: int = 30        # 강제 청산 시각이 이만큼 지나도 남아 있으면 알림
    stale_exit_hours: float = 3.0        # 체결가가 이 시간 동안 stale_move_pct 넘게 안 움직이면 '변동없음'으로 청산
    stale_move_pct: float = 1.0          # 이 % 안에서만 움직였으면 '변동없음'으로 친다
    decline_exit_hours: float = 3.5      # 손절가까진 안 가도, 매집(수급) 없이 신고점을 이 시간 동안 못 만들면 청산
    morning_flatten_et: str = "09:00"    # 정규장 전 이 시각(ET)부터는 전략 상관없이 전량 강제 청산
    session_transition_buffer_minutes: int = 3   # 세션 전환 전후 이 시간 동안은 장통합 호가 오류 때문에 손절 보류
    fee_pct: float = 0.1                 # 매매 수수료 % (실거래는 시작할 때 토스 /commissions 값으로 덮어씀)
    use_stream: bool = True              # 토스 웹소켓(호가·체결·내 주문 이벤트). 끊기면 자동으로 REST
    stream_max_age_seconds: float = 5.0  # 웹소켓 호가가 이보다 오래되면 REST 호가
    order_poll_fallback_seconds: int = 30  # 웹소켓 연결 중에도 주문을 이 주기로는 REST 확인
    # 서버 손절 안전망: 보유 중에는 토스 조건주문(모든 세션 발동)으로 손절가 아래에 매도를 걸어 둔다.
    # PC·봇이 꺼져도 동작. 평소에는 봇의 손절이 먼저 나가도록 손절가보다 buffer 만큼 아래
    backstop: bool = True
    backstop_buffer_pct: float = 3.0
    backstop_slippage_pct: float = 8.0   # 발동 시 지정가 = 감시가 × (1 − 8%) (급락 중에도 체결되게)
    backstop_check_seconds: int = 60
    # 리스크
    max_position_usd: float = 300.0
    max_position_pct: float = 0.05       # 계좌의 5%
    risk_per_trade_pct: float = 0.01     # 손절 시 손실 계좌의 1%
    max_open_positions: int = 8
    max_daily_loss_usd: float = 150.0
    max_weekly_loss_usd: float = 400.0   # 최근 7일 실현 손실이 이만큼이면 신규 매수 중단 (0 = 끔)
    loss_streak_stop: int = 4            # N연속 손실이면 (0 = 끔)
    loss_streak_cooldown_hours: int = 24 # 마지막 손실 후 이 시간 동안 신규 매수 쉼
    auto_disable_trades: int = 20        # 최근 N회 평균 손익이 음수면 그 전략 자동 정지
    regime_symbol: str = "IWM"           # 소형주 시장 상태 (데이마켓에서도 거래됨)
    regime_drop_pct: float = 1.5         # 데이마켓 중 IWM 이 전일 종가 대비 이만큼 빠지면 신규 매수 중단
    notify_webhook: str = ""             # 디스코드/슬랙 웹훅 (환경변수 PENNYBOT_WEBHOOK 우선)
    telegram_token: str = ""             # 텔레그램 봇 토큰·내 chat id: 환경변수 PENNYBOT_TELEGRAM_TOKEN / _CHAT
    telegram_chat: str = ""
    daily_report: bool = True            # 영업일이 끝나면 일일 리포트 (알림 + state/reports/ 파일)
    daily_report_time_kst: str = "07:30" # 한국 시간 이 시각 이후에 보냄 (정규장은 새벽 5~6시에 끝나므로). "00:00" = 끝나자마자
    strategies: dict = field(default_factory=lambda: {
        # gap_risk_pct: 밤사이 최악의 갭(%) — 수량은 손절폭이 아니라 이 갭을 기준으로 정한다
        "runner_overnight": {"enabled": True, "budget_pct": 0.40, "max_positions": 4,
                             "min_runner_pct": 30.0, "min_close_loc": 0.6, "min_rotation": 0.3,
                             "min_day_change_pct": -8.0, "max_day_change_pct": 15.0,
                             "take_profit_pct": 12.0, "stop_loss_pct": 10.0,
                             "entry_discount_pct": 0.5,  # 신호가 뜬 가격보다 3% 낮게 지정가 (체결률은 낮아지지만 더 싸게)
                             "gap_risk_pct": 35.0, "sell_before_open_minutes": 30,
                             "exit_after_open_minutes": 30,
                             # 익절가에서 절반만 팔고, 나머지는 고점 대비 −8% 트레일링 (강제 청산 시각까지)
                             "scale_out_pct": 0.5, "trail_pct": 8.0,
                             # 과거 검증(180일, 급등일 940건): 공매도 비율 50%+ 또는 평소(5일 평균) 대비 1.5배+ 급증한 날은
                             # 다음 날 시가 수익률이 뚜렷이 나빴음 (오버나이트라 특히 중요) → 그런 날은 거름
                             "max_short_ratio": 0.5, "max_short_surge": 1.5},
        "dilution_catalyst": {"enabled": True, "budget_pct": 0.30, "max_positions": 3,
                              "max_runup_pct": 15.0, "entry_discount_pct": 0.5,
                              "take_profit_pct": 10.0, "stop_loss_pct": 8.0, "gap_risk_pct": 25.0,
                              "sell_before_open_minutes": 90},
        "day_ignition": {"enabled": True, "budget_pct": 0.30, "max_positions": 3,
                         "min_insider_usd": 10000, "min_move_pct": 5.0, "max_move_pct": 25.0,
                         "max_pullback_pct": 4.0, "entry_discount_pct": 0.5,
                         "take_profit_pct": 10.0, "stop_loss_pct": 7.0, "gap_risk_pct": 25.0,
                         "sell_before_open_minutes": 150},
        "day_dip": {"enabled": False, "budget_pct": 0.10, "max_positions": 2,
                    "dip_pct": 12.0, "stop_loss_pct": 8.0, "retrace": 0.5, "take_profit_pct": 10.0,
                    "no_news_days": 3, "stall_minutes": 10, "gap_risk_pct": 25.0},
        # 유일한 '장중 전용' 전략 — 정규장 중에 새로 터지는 공시·내부자 매수·실시간 수급으로 산다.
        # 나머지는 전부 데이마켓에만 사서 정규장 중엔 신규 매수를 안 하기 때문에 따로 둠. 숫자 전부 백테스트 전 초안.
        "regular_momentum": {"enabled": True, "budget_pct": 0.20, "max_positions": 3,
                             "min_insider_usd": 10000, "min_move_pct": 3.0, "max_move_pct": 20.0,
                             "max_pullback_pct": 4.0, "signal_window_minutes": 5,
                             # 정규장 마감(애프터 시작)이라고 거기서 끊지 않고 다음 데이마켓 시작 전까지 캡 → 8시간까지 둬도 됨
                             "entry_discount_pct": 1.0, "max_hold_minutes": 480,
                             "take_profit_pct": 8.0, "stop_loss_pct": 6.0, "gap_risk_pct": 15.0},
        # 데이·프리·정규·애프터 상관없이 실시간 수급 신호(거래 터짐 등)가 뜨면 바로 들어가서 목표가에 바로 턴다.
        # 숫자 전부 백테스트 전 초안 — 다른 전략들과 달리 세션 전환을 기다리지 않고 자주 돌 수 있어서 예산을 작게 잡음.
        "scalp": {"enabled": True, "budget_pct": 0.15, "max_positions": 3,
                 "signal_window_minutes": 3, "entry_discount_pct": 1.0, "max_hold_minutes": 120,
                 "take_profit_pct": 8.0, "stop_loss_pct": 6.0, "gap_risk_pct": 20.0,
                 "take_profit_pct_override": 10.0,
                 "scale_out_pct": 0.5, "trail_pct": 2.0},
        # scalp 과 같은 '세션 안 가리는' 전략이지만 반응하는 신호가 다르다 — poll_steady_flow 가 잡는 '매집'
        # (한 번에 안 터지고 꾸준히 붙는 수급) 신호에 반응해서 목표가·손절가 안 걸리면 다음 데이마켓 시작
        # 직전까지 길게 들고 간다. 숫자 전부 백테스트 전 초안.
        "steady_trend": {"enabled": True, "budget_pct": 0.15, "max_positions": 3,
                        "signal_window_minutes": 30, "entry_discount_pct": 1.0,
                        "take_profit_pct": 15.0, "stop_loss_pct": 8.0, "gap_risk_pct": 20.0},
        # 설계 문서(2026-10-28)의 당일 셋업 5개. 숫자는 백테스트 전 초안이고, 청산은 공통 규칙이 맡는다.
        "squeeze": {"enabled": True, "budget_pct": 0.10, "max_positions": 2, "entry_discount_pct": 1.0,
                    "min_short_ratio": 0.40, "min_borrow_fee": 30.0, "min_dollar": 2e6, "max_spread_pct": 3.0,
                    "take_profit_pct": 20.0, "stop_loss_pct": 50.0, "gap_risk_pct": 25.0,
                    "scale_out_pct": 0.5, "trail_pct": 10.0},
        "news": {"enabled": True, "budget_pct": 0.10, "max_positions": 2, "entry_discount_pct": 1.0,
                 "window_minutes": 60, "min_dollar_3m": 2e5, "max_spread_pct": 2.0,
                 "max_runup_after_signal_pct": 30.0,
                 "take_profit_pct": 8.0, "stop_loss_pct": 50.0, "gap_risk_pct": 25.0},
        "session_gap": {"enabled": True, "budget_pct": 0.10, "max_positions": 2, "entry_discount_pct": 1.0,
                        "discount_pct": 3.0, "min_dollar": 1e6, "max_spread_pct": 3.0,
                        "take_profit_pct": 3.0, "stop_loss_pct": 50.0, "gap_risk_pct": 20.0},
        "accumulation": {"enabled": True, "budget_pct": 0.10, "max_positions": 2, "entry_discount_pct": 1.0,
                         "box_pct": 5.0, "max_atr_ratio": 0.7, "min_vol_ratio": 1.3,
                         "take_profit_pct": 20.0, "stop_loss_pct": 50.0, "gap_risk_pct": 25.0},
        "reversal": {"enabled": True, "budget_pct": 0.10, "max_positions": 2, "entry_discount_pct": 1.0,
                     "min_drop_pct": 35.0, "min_dollar": 5e6, "recover_pct": 15.0,
                     "take_profit_pct": 12.0, "stop_loss_pct": 50.0, "gap_risk_pct": 25.0},
        "pullback": {"enabled": False, "budget_pct": 0.40, "max_positions": 3, "max_candidates": 3,
                     "take_profit_pct": 10.0, "stop_loss_pct": 50.0, "scale_out_pct": 0.5, "trail_pct": 2.0,
                     "gap_risk_pct": 25.0},
        "rebound": {"enabled": False, "budget_pct": 0.30, "max_positions": 2, "max_candidates": 2,
                    "take_profit_pct": 12.0, "stop_loss_pct": 50.0, "gap_risk_pct": 25.0},
        "surge": {"enabled": False, "budget_pct": 0.30, "max_positions": 2, "max_candidates": 2,
                  "take_profit_pct": 20.0, "stop_loss_pct": 50.0, "scale_out_pct": 0.5, "trail_pct": 5.0,
                  "gap_risk_pct": 25.0},
        "pump": {"enabled": False, "budget_pct": 0.20, "max_positions": 2, "max_candidates": 2,
                 "entry_discount_pct": 0.5, "min_day_change": 50.0, "min_dollar": 1e6, "min_vol_ratio": 5.0,
                 "pullback_pct": 5.0, "min_session_minutes": 30,
                 "take_profit_pct": 20.0, "stop_loss_pct": 50.0, "scale_out_pct": 0.5, "trail_pct": 5.0,
                 "gap_risk_pct": 25.0},
    })

    def spread_cap(self, session: str) -> float:
        return self.max_spread_pct_by_session.get(session, self.max_spread_pct)


@dataclass
class Config:
    mode: str = "paper"              # paper | live
    watchlist: list[str] = field(default_factory=list)
    poll_seconds: int = 20
    paper_cash: float = 2000.0
    state_dir: str = "state"
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    screener: ScreenerConfig = field(default_factory=ScreenerConfig)
    collector: CollectorConfig = field(default_factory=CollectorConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    toss: TossConfig = field(default_factory=TossConfig)
    sec_user_agent: str = ""
    dashboard_password: str = ""     # 있으면 대시보드에 비밀번호 (외부 주소로 열 때 필수)

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    @property
    def auto_watchlist(self) -> bool:
        return self.watchlist == ["AUTO"]


def _trading(d: dict) -> TradingConfig:
    """전략별 설정은 기본값 위에 덮어쓴다 (일부 키만 바꿔도 되게). 사라진 키는 무시 (_known 과 동일)."""
    d = dict(d)
    over = d.pop("strategies", {}) or {}
    t = _known(TradingConfig, d)
    for name, params in over.items():
        t.strategies[name] = {**t.strategies.get(name, {}), **(params or {})}
    return t


def _known(cls, d: dict):
    """예전 설정 파일의 사라진 키(엔드포인트 경로 등)는 무시한다."""
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in d.items() if k in names})


def _watchlist(v) -> list[str]:
    """'auto' → 스크리너 결과 사용, 리스트 → 수동 감시종목."""
    if isinstance(v, str):
        return ["AUTO"] if v.lower() == "auto" else [v.upper()]
    return [s.upper() for s in v]


def load_config(path: str | Path = "config.yaml") -> Config:
    load_dotenv()
    raw: dict = {}
    p = Path(path)
    if p.exists():
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    cfg = Config(
        mode=raw.get("mode", "paper"),
        watchlist=_watchlist(raw.get("watchlist", "auto")),
        poll_seconds=int(raw.get("poll_seconds", 20)),
        paper_cash=float(raw.get("paper_cash", 2000.0)),
        state_dir=raw.get("state_dir", "state"),
        strategy=_known(StrategyConfig, raw.get("strategy", {})),
        risk=_known(RiskConfig, raw.get("risk", {})),
        screener=_known(ScreenerConfig, raw.get("screener", {})),
        collector=_known(CollectorConfig, raw.get("collector", {})),
        trading=_trading(raw.get("trading", {})),
        toss=_known(TossConfig, raw.get("toss", {})),
    )
    # 비밀값은 환경변수로만 받는다 (yaml에 커밋 금지)
    cfg.toss.client_id = os.getenv("TOSS_CLIENT_ID", "")
    cfg.toss.client_secret = os.getenv("TOSS_CLIENT_SECRET", "")
    cfg.toss.account = os.getenv("TOSS_ACCOUNT", "")
    cfg.sec_user_agent = os.getenv("SEC_USER_AGENT", "")
    cfg.trading.notify_webhook = os.getenv("PENNYBOT_WEBHOOK", cfg.trading.notify_webhook)
    cfg.trading.telegram_token = os.getenv("PENNYBOT_TELEGRAM_TOKEN", "")
    cfg.trading.telegram_chat = os.getenv("PENNYBOT_TELEGRAM_CHAT", "")
    cfg.dashboard_password = os.getenv("PENNYBOT_DASHBOARD_PASSWORD", "")

    # 실거래는 yaml + 환경변수 이중 확인
    if cfg.is_live and os.getenv("PENNYBOT_LIVE_CONFIRM") != "YES":
        raise SystemExit(
            "mode=live 이지만 PENNYBOT_LIVE_CONFIRM=YES 가 설정되지 않았습니다. 실거래를 중단합니다."
        )
    return cfg
