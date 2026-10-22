"""실행:
  python -m pennybot check                 # 토스 API 연결 점검 (토큰·장 캘린더·시세·세션별 1분봉)
  python -m pennybot accounts              # accountSeq 확인 → .env 의 TOSS_ACCOUNT
  python -m pennybot collect [--once]      # 24시간 세션별 데이터 수집 (state/market.db)
  python -m pennybot summarize --date D    # 영업일 D(미국 날짜)의 세션 요약 다시 만들기
  python -m pennybot trade [--once]        # 세션 매매 엔진 (mode: paper 기본, live 는 이중 확인)
  python -m pennybot dashboard [--demo]    # 로컬 대시보드 http://127.0.0.1:8050 (--demo: 예시 데이터)
  python -m pennybot probe-orders --symbol SYM --yes-real-order   # 토스 정정·취소 동작 실계좌 확인 (1주)
  python -m pennybot screen                # (구) Coil & Catalyst 스크리닝
  python -m pennybot run [--once]          # (구) 매매 루프
"""
from __future__ import annotations

import argparse
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone

from .brokers import PaperBroker, TossBroker
from .config import Config, load_config
from .data import YahooFeed
from .engine import Engine
from .toss_api import TossApiError, TossClient, us_price_str


def make_client(cfg: Config, role: str = "other") -> TossClient:
    t = cfg.toss
    share = {"collector": t.rate_share_collector, "trader": t.rate_share_trader}.get(role, t.rate_share_other)
    return TossClient(t.client_id, t.client_secret, t.account or None, t.token_path, t.base_url, rate_share=share)


def _feed(cfg: Config):
    """토스 키가 있으면 토스 시세, 없으면 공개 피드."""
    return TossBroker(make_client(cfg)) if cfg.toss.client_id else YahooFeed()


def _fundamentals(cfg: Config, client):
    """SEC_USER_AGENT 가 있으면 공시 본문 판정(희석 분석)까지, 없으면 None."""
    from .dilution import DilutionService
    from .trading.fundamentals import Fundamentals
    edgar = _edgar(cfg)
    dil = DilutionService(edgar, f"{cfg.state_dir}/edgar_cache") if edgar else None
    return dil, Fundamentals(client, dil)


def _edgar(cfg: Config):
    from .screener.edgar import EdgarClient
    if not cfg.sec_user_agent:
        logging.warning("SEC_USER_AGENT 미설정 → 공시 레이어 없이 진행합니다.")
        return None
    return EdgarClient(cfg.sec_user_agent)


def cmd_check(cfg: Config) -> None:
    from .sessions import SessionCalendar
    c = make_client(cfg)
    cal = SessionCalendar(c)
    now = datetime.now(timezone.utc)
    cal.refresh(now)
    print("장 캘린더 (KST):")
    for d in sorted(cal.days):
        for name, (s, e) in cal.days[d].sessions.items():
            print(f"  {d} {name:8s} {s:%m-%d %H:%M} ~ {e:%m-%d %H:%M}")
    print("현재 세션:", cal.classify(now))
    nasdaq = c.stocks_all("NASDAQ")
    print(f"NASDAQ 보통주 {len(nasdaq)}종목")
    sample = [s["symbol"] for s in nasdaq[:5]]
    for p in c.prices(sample):
        print(f"  {p['symbol']:6s} {p['lastPrice']:>10s}  {p.get('timestamp')}")
    if sample:
        sym = "AAPL"
        res = c.candles(sym, "1m", 200)
        ts = [datetime.fromisoformat(x["timestamp"]) - timedelta(seconds=30) for x in res.get("candles", [])]
        print(f"{sym} 최근 1분봉 200개의 세션 분포:", dict(Counter(cal.classify(t)[1] for t in ts)))
        print("  → DAY/PRE/AFTER 가 보이면 1분봉으로 장외 세션 과거치를 받을 수 있습니다.")


def cmd_accounts(cfg: Config) -> None:
    for a in make_client(cfg).accounts():
        print(f"accountSeq={a['accountSeq']}  계좌={a['accountNo']}  유형={a['accountType']}")


def cmd_collect(cfg: Config, once: bool) -> None:
    from .collector import Collector, Store
    from .sessions import SessionCalendar
    client = make_client(cfg, "collector")
    dil, fund = _fundamentals(cfg, client)
    from .news import NewsClient
    col = Collector(client, Store(cfg.collector.db_path), SessionCalendar(client), cfg.collector,
                    dil.edgar if dil else None, dilution=dil, fundamentals=fund,
                    news=NewsClient(cfg.collector.news_feeds or None, cfg.sec_user_agent or "pennybot/1.0"))
    if once:
        col.tick()
    else:
        col.run()


def cmd_summarize(cfg: Config, date: str) -> None:
    from .collector import Store
    print(Store(cfg.collector.db_path).summarize(date), "행")


def cmd_trade(cfg: Config, once: bool) -> None:
    from pathlib import Path

    from .sessions import SessionCalendar
    from .trading import Control, Journal, LiveExchange, PaperExchange, TossMarket, Trader
    from .trading.market import MarketView
    from .trading.notify import Notifier

    client = make_client(cfg, "trader")
    t = cfg.trading
    market, stream = TossMarket(client), None
    if t.use_stream:
        from . import stream as ws
        if ws.available():
            # 계정당 웹소켓 2개까지 → 매매 봇만 1개. 모의 모드는 시세만 (모의 주문은 토스에 없으므로)
            stream = ws.TossStream(client, cfg.toss.account if cfg.is_live else None).start()
            market = ws.StreamMarket(stream, market, t.stream_max_age_seconds)
        else:
            logging.warning("websocket-client 미설치 → REST 폴링만 사용 (pip install websocket-client)")
    if cfg.is_live:
        ex = LiveExchange(client, market)
        logging.warning("⚠️  실거래 모드: 토스증권 계좌로 실제 주문이 나갑니다.")
    else:
        ex = PaperExchange(market, t.paper_cash, Path(cfg.state_dir) / "paper_account.json")
    cal = SessionCalendar(client)
    if not Path(cfg.collector.db_path).exists():
        logging.warning("수집기 DB(%s)가 없습니다. 'python -m pennybot collect' 를 같이 돌려야 전략이 후보를 찾습니다.",
                        cfg.collector.db_path)
        from .collector import Store
        Store(cfg.collector.db_path)
    _, fund = _fundamentals(cfg, client)
    trader = Trader(ex, Journal(t.db_path, mode=cfg.mode), cal,
                    MarketView(cfg.collector.db_path, client, cal, ex), t, Control(cfg.state_dir),
                    Notifier(t.notify_webhook), fund=fund, stream=stream)
    if once:
        trader.tick()
    else:
        trader.run()


def cmd_probe_orders(cfg: Config, symbol: str, confirmed: bool) -> None:
    """토스의 정정·취소 동작을 실계좌로 한 번 확인한다.
    시장가의 절반 가격에 1주 지정가 매수(체결되지 않을 가격) → 가격 정정 → 취소, 각 id 의 상태를 기록."""
    import json
    import time as _t
    from pathlib import Path
    if not confirmed:
        raise SystemExit("실제 주문이 나갑니다 (1주, 체결되지 않을 가격, 곧바로 취소). "
                         "확인했다면 --yes-real-order 를 붙여 다시 실행하세요.")
    c = make_client(cfg)
    ob = c.orderbook(symbol)
    bids = ob.get("bids") or []
    if not bids:
        raise SystemExit(f"{symbol} 호가가 없습니다. 장이 열린 시간(데이·프리·정규)에 유동성 있는 종목으로 하세요.")
    bid = float(bids[0]["price"])
    log = []

    def snap(label, ids):
        for i in ids:
            try:
                d = c.get_order(i)
                log.append({"step": label, "id": i, "status": d["status"],
                            "filled": d["execution"]["filledQuantity"], "price": d.get("price")})
            except Exception as e:
                log.append({"step": label, "id": i, "error": str(e)})
        print(json.dumps(log[-len(ids):], ensure_ascii=False))

    o1 = c.create_order({"symbol": symbol, "side": "BUY", "orderType": "LIMIT", "quantity": "1",
                         "price": us_price_str(max(bid * 0.5, 0.01)), "timeInForce": "DAY"})["orderId"]
    _t.sleep(2); snap("주문", [o1])
    o2 = c.modify_order(o1, max(bid * 0.55, 0.01))["orderId"]
    _t.sleep(3); snap("정정 후", [o1, o2])
    o3 = (c.cancel_order(o2) or {}).get("orderId")
    for k in range(5):
        _t.sleep(2); snap(f"취소 후 {2 * (k + 1)}초", [x for x in (o1, o2, o3) if x])
    open_ids = {o["orderId"] for o in (c.list_orders("OPEN") or {}).get("orders", [])}
    log.append({"step": "미체결 목록", "open": sorted(open_ids & {o1, o2, o3} - {None})})
    out = Path(cfg.state_dir) / "probe_orders.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(log, ensure_ascii=False, indent=2))
    print(f"\n결과 저장: {out} — 정정 후 원주문이 REPLACED 인지, 취소 후 어느 id 가 CANCELED 가 되는지 확인하세요.")


def cmd_dashboard(cfg: Config, port: int, demo: bool) -> None:
    from .dashboard import serve
    if demo:
        from .dashboard.demo import build
        cfg.trading.db_path, cfg.collector.db_path = build("state_demo")
        cfg.state_dir = "state_demo"
        print("⚠️  예시 데이터(state_demo/)로 띄웁니다. 실제 계좌·시장 데이터가 아닙니다.")
    serve(cfg, port)


def cmd_backtest(cfg: Config, args) -> None:
    """수집기 DB 를 재생해 매매 엔진을 그대로 돌린다 (토스 API 접속 없음)."""
    from datetime import date, datetime
    from pathlib import Path

    from .backtest import Backtest, ReplayClient, apply_params, format_report, replay_calendar, _DB

    db = cfg.collector.db_path
    if not Path(db).exists():
        raise SystemExit(f"수집기 DB({db})가 없습니다. 먼저 'python -m pennybot collect' 로 데이터를 모으세요.")
    rng = _DB(db).q("SELECT MIN(trade_date), MAX(trade_date) FROM prices")
    if not rng or not rng[0][0]:
        raise SystemExit("수집기 DB 에 가격 기록이 없습니다.")
    start = date.fromisoformat(args.start or rng[0][0])
    end = date.fromisoformat(args.end or rng[0][1])
    params = apply_params(cfg.trading, args.param)
    fund = None
    if args.with_edgar:
        # 희석 프로필은 날짜 기준으로 잘리므로(제출일 ≤ 재생일) 그대로 써도 미래 데이터가 섞이지 않는다
        from .dilution import DilutionService
        from .trading.fundamentals import Fundamentals
        edgar = _edgar(cfg)
        if edgar:   # 발행주식 수는 재생 시점 값이 없어 쓰지 않는다 (ReplayClient.stocks → 빈 값)
            fund = Fundamentals(ReplayClient(db, replay_calendar(_DB(db), start, end), datetime.now),
                                DilutionService(edgar, f"{cfg.state_dir}/edgar_cache"))
    out = Path(args.out or f"{cfg.state_dir}/backtests/{datetime.now():%Y%m%d-%H%M%S}")
    rep = Backtest(db, cfg.trading, start, end, out, step_seconds=args.step, fee_pct=args.fee,
                   half_spread_pct=args.half_spread, train_frac=args.train_frac, fund=fund, params=params).run()
    print(format_report(rep))
    print(f"\n결과: {out}/report.json, 매매 기록: {out}/backtest.db (대시보드 '백테스트' 탭)")


def cmd_screen(cfg: Config) -> None:
    from .screener import run_screen
    picks = run_screen(cfg.screener, _feed(cfg), _edgar(cfg), cfg.state_dir)
    print(f"\n{'순위':>4} {'종목':<6} {'총점':>6} {'응축':>6} {'공시':>6} {'현재가':>8} {'피벗':>8}  사유")
    for i, p in enumerate(picks, 1):
        print(f"{i:>4} {p.symbol:<6} {p.score:>6.1f} {p.coil:>6.1f} {p.catalyst:>+6.1f} "
              f"{p.price:>8.4f} {p.pivot:>8.4f}  {', '.join(p.reasons)}")


def cmd_run(cfg: Config, once: bool) -> None:
    if cfg.is_live:
        broker = TossBroker(make_client(cfg))
        logging.warning("⚠️  실거래 모드: 토스증권 계좌로 실제 주문이 나갑니다.")
    else:
        broker = PaperBroker(_feed(cfg), cash=cfg.paper_cash)
    engine = Engine(cfg, broker)
    if once:
        engine.tick()
    else:
        engine.run()


def main() -> None:
    ap = argparse.ArgumentParser(description="미국 페니주 자동매매 (토스증권)")
    ap.add_argument("command", nargs="?", default="collect",
                    choices=["check", "accounts", "collect", "summarize", "trade", "dashboard", "probe-orders",
                             "backtest", "screen", "run"])
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--once", action="store_true", help="collect/run: 한 번만 실행")
    ap.add_argument("--date", help="summarize: 미국 영업일 YYYY-MM-DD")
    ap.add_argument("--port", type=int, default=8050, help="dashboard 포트")
    ap.add_argument("--demo", action="store_true", help="dashboard: 예시 데이터로 미리보기")
    ap.add_argument("--symbol", help="probe-orders: 확인에 쓸 종목 (유동성 있는 종목)")
    ap.add_argument("--yes-real-order", action="store_true", help="probe-orders: 실제 주문 확인")
    ap.add_argument("--start", help="backtest: 시작 영업일 (기본: DB 첫날)")
    ap.add_argument("--end", help="backtest: 끝 영업일 (기본: DB 마지막 날)")
    ap.add_argument("--step", type=int, default=60, help="backtest: 재생 간격(초)")
    ap.add_argument("--fee", type=float, default=0.1, help="backtest: 수수료 %%")
    ap.add_argument("--half-spread", type=float, default=0.75, help="backtest: 호가 기록 없을 때 가정 반스프레드 %%")
    ap.add_argument("--train-frac", type=float, default=0.7, help="backtest: 학습 구간 비율")
    ap.add_argument("--param", action="append", help="backtest: 설정 덮어쓰기 (예: runner_overnight.trail_pct=6)")
    ap.add_argument("--with-edgar", action="store_true", help="backtest: SEC 희석 프로필 사용 (네트워크 필요)")
    ap.add_argument("--out", help="backtest: 결과 폴더")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    cfg = load_config(args.config)
    try:
        _dispatch(cfg, args)
    except KeyboardInterrupt:
        print("\n중지했습니다.")
    except TossApiError as e:
        raise SystemExit(f"토스 API 오류: {e}\n" + _HINT.get(e.status, ""))
    except ValueError as e:
        if "TOSS_" not in str(e):
            raise
        raise SystemExit(f"{e}\n.env 파일에 토스 Open API 키를 넣으세요 (README '내 컴퓨터에서 실행하기').")
    except Exception as e:
        import requests
        if isinstance(e, requests.ConnectionError):
            raise SystemExit(f"인터넷 연결 오류: {e}")
        raise


_HINT = {
    401: "키(TOSS_CLIENT_ID/SECRET)가 틀렸거나 만료됐습니다. .env 를 확인하세요.",
    403: "허용 IP 에 이 컴퓨터의 공인 IP 가 등록돼 있지 않습니다. 토스증권 설정 > Open API > 허용 IP 관리에서 등록하세요."
         " (집 인터넷 IP 는 가끔 바뀝니다)",
}


def _dispatch(cfg, args) -> None:
    {
        "check": lambda: cmd_check(cfg),
        "accounts": lambda: cmd_accounts(cfg),
        "collect": lambda: cmd_collect(cfg, args.once),
        "summarize": lambda: cmd_summarize(cfg, args.date),
        "trade": lambda: cmd_trade(cfg, args.once),
        "dashboard": lambda: cmd_dashboard(cfg, args.port, args.demo),
        "probe-orders": lambda: cmd_probe_orders(cfg, args.symbol, args.yes_real_order),
        "backtest": lambda: cmd_backtest(cfg, args),
        "screen": lambda: cmd_screen(cfg),
        "run": lambda: cmd_run(cfg, args.once),
    }[args.command]()


if __name__ == "__main__":
    main()
