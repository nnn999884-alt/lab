"""실행:
  python -m pennybot check                 # 토스 API 연결 점검 (토큰·장 캘린더·시세·세션별 1분봉)
  python -m pennybot accounts              # accountSeq 확인 → .env 의 TOSS_ACCOUNT
  python -m pennybot collect [--once]      # 24시간 세션별 데이터 수집 (state/market.db)
  python -m pennybot summarize --date D    # 영업일 D(미국 날짜)의 세션 요약 다시 만들기
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
from .toss_api import TossClient


def make_client(cfg: Config) -> TossClient:
    t = cfg.toss
    return TossClient(t.client_id, t.client_secret, t.account or None, t.token_path, t.base_url)


def _feed(cfg: Config):
    """토스 키가 있으면 토스 시세, 없으면 공개 피드."""
    return TossBroker(make_client(cfg)) if cfg.toss.client_id else YahooFeed()


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
    client = make_client(cfg)
    col = Collector(client, Store(cfg.collector.db_path), SessionCalendar(client), cfg.collector, _edgar(cfg))
    if once:
        col.tick()
    else:
        col.run()


def cmd_summarize(cfg: Config, date: str) -> None:
    from .collector import Store
    print(Store(cfg.collector.db_path).summarize(date), "행")


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
                    choices=["check", "accounts", "collect", "summarize", "screen", "run"])
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--once", action="store_true", help="collect/run: 한 번만 실행")
    ap.add_argument("--date", help="summarize: 미국 영업일 YYYY-MM-DD")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    cfg = load_config(args.config)
    {
        "check": lambda: cmd_check(cfg),
        "accounts": lambda: cmd_accounts(cfg),
        "collect": lambda: cmd_collect(cfg, args.once),
        "summarize": lambda: cmd_summarize(cfg, args.date),
        "screen": lambda: cmd_screen(cfg),
        "run": lambda: cmd_run(cfg, args.once),
    }[args.command]()


if __name__ == "__main__":
    main()
