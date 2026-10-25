"""24시간 세션별 데이터 수집기.

하는 일 (주기는 config.collector):
  - 유니버스: 토스 /stocks/all(NASDAQ·NYSE·AMEX 보통주) → 현재가로 가격대 필터 → 하루 1번
  - 현재가: 유니버스 전체를 200개씩 묶어 매분 (데이마켓 포함 4 세션 모두)
  - 감시 종목(watch): 공시 뜬 종목 > 랭킹 종목 > 급변 종목 순으로 N개 선정
      → 호가 스냅샷 매분, 1분봉은 주기적으로 + 세션이 바뀔 때마다 동기화
  - 랭킹: 시장/토스 거래대금, 급등/급락 스냅샷
  - SEC 최신 공시(Form 4, 8-K, 424B*): 접수 시각 → 어느 세션에 공개됐는지 기록
  - 애프터마켓이 끝나면 그 영업일 세션 요약(session_summary) 생성
  - 휴장 중(주말 포함)엔 python -m pennybot research 를 틈틈이 자동으로 다시 돌려 과거 급등주 패턴(분석 탭)을 최신화 (research_refresh_days)

한 작업이 실패해도 나머지는 계속 돈다. 재시작해도 같은 키는 덮어쓰므로 안전하다.
"""
from __future__ import annotations

import json
import logging
import statistics
import time
from datetime import datetime, timedelta, timezone

from ..config import CollectorConfig
from ..dilution import classify
from ..screener import edgar as ed
from ..sessions import AFTER, CLOSED, DAY, ET, PRE, REGULAR, SessionCalendar
from ..toss_api import TossApiError
from .store import Store

log = logging.getLogger(__name__)

# '방금 일어난 일' 피드에 띄울 공시 판정 (형식적인 등록 효력·S-3 제출은 뺀다)
FEED_EVENTS = {"ATM_NEW", "ATM_TERMINATED", "PUBLIC_OFFERING", "REGISTERED_DIRECT", "PRIVATE_PLACEMENT",
               "SECURITIES_PURCHASE_AGREEMENT", "WARRANT_REPRICE", "WARRANT_INDUCEMENT", "CONVERTIBLE", "EQUITY_LINE",
               "OFFERING_TERMINATED", "REGISTRATION_WITHDRAWN", "BUYBACK", "COMPLIANCE_REGAINED", "FDA_APPROVAL",
               "MATERIAL_CONTRACT", "DELISTING_NOTICE", "REVERSE_SPLIT", "TAKEDOWN", "GOING_CONCERN"}

RANKINGS = [  # (type, duration)
    ("MARKET_TRADING_AMOUNT", "realtime"),
    ("TOSS_SECURITIES_TRADING_AMOUNT", "realtime"),
    ("MARKET_TRADING_VOLUME", "realtime"),
    ("TOSS_SECURITIES_TRADING_VOLUME", "realtime"),
    ("TOP_GAINERS", "1d"),
    ("TOP_LOSERS", "1d"),
]
EDGAR_FORMS = ["4", "8-K", "6-K", "424B4", "424B5", "424B3", "S-1", "F-1", "S-3", "F-3", "RW", "EFFECT"]
MAX_CANDLE_PAGES = 8   # 200봉 × 8 = 약 26시간 (하루 4 세션 전체)


def iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).isoformat(timespec="seconds")


class Collector:
    def __init__(self, client, store: Store, calendar: SessionCalendar, cfg: CollectorConfig,
                 edgar_client: ed.EdgarClient | None = None,
                 clock=lambda: datetime.now(timezone.utc), dilution=None, fundamentals=None, news=None,
                 short=None, notifier=None, link=None, root_cfg=None):
        self.c, self.store, self.cal, self.cfg = client, store, calendar, cfg
        self.root_cfg = root_cfg          # 전체 Config (research.run() 이 collector/trading 양쪽 설정을 다 필요로 함)
        self._research_thread = None
        self.edgar = edgar_client
        self.dilution = dilution          # DilutionService: 공시 본문으로 희석 이벤트 판정
        self.fund = fundamentals          # Fundamentals: 급등주 희석 프로필·펌프 신호
        self.news = news                  # NewsClient: 보도자료 피드
        self.short = short                # ShortData: 공매도·대차·SSR·Reg SHO·거래정지
        self.notifier = notifier          # 급등 전조 알림 (텔레그램·웹훅)
        self.link = link                  # () -> 대시보드 외부 주소
        self._pre_thread = None
        self._arch_thread = None
        from pathlib import Path as _P
        from ..settings import Watcher
        self._settings = Watcher(str(_P(cfg.db_path).parent))   # 대시보드 설정 탭 (state/settings.json)
        self._pre_tried = None
        self._ev_last: dict = {}                  # (종목, 종류) → 마지막 이벤트 시각
        self._rank_prev: dict = {}                # 랭킹 종류 → {종목: 순위}
        self._rank_skip: tuple = (None, set())    # (기준일, 일봉 점수를 못 매긴 랭킹 종목)
        self._day_cache: dict = {}                # 종목 → (그날 시작, 그날 1분봉) — 가상 매매 VWAP 용
        self._fast_pc: tuple = (None, {})         # (기준일, 종목 → 전날 종가)
        self._fast_hot: tuple = (None, set())     # (기준일, 랭킹 훑기로 새로 잡은 종목) → 실시간 구독·1분 감시·가상 매매에 넣는다
        # 실시간 체결 (웹소켓)
        self._rt = None                           # TossStream (체결만)
        self._rt_syms: set = set()                # 지금 구독할 종목
        self._rt_win: dict = {}                   # 종목 → deque[(초, 가격, 수량)] 최근 60초
        self._rt_min: dict = {}                   # 종목 → {분: 수량} 최근 40분 (평소 거래량)
        self._rt_seeded: set = set()              # 1분봉으로 평소 거래량을 채운 종목
        self._rt_fired: dict = {}                 # 종목 → 마지막 실시간 신호 시각(초)
        self._rt_shares: dict = {}
        self._rb_levels: dict = {}                # 며칠 전 급등주 → 그 뒤 최고가 (넘기 전에 접근·임박 알림)
        self._rb_sent: set = set()                # 오늘 보낸 (종목, 단계)
        self._rt_last: dict = {}                  # 종목 → 직전 체결가 (선을 아래에서 위로 넘는 순간만)
        self._keep_today: set = set()
        self._vopen: dict = {}                    # 종목 → [가상 포지션] (실시간 체결로 익절·손절·목표 근처 밀림 확인)
        self._vhi: dict = {}                      # 가상 포지션 id → 실시간 고점
        self._vorders: dict = {}                  # 종목 → [걸려 있는 가상 지정가 주문]
        import queue
        self._rt_q = queue.Queue()
        self._short_done: set[tuple[str, str]] = set()   # (종류, 날짜) 이미 받은 일별 파일
        self._seen_news: set[str] = set()
        self.clock = clock
        self.universe: dict[str, dict] = {}       # symbol -> {market, name, last}
        self.cik_to_symbol: dict[int, str] = {}
        self.watch: list[str] = []
        self.recent_filers: dict[str, datetime] = {}
        self._next: dict[str, datetime] = {}
        try:    # 재시작해도 휴장 중 과거 패턴 자동 갱신이 바로 다시 돌지 않도록, 지난 완료 시각을 파일에서 이어받는다
            last = datetime.fromisoformat(json.loads(self._research_marker().read_text(encoding="utf-8"))["last"])
            self._next["research_refresh"] = last + timedelta(days=cfg.research_refresh_days)
        except (OSError, ValueError, KeyError):
            pass
        self._last_session: tuple | None = None
        self._seen_accessions: set[str] = set()

    def _on(self, alert: str) -> bool:
        """설정 탭의 알림 켜기·끄기."""
        return bool(self._settings.get()["alerts"].get(alert, True))

    def write_status(self, now: datetime, session: str) -> None:
        """설정 탭 '시스템 상태' 용: 웹소켓·구독·보유 (state/collector_status.json)."""
        from pathlib import Path
        rt = self._rt
        st = {"ts": iso(now), "session": session, "universe": len(self.universe),
              "ws": {"on": bool(rt), "connected": bool(rt and rt.connected), "healthy": bool(rt and rt.healthy),
                     "last_frame_sec": round(rt.clock() - rt.last_frame, 1) if rt and rt.last_frame else None,
                     "connects": rt.connects if rt else 0, "subscribed": len(rt.desired()) if rt else 0,
                     "error": rt.last_error if rt else ""},
              "watch": len(self._rt_syms), "open": sum(len(v) for v in self._vopen.values()),
              "orders": sum(len(v) for v in self._vorders.values())}
        p = Path(self.cfg.db_path).parent / "collector_status.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        import os
        os.replace(tmp, p)

    def _mq(self, store=None):
        """sql → [dict] (알림 성과·브리핑·매물대 모듈용)."""
        db = (store or self.store).db

        def q(sql, args=()):
            cur = db.execute(sql, args)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        return q

    def _rq(self):
        """연구용 1분봉 보관본 (state/research.db) — 수집기 1분봉이 없는 알림의 감지가용."""
        import sqlite3
        from pathlib import Path
        from .store import ro_uri
        p = Path(self.cfg.db_path).parent / "research.db"
        if not p.exists():
            return None

        def q(sql, args=()):
            con = sqlite3.connect(ro_uri(str(p)), uri=True)
            try:
                cur = con.execute(sql, args)
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]
            finally:
                con.close()
        return q

    def send_briefing(self, now: datetime, trade_date) -> str | None:
        """03:45 ET (프리마켓 열기 15분 전) 한 번: 장 시작 전 브리핑. state/briefing.json 에도 남긴다 (대시보드)."""
        from pathlib import Path
        from .. import briefing
        t = now.astimezone(ET)
        if not (t.hour == 3 and 45 <= t.minute < 55) or getattr(self, "_brief_sent", None) == trade_date:
            return None
        self._brief_sent = trade_date
        b = briefing.build(self._mq(), self.cfg.db_path, self._rq(), now)
        (Path(self.cfg.db_path).parent / "briefing.json").write_text(json.dumps(b, ensure_ascii=False), encoding="utf-8")
        txt = briefing.text(b)
        if self.notifier and self._on("briefing"):
            self.notifier.send(txt)
        log.info("장 시작 전 브리핑 보냄 (%s)", trade_date)
        return txt

    def send_perf(self, now: datetime, trade_date: str) -> str | None:
        """애프터장 마감 뒤: 그날 알림이 실제로 얼마나 갔나 (텔레그램)."""
        from .. import alertperf
        y, m, d = map(int, trade_date.split("-"))
        end = datetime(y, m, d, 20, tzinfo=ET).astimezone(timezone.utc)
        mq, rq = self._mq(), self._rq()
        rows = alertperf.rows(mq, rq, (end - timedelta(hours=24)).isoformat(), end.isoformat())
        txt = alertperf.summary_text(rows, alertperf.stats(mq, rq, today=trade_date), trade_date)
        if txt and self.notifier and self._on("perf"):
            self.notifier.send(txt)
        log.info("알림 성과 요약 %s: %d건", trade_date, len(rows))
        return txt

    def write_account(self, now: datetime) -> None:
        """토스 실제 계좌 (보유·평가손익·매수 가능) → state/account.json (대시보드 '실제 계좌'). 계좌 권한이 없으면 조용히 멈춤."""
        import os
        from pathlib import Path
        if getattr(self, "_acct_off", False) or not hasattr(self.c, "holdings"):
            return
        try:
            h = self.c.holdings() or {}
        except Exception as e:
            self._acct_off = True
            log.warning("계좌 조회 안 됨 — 대시보드 실제 계좌 꺼짐: %s", e)
            return
        try:
            bp = self.c.buying_power("USD") or {}
        except Exception:
            bp = {}
        p = Path(self.cfg.db_path).parent / "account.json"
        tmp = p.with_suffix(".acct.tmp")
        tmp.write_text(json.dumps({"ts": iso(now), "holdings": h, "buying_power": bp}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)

    # ---------- 휴장 중 과거 패턴 자동 갱신 ----------
    def _research_marker(self):
        from pathlib import Path
        return Path(self.cfg.db_path).parent / "research_auto.json"

    def _maybe_research_refresh(self, now: datetime) -> None:
        """장이 닫혀 있을 때(주말 포함) 틈틈이 python -m pennybot research 를 다시 돌려 과거 급등주 패턴(분석 탭)을 최신화.
        research.db 를 archive_1m 과 동시에 쓰지 않도록, 그 스레드가 돌고 있으면 이번 주기는 건너뛴다 (다음 주기에 다시 시도)."""
        if not self.cfg.research_auto or not self.root_cfg:
            return
        if self._research_thread and self._research_thread.is_alive():
            return
        if self._arch_thread and self._arch_thread.is_alive():
            return
        if not self._due("research_refresh", now, timedelta(days=self.cfg.research_refresh_days)):
            return
        import threading
        self._research_thread = threading.Thread(target=self._run_research_refresh, daemon=True, name="research-refresh")
        self._research_thread.start()

    def _run_research_refresh(self) -> None:
        try:
            from .. import research
            log.info("휴장 중 과거 패턴 데이터 자동 갱신 시작")
            # 수집기·매매봇 몫(rate_share_other)을 넘지 않게, 장이 다시 열리면 중단 (받은 만큼은 캐시에 남아 다음 번에 이어받음)
            still_closed = lambda: self.cal.classify(self.clock())[1] != CLOSED
            rep = research.run(self.root_cfg, rate_share=self.root_cfg.toss.rate_share_other, should_stop=still_closed)
            self._research_marker().write_text(json.dumps({"last": iso(datetime.now(timezone.utc))}), encoding="utf-8")
            log.info("휴장 중 과거 패턴 데이터 자동 갱신 %s", "완료" if rep is not None else "중단 (장 재개, 다음 주기에 이어받음)")
        except Exception:
            log.exception("과거 패턴 데이터 자동 갱신 실패 (다음 주기에 다시 시도)")

    # ---------- 스케줄 ----------
    def _due(self, name: str, now: datetime, every: timedelta) -> bool:
        if now >= self._next.get(name, datetime.min.replace(tzinfo=timezone.utc)):
            self._next[name] = now + every
            return True
        return False

    def _run(self, name: str, fn, *args) -> None:
        try:
            fn(*args)
        except TossApiError as e:
            log.warning("%s 실패: %s", name, e)
        except (ConnectionError, TimeoutError, OSError) as e:      # SEC·FINRA 등 일시적인 네트워크 오류: 다음 주기에 다시
            log.warning("%s 실패 (네트워크): %s", name, str(e)[:160])
        except Exception as e:
            if type(e).__module__.startswith(("requests", "urllib3")):
                log.warning("%s 실패 (네트워크): %s", name, str(e)[:160])
            else:
                log.exception("%s 실패", name)

    def tick(self) -> None:
        now = self.clock()
        trade_date, session = self.cal.classify(now)
        c = self.cfg

        if self._due("universe", now, timedelta(hours=c.universe_refresh_hours)) or not self.universe:
            self._run("universe", self.refresh_universe, now)

        if self._due("status", now, timedelta(seconds=15)):
            self._run("status", self.write_status, now, session)
        if self._due("account", now, timedelta(seconds=10 if session != CLOSED else 300)):
            self._run("account", self.write_account, now)
        if self._last_session is None:
            self._run("catch_up", self.catch_up_summaries)
            if c.vsignal:                                # 꺼져 있는 사이 애프터장 마감이 지나간 가상 포지션 정리
                # 휴장 중에 켜졌으면 애프터장 마감이 이미 지난 것 → 전부, 장중이면 20시간 넘은 것만
                self._run("vsignals_stale", self.check_vsignals, now, session, True, None if session == CLOSED else 20)
            self._run("filing_details", self.backfill_details)
        # 세션이 바뀌면: 직전 세션의 1분봉을 마저 받고, 애프터마켓이 끝났으면 요약
        cur = (trade_date, session)
        if self._last_session and cur != self._last_session:
            prev_date, prev_session = self._last_session
            log.info("세션 변경 %s/%s → %s/%s", prev_date, prev_session, trade_date, session)
            self._run("candles", self.sync_candles, now)
            if prev_session == AFTER and prev_date:
                self._run("summary", self.summarize, prev_date.isoformat())
            if prev_session == REGULAR and session == AFTER and c.archive_1m:
                self._run("archive", self.start_archive, trade_date)
            if prev_session == AFTER and c.vsignal:              # 애프터장이 끝나면 남은 가상 포지션 정리
                self._run("vsignals_eod", self.check_vsignals, now, session, True)
            if prev_session == AFTER and prev_date:
                self._run("perf", self.send_perf, now, prev_date.isoformat())
        self._last_session = cur

        if self.edgar and self._due("edgar", now, timedelta(minutes=c.edgar_poll_minutes)):
            self._run("edgar", self.poll_edgar, now)
        if self.news and self._due("news", now, timedelta(minutes=c.news_poll_minutes)):
            self._run("news", self.poll_news, now)
        if self.short and c.short_data:      # 휴장 중에도 (일별 파일은 장 마감 뒤에 나온다)
            if self._due("short_daily", now, timedelta(minutes=c.short_daily_poll_minutes)):
                self._run("short_daily", self.poll_short_daily, now)
            if self._due("borrow", now, timedelta(minutes=c.borrow_poll_minutes)):
                self._run("borrow", self.poll_borrow, now)
            if self._due("halts", now, timedelta(minutes=c.halts_poll_minutes)):
                self._run("halts", self.poll_halts)
        if c.precursor:
            self._run("precursor_daily", self.ensure_precursor_daily, trade_date, session)
            if c.pre_open_watch and session == DAY:
                self._run("pre_open", self.pre_open_watch, now, trade_date)
            if session == DAY:
                self._run("briefing", self.send_briefing, now, trade_date)
            if session in (DAY, PRE, REGULAR) and self._due("precursor_live", now, timedelta(seconds=60)):
                self._run("precursor_live", self.poll_precursor_live, now, trade_date, session)
        if session == CLOSED:
            self._maybe_research_refresh(now)
            return
        every = timedelta(seconds=c.ranking_poll_live_seconds) if session != AFTER else timedelta(minutes=c.ranking_poll_minutes)
        if self._due("rankings", now, every):
            self._run("rankings", self.poll_rankings, now, trade_date, session)
            self._run("watch", self.select_watch, now, trade_date)
        if self._due("prices", now, timedelta(seconds=c.price_poll_seconds)):
            self._run("prices", self.poll_prices, now, trade_date, session)
            if c.vsignal:
                self._run("vsignals", self.check_vsignals, now, session)
        if self._due("orderbook", now, timedelta(seconds=c.orderbook_poll_seconds)):
            self._run("orderbook", self.poll_orderbooks, now, trade_date, session)
        if self._due("candles", now, timedelta(minutes=c.candle_sync_minutes)):
            self._run("candles", self.sync_candles, now)

    def run(self, sleep=time.sleep) -> None:
        log.info("수집기 시작 (DB=%s)", self.cfg.db_path)
        import threading
        if self.cfg.fast_scan_seconds and self.cfg.precursor:
            threading.Thread(target=self._fast_loop, name="rank-scan", daemon=True).start()
        if self.cfg.realtime_alerts and self.cfg.precursor:
            from .. import stream
            if stream.available():
                self._rt = stream.TossStream(self.c, None, channels=("trade",), on_trade=self._on_trade).start()
                threading.Thread(target=self._rt_worker, name="rt-alert", daemon=True).start()
                log.info("실시간 체결 알림 켜짐 (웹소켓, 최대 %d종목)", self._rt.max_symbols)
            else:
                log.warning("websocket-client 패키지가 없어 실시간 알림 없이 1분 확인만 씁니다")
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("수집 틱 실패 (계속 진행)")
            sleep(max(1.0, 5 - (time.time() % 5)))

    # ---------- 작업 ----------
    def refresh_universe(self, now: datetime) -> None:
        listed: dict[str, dict] = {}
        for market in self.cfg.markets:
            for s in self.c.stocks_all(market) or []:
                listed[s["symbol"]] = {"market": market, "name": s.get("name", "")}
        prices = {p["symbol"]: float(p["lastPrice"]) for p in self.c.prices(sorted(listed))}
        self.universe = {sym: {**info, "last": prices[sym]} for sym, info in listed.items()
                         if sym in prices and self.cfg.min_price <= prices[sym] <= self.cfg.max_price}
        snap = now.date().isoformat()
        self.store.put_universe([{"snapshot_date": snap, "symbol": s, "market": v["market"],
                                  "name": v["name"], "last": v["last"]} for s, v in self.universe.items()])
        if self.edgar:
            for r in self.edgar.ticker_map():
                t = str(r.get("ticker", "")).upper()
                if t in self.universe:
                    self.cik_to_symbol[int(r["cik"])] = t
        log.info("유니버스 %d종목 (상장 %d 중 $%.2f~$%.2f)", len(self.universe), len(listed),
                 self.cfg.min_price, self.cfg.max_price)
        try:
            info = self.c.stocks(sorted(self.universe)) or []
            self.store.put_stock_info([{"symbol": s["symbol"], "name": s.get("name"),
                                        "shares": float(s["sharesOutstanding"]) if s.get("sharesOutstanding") else None,
                                        "list_date": s.get("listDate"), "updated": snap} for s in info])
            log.info("종목 정보(발행주식) %d종목", len(info))
        except Exception as e:
            log.warning("종목 정보 실패: %s", e)

    def _event_due(self, now: datetime, sym: str, kind: str) -> bool:
        last = self._ev_last.get((sym, kind))
        return not last or (now - last).total_seconds() >= 600

    def _event(self, now: datetime, sym: str, kind: str, text: str, value: float | None = None) -> None:
        """대시보드 '방금 일어난 일' 피드. 같은 종목·종류는 10분에 한 번만."""
        key = (sym, kind)
        last = self._ev_last.get(key)
        if last and (now - last).total_seconds() < 600:
            return
        self._ev_last[key] = now
        self.store.put_events([{"ts": iso(now), "symbol": sym, "kind": kind, "text": text, "value": value}])

    def poll_prices(self, now: datetime, trade_date, session: str) -> None:
        prev = {s: v.get("last") for s, v in self.universe.items()}
        rows = []
        for p in self.c.prices(sorted(self.universe)):
            rows.append({"symbol": p["symbol"], "ts": iso(now), "trade_date": trade_date.isoformat(),
                         "session": session, "last": float(p["lastPrice"]), "price_ts": p.get("timestamp")})
            if p["symbol"] in self.universe:
                self.universe[p["symbol"]]["last"] = float(p["lastPrice"])
        self.store.put_prices(rows)
        from ..trading.strategies import SESSION_KO
        for r in rows:
            a = prev.get(r["symbol"])
            if a and a >= 0.2 and r["last"]:
                chg = (r["last"] / a - 1) * 100
                kind = "spike_up" if chg >= 4 else "spike_dn" if chg <= -7 else None
                if not kind or not self._event_due(now, r["symbol"], kind):
                    continue
                vol = ""
                try:                                  # 그 순간 1분봉 거래량·거래대금 (이벤트 날 때만 조회)
                    cs = (self.c.candles(r["symbol"], "1m", 3) or {}).get("candles", [])
                    v = sum(float(cd["volume"]) for cd in cs[:2])
                    d = sum(float(cd["volume"]) * float(cd["closePrice"]) for cd in cs[:2])
                except Exception:
                    continue
                if d < self.cfg.spike_min_dollar:
                    self._ev_last[(r["symbol"], kind)] = now      # 얇은 체결로 튄 것 → 10분 동안 다시 안 봄
                    continue
                from ..trading.messages import money_s, qty_s
                vol = f" · 2분 거래량 {qty_s(v)}주 ({money_s(d)})"
                word = "급등" if kind == "spike_up" else "급락"
                name = (self.universe.get(r["symbol"]) or {}).get("name") or ""
                self._event(now, r["symbol"], kind,
                            f"{'⚡' if kind == 'spike_up' else '▼'} {r['symbol']} {name} 1분새 {chg:+.1f}% {word} ${r['last']:.4g}"
                            f" · {SESSION_KO.get(session, session)}{vol}", chg)

    def poll_orderbooks(self, now: datetime, trade_date, session: str) -> None:
        rows = []
        for sym in self.watch:
            try:
                ob = self.c.orderbook(sym)
            except TossApiError as e:
                log.debug("호가 실패 %s: %s", sym, e)
                continue
            bids, asks = ob.get("bids") or [], ob.get("asks") or []
            rows.append({
                "symbol": sym, "ts": iso(now), "trade_date": trade_date.isoformat(), "session": session,
                "bid": float(bids[0]["price"]) if bids else None,
                "bid_size": float(bids[0]["volume"]) if bids else None,
                "ask": float(asks[0]["price"]) if asks else None,
                "ask_size": float(asks[0]["volume"]) if asks else None,
                "levels": min(len(bids), len(asks)), "book_ts": ob.get("timestamp"),
            })
        self.store.put_quotes(rows)

    def poll_rankings(self, now: datetime, trade_date, session: str) -> None:
        rows = []
        for type_, duration in RANKINGS:
            res = self.c.rankings(type_, duration) or {}
            for r in res.get("rankings", []):
                pr = r.get("price") or {}
                rows.append({
                    "ts": iso(now), "trade_date": trade_date.isoformat(), "session": session,
                    "type": type_, "duration": duration, "rank": r["rank"], "symbol": r["symbol"],
                    "last": float(pr.get("lastPrice") or 0), "base": float(pr.get("basePrice") or 0),
                    "change_rate": float(pr["changeRate"]) if pr.get("changeRate") is not None else None,
                    "volume": float(r.get("tradingVolume") or 0), "amount": float(r.get("tradingAmount") or 0),
                    "ranked_at": res.get("rankedAt"),
                })
        self.store.put_rankings(rows)
        names = {"MARKET_TRADING_AMOUNT": "시장 거래대금", "TOSS_SECURITIES_TRADING_AMOUNT": "토스 거래대금",
                 "MARKET_TRADING_VOLUME": "시장 거래량", "TOSS_SECURITIES_TRADING_VOLUME": "토스 거래량",
                 "TOP_GAINERS": "급등률", "TOP_LOSERS": "급락률"}
        cur: dict[str, dict[str, int]] = {}
        for r in rows:
            if r["symbol"] in self.universe:               # 페니주 유니버스만 (대형주 순위 변화는 소음)
                cur.setdefault(r["type"], {})[r["symbol"]] = r["rank"]
        for typ, ranks in cur.items():
            before = self._rank_prev.get(typ)
            if before is not None and typ != "TOP_LOSERS":
                for sym, rk in ranks.items():
                    old_rk = before.get(sym)
                    if rk <= 10 and (old_rk is None or old_rk - rk >= 3):
                        moved = f"{old_rk}→{rk}위" if old_rk else f"{rk}위 신규 진입"
                        nm = (self.universe.get(sym) or {}).get("name") or ""
                        self._event(now, sym, f"rank:{typ}", f"📈 {sym} {nm} {names.get(typ, typ)} {moved}".replace("  ", " "), rk)
            self._rank_prev[typ] = ranks

    def select_watch(self, now: datetime, trade_date) -> None:
        """공시 > 랭킹 > 급변 순으로 감시 종목을 채운다 (모두 유니버스 안에서만)."""
        picked: list[str] = []

        def add(sym):
            if sym in self.universe and sym not in picked and len(picked) < self.cfg.watch_size:
                picked.append(sym)

        cutoff = now - timedelta(hours=24)
        for sym, t in sorted(self.recent_filers.items(), key=lambda x: x[1], reverse=True):
            if t >= cutoff:
                add(sym)
        for (sym,) in self.store.query(
                "SELECT symbol FROM rankings WHERE ts=(SELECT MAX(ts) FROM rankings) ORDER BY rank"):
            add(sym)
        # 급변: 이번 영업일 첫 현재가 대비 변동폭이 큰 순
        movers = self.store.query("""
            SELECT symbol, MIN(ts), MAX(ts) FROM prices WHERE trade_date=? GROUP BY symbol""",
                                  (trade_date.isoformat(),))
        first_last = []
        for sym, t0, t1 in movers:
            r = self.store.query("SELECT ts, last FROM prices WHERE symbol=? AND ts IN (?, ?)", (sym, t0, t1))
            vals = dict(r)
            if vals.get(t0):
                first_last.append((abs(vals[t1] / vals[t0] - 1), sym))
        for _, sym in sorted(first_last, reverse=True):
            add(sym)
        self.watch = picked
        log.info("감시 종목 %d개", len(picked))

    def sync_candles(self, now: datetime) -> None:
        counts: dict[str, int] = {}
        for sym in self.watch:
            last = self.store.latest_bar_ts(sym)
            before, rows = None, []
            for _ in range(MAX_CANDLE_PAGES):
                res = self.c.candles(sym, "1m", 200, before) or {}
                page = res.get("candles", [])
                stop = False
                for cd in page:
                    end = datetime.fromisoformat(cd["timestamp"].replace("Z", "+00:00"))
                    ts = iso(end)
                    if last and ts <= last:
                        stop = True
                        break
                    # 1분봉 timestamp 는 봉 종료 시각 → 봉 중간 시각으로 세션 판정
                    d, sess = self.cal.classify(end - timedelta(seconds=30))
                    counts[sess] = counts.get(sess, 0) + 1
                    rows.append({"symbol": sym, "ts": ts, "trade_date": d.isoformat() if d else None,
                                 "session": sess, "open": float(cd["openPrice"]), "high": float(cd["highPrice"]),
                                 "low": float(cd["lowPrice"]), "close": float(cd["closePrice"]),
                                 "volume": float(cd["volume"])})
                before = res.get("nextBefore")
                if stop or not before or not page or (not last and len(rows) >= 1440):
                    break
            self.store.put_bars(rows)
        if counts:
            # 토스 1분봉에 데이·프리·애프터 봉이 들어 있는지 여기서 바로 드러난다
            log.info("1분봉 동기화: 세션별 봉 수 %s", counts)

    def poll_edgar(self, now: datetime) -> None:
        rows = []
        for form in EDGAR_FORMS:
            for f in self.edgar.current(form):
                if f["accession"] in self._seen_accessions:
                    continue
                sym = self.cik_to_symbol.get(f["cik"])
                # Form 4 는 Issuer(회사) 행만, 그 외는 Filer 행만 의미가 있다
                if not sym or f["role"] not in ("Issuer", "Filer", "Subject"):
                    continue
                self._seen_accessions.add(f["accession"])
                d, sess = self.cal.classify(f["accepted"])
                # 피드에는 8-K 항목·Form 4 금액·본문이 없어서 submissions 와 원문에서 보충한다
                detail = self._lookup(f["cik"], f["accession"])
                buy, info = None, {}
                if f["form"] == "4" and detail:
                    from ..filing_detail import form4
                    xml = self._safe(lambda: self.edgar.form4_xml(f["cik"], detail))
                    if xml:
                        buy = ed.insider_buy_usd(xml)
                        info = form4(xml)
                evs = []
                if detail and f["form"] != "4":
                    if self.dilution:
                        evs = self._safe(lambda: self.dilution._events_for(f["cik"], detail)) or []
                        info = self.dilution.detail_for(f["accession"]) or {}
                    else:
                        evs = classify(detail.form, detail.items, "", detail.filed, detail.accession)
                    if not info and detail.items:
                        from ..filing_detail import extract
                        info = extract(detail.form, detail.items, "")
                direction = (1 if sum(e.direction for e in evs) > 0 else -1 if sum(e.direction for e in evs) < 0
                             else 0) if evs else (1 if buy and buy > 0 else 0)
                rows.append({"accession": f["accession"], "cik": f["cik"], "symbol": sym, "form": f["form"],
                             "items": detail.items if detail else "", "accepted": iso(f["accepted"]),
                             "trade_date": d.isoformat() if d else None, "session": sess,
                             "insider_buy_usd": buy, "events": ",".join(sorted({e.kind for e in evs})),
                             "direction": direction,
                             "amount": max((e.amount or 0 for e in evs), default=0) or None,
                             "detail": json.dumps(info, ensure_ascii=False) if info else None})
                self.recent_filers[sym] = now
        if rows:
            self.store.put_filings(rows)
            from ..filing_detail import EVENT_KO, summary
            for r in rows:
                evs = [k for k in (r["events"] or "").split(",") if k]
                key = [k for k in evs if k in FEED_EVENTS]
                insider = r["form"] == "4" and (r["insider_buy_usd"] or 0) >= 10000
                if not key and not insider:
                    continue                      # 등록 효력·S-3 제출·일반 8-K 같은 형식적인 공시는 피드에 안 띄운다
                if now - datetime.fromisoformat(r["accepted"]) > timedelta(hours=3):
                    continue                      # 재시작 직후 몰려 들어오는 지난 공시는 저장만
                tag = "▲호재" if r["direction"] > 0 else "▼악재" if r["direction"] < 0 else ""
                what = "내부자 장내 매수" if insider and not key else ", ".join(EVENT_KO.get(k, k) for k in key)
                info = json.loads(r["detail"]) if r.get("detail") else {}
                detail = summary(r["form"], info, (self.universe.get(r["symbol"]) or {}).get("last"))
                name = (self.universe.get(r["symbol"]) or {}).get("name") or ""
                self._event(now, r["symbol"], "filing",
                            f"📰 {r['symbol']} {name} {what} {tag}".replace("  ", " ").strip() + (f" · {detail}" if detail else ""),
                            r["direction"])
            log.info("새 공시 %d건 (유니버스 종목)", len(rows))

    def poll_news(self, now: datetime) -> None:
        from ..news import classify_news, item_id, tickers
        rows = []
        for src, it in self.news.fetch():
            nid = item_id(src, it)
            if nid in self._seen_news:
                continue
            self._seen_news.add(nid)
            syms = {t for t in tickers(f"{it.title} {it.summary}") if t in self.universe}
            if not syms:
                continue
            evs = classify_news(it.title, it.summary, it.published.date())
            d, sess = self.cal.classify(it.published)
            direction = sum(e.direction for e in evs)
            for sym in syms:
                rows.append({"id": nid, "symbol": sym, "source": src, "title": it.title[:300], "url": it.url,
                             "published": iso(it.published), "trade_date": d.isoformat() if d else None,
                             "session": sess, "events": ",".join(sorted({e.kind for e in evs})),
                             "direction": (direction > 0) - (direction < 0)})
                self.recent_filers[sym] = now
        if rows:
            self.store.put_news(rows)
            log.info("새 보도자료 %d건 (유니버스 종목)", len(rows))

    # ---------- 급등 전조 ----------
    def _expected_d0(self, trade_date, session):
        """전조 일봉 점수의 기준일: 장 마감 전(데이·프리·정규)이면 전 영업일, 마감 뒤면 그날."""
        if trade_date is None:
            return None
        if session in (DAY, PRE, REGULAR):
            days = [d for d, day in self.cal.days.items() if d < trade_date and REGULAR in day.sessions]
            if days:
                return max(days)
            d = trade_date - timedelta(days=1)
            while d.weekday() >= 5:
                d -= timedelta(days=1)
            return d
        return trade_date

    def start_archive(self, trade_date) -> None:
        """정규장이 끝나면 백그라운드로: 오늘 장중 감시·가상 매매한 종목의 하루치 1분봉(데이마켓~정규장)을 research.db 에 저장.
        이미 받은 날은 건너뛴다 (bars_1m_done 'full:<날짜>')."""
        if self._arch_thread and self._arch_thread.is_alive():
            return
        d0 = self.store.query("SELECT MAX(d0) FROM precursor_daily")
        if not d0 or not d0[0][0]:
            return
        import threading
        self._arch_thread = threading.Thread(target=self.archive_1m, args=(d0[0][0], trade_date.isoformat()),
                                             name="archive", daemon=True)
        self._arch_thread.start()

    def archive_1m(self, d0: str, d: str) -> int:
        from pathlib import Path
        from ..anatomy import fetch_day
        from ..research import ResearchDB
        store = Store(self.cfg.db_path)
        syms = sorted({r[0] for r in store.query("SELECT symbol FROM precursor_live WHERE d0=?", (d0,))}
                      | {r[0] for r in store.query("SELECT symbol FROM vsignals WHERE d0=?", (d0,))})
        rdb = ResearchDB(Path(self.cfg.db_path).parent / "research.db")
        n = 0
        for sym in syms:
            if rdb.q("SELECT 1 FROM bars_1m_done WHERE symbol=? AND d=?", (sym, f"full:{d}")):
                continue
            try:
                if fetch_day(self.c, rdb, sym, d0, d):
                    n += 1
            except Exception as e:
                log.debug("1분봉 저장 실패 %s: %s", sym, e)
        log.info("하루치 1분봉 저장 %s: %d/%d종목 (research.db)", d, n, len(syms))
        return n

    def ensure_precursor_daily(self, trade_date, session) -> None:
        """기준일 점수가 없으면 백그라운드로 전 종목 계산 (약 2,000번 일봉 조회 → 몇 분, 그동안 수집은 계속)."""
        exp = self._expected_d0(trade_date, session)
        if not exp or not self.universe or (self._pre_thread and self._pre_thread.is_alive()):
            return
        have = self.store.query("SELECT MAX(d0) FROM precursor_daily")
        if have and have[0][0] and have[0][0] >= exp.isoformat():
            return
        if self._pre_tried and self._pre_tried[0] == exp and self.clock() - self._pre_tried[1] < timedelta(minutes=30):
            return                                   # 방금 시도했는데 비었으면 30분 뒤 다시
        self._pre_tried = (exp, self.clock())
        import threading
        self._pre_thread = threading.Thread(target=self.build_precursor_daily, args=(exp, sorted(self.universe)),
                                            name="precursor", daemon=True)
        self._pre_thread.start()

    def build_precursor_daily(self, d0, symbols: list[str]) -> int:
        store = Store(self.cfg.db_path)                # 스레드 전용 연결
        rows, n = [], 0
        for sym in symbols:
            r = self._precursor_row(sym, d0, store)
            if r is False:
                continue
            if r and r["score"] >= 2:
                rows.append(r)
            n += 1
        store.put_precursor_daily(rows)
        log.info("급등 전조 일봉 점수 %s: %d종목 계산, 2점 이상 %d, %d점 이상 %d", d0, n, len(rows),
                 self.cfg.precursor_min_score, sum(1 for r in rows if r["score"] >= self.cfg.precursor_min_score))
        top = [r["symbol"] for r in sorted(rows, key=lambda r: -r["score"])][:200]
        self.build_stock_tags(d0, top, store)
        if self.notifier and self.cfg.precursor_daily_alert:
            try:
                from ..precursor import daily_list_text
                link = self.link() if self.link else None
                self.notifier.send(daily_list_text(self.cfg.db_path, self.cfg.precursor_daily_alert, link),
                                   buttons=[[{"text": "📊 전체 목록 보기", "url": f"{link}/#home"}]] if link else None)
            except Exception as e:
                log.warning("감시 목록 알림 실패: %s", e)
        return len(rows)

    def _precursor_row(self, sym: str, d0, store, src: str | None = None):
        """일봉 70개 → 전조 행 (기준일 봉이 없으면 None, 조회 실패면 False). 일봉은 daily_bars 에도 저장."""
        from ..precursor import daily
        d0s = d0 if isinstance(d0, str) else d0.isoformat()
        try:
            cs = (self.c.candles(sym, "1d", 70) or {}).get("candles", [])
        except Exception as e:
            log.debug("전조 일봉 실패 %s: %s", sym, e)
            return False
        bars = sorted((cd["timestamp"][:10], float(cd["openPrice"]), float(cd["highPrice"]), float(cd["lowPrice"]),
                       float(cd["closePrice"]), float(cd["volume"])) for cd in cs)
        bars = [b for b in bars if b[0] <= d0s]                    # 기준일 뒤(진행 중인 봉)는 뺀다
        store.put_daily_bars([{"symbol": sym, "d": b[0], "open": b[1], "high": b[2], "low": b[3], "close": b[4],
                               "volume": b[5]} for b in bars[-70:]])
        try:
            r = daily(bars)
        except (ValueError, ZeroDivisionError, statistics.StatisticsError) as e:
            log.debug("전조 일봉 계산 실패 %s: %s", sym, e)     # 데이터 불량(0 종가 등) 한 종목이 배치 전체를 죽이지 않게
            return False
        if not r or r["d0"] != d0s:
            return None
        if src:
            r["metrics"]["src"] = src
        return {"d0": r["d0"], "symbol": sym, "score": r["score"], "signals": json.dumps(r["signals"]),
                "metrics": json.dumps(r["metrics"]), "close": r["close"], "created": iso(self.clock())}

    def _rank_entrants(self, d0: str, have: set, daily_rows: dict) -> list[tuple]:
        """지금 랭킹(급락률 제외)에 든 유니버스 종목 중 목록 밖인 것 → 전조 행. 처음 보는 종목은 일봉으로 점수를 매겨 저장."""
        if self._rank_skip[0] != d0:
            self._rank_skip = (d0, set())
        skip = self._rank_skip[1]
        out, fetched = [], 0
        for (sym,) in self.store.query(
                "SELECT symbol FROM rankings WHERE ts=(SELECT MAX(ts) FROM rankings) AND type != 'TOP_LOSERS' "
                "GROUP BY symbol ORDER BY MIN(rank), symbol"):
            if len(out) >= self.cfg.precursor_rank_max:
                break
            if sym in have or sym in skip or sym not in self.universe:
                continue
            r = daily_rows.get(sym)
            if r is None:
                if fetched >= 10:                    # 한 번에 일봉 10개까지 (나머지는 다음 분에)
                    continue
                fetched += 1
                new = self._precursor_row(sym, d0, self.store, src="rank")
                if not new:
                    skip.add(sym)
                    continue
                self.store.put_precursor_daily([new])
                r = (sym, new["score"], new["signals"], new["metrics"], new["close"])
            out.append(r)
        return out

    def build_stock_tags(self, d0, symbols: list[str], store=None) -> int:
        """ATM·워런트·공모·액면병합·상폐 통지·전환사채 태그 (SEC 공시 판정, 디스크 캐시라 두 번째부터 빠름)."""
        if not self.dilution:
            return 0
        from ..dilution import build_profile
        from ..motive import motives
        from ..screener import edgar as ed
        store = store or Store(self.cfg.db_path)
        try:
            tick = {str(r["ticker"]).upper(): int(r["cik"]) for r in self.dilution.edgar.ticker_map()}
        except Exception as e:
            log.warning("SEC 티커 목록 실패: %s", e)
            return 0
        today = datetime.fromisoformat(str(d0)).date() if not hasattr(d0, "year") else d0
        out = []
        for sym in symbols:
            cik = tick.get(sym)
            if not cik:
                continue
            try:
                evs = self.dilution.events(cik, today)
                fin = self.dilution.financials(cik, today)
                filings = ed.parse_submissions(self.dilution.submissions(cik, today))
            except Exception as e:
                log.debug("공시 태그 실패 %s: %s", sym, e)
                continue
            price = (self.universe.get(sym) or {}).get("last")
            prof = build_profile(evs, today, None, fin, price, None)
            ms = motives(evs, filings, fin, today, price)
            tags, info = [], {"dilution_score": prof.score}
            if prof.atm_active:
                tags.append("ATM 진행")
            if fin and fin.get("warrants") and fin.get("warrant_price"):
                tags.append(f"워런트 ${fin['warrant_price']:g}")
            recent = lambda kinds, days: next((e for e in sorted(evs, key=lambda e: e.filed, reverse=True)
                                               if e.kind in kinds and (today - e.filed).days <= days), None)
            off = recent({"PUBLIC_OFFERING", "REGISTERED_DIRECT", "TAKEDOWN", "PRIVATE_PLACEMENT", "SECURITIES_PURCHASE_AGREEMENT"}, 60)
            if off:
                tags.append(f"최근 공모 {off.filed:%m/%d}")
            elif any(m.key == "offering" for m in ms):
                tags.append("공모 대기")
            current = [e for e in evs if e.form.upper().startswith(("8-K", "6-K", "DEF 14", "PRE 14"))]
            recent_cur = lambda kinds, days: next((e for e in sorted(current, key=lambda e: e.filed, reverse=True)
                                                   if e.kind in kinds and (today - e.filed).days <= days), None)
            rs = recent_cur({"REVERSE_SPLIT"}, 365)       # 공모 설명서의 '과거 액면병합' 문구는 빼고
            if rs:
                tags.append(f"액면병합 {rs.filed:%y/%m/%d}")
                info["reverse_split"] = rs.filed.isoformat()
            if recent_cur({"CONVERTIBLE"}, 180):
                tags.append("전환사채")
            for m in ms:
                if m.key == "delisting":
                    tags.append(m.label)
                elif m.key == "cash" and m.label != "현금 바닥 추정":
                    tags.append(m.label)
            info["motives"] = [m.label for m in ms]
            out.append({"d0": str(d0), "symbol": sym, "tags": json.dumps(tags, ensure_ascii=False),
                        "info": json.dumps(info, ensure_ascii=False)})
        store.put_stock_tags(out)
        log.info("공시 태그 %s: %d종목", d0, len(out))
        return len(out)

    def poll_precursor_live(self, now: datetime, trade_date, session: str) -> None:
        """일봉 점수 높은 종목의 오늘 1분봉 → 거래 깨어남·슬금슬금 상승. 둘 다 켜지면 알림 (종목당 하루 한 번)."""
        from ..precursor import alert_grade, live, tier
        from ..vstrat import in_hours, signal_k
        from ..trading.strategies import SESSION_KO
        d0 = self.store.query("SELECT MAX(d0) FROM precursor_daily")
        if not d0 or not d0[0][0]:
            return
        d0 = d0[0][0]
        shares = dict(self.store.query("SELECT symbol, shares FROM stock_info WHERE shares > 0"))
        tiers = {}
        rows, all_rows = [], {}
        for r in self.store.query("SELECT symbol, score, signals, metrics, close FROM precursor_daily WHERE d0=?", (d0,)):
            all_rows[r[0]] = r
            tiers[r[0]] = tier(json.loads(r[3] or "{}"), shares.get(r[0]), r[4])
            if (tiers[r[0]] or r[1] >= self.cfg.precursor_min_score) and json.loads(r[3] or "{}").get("src") != "rank":
                rows.append(r)
        rows.sort(key=lambda r: (-tiers[r[0]], -r[1], -(json.loads(r[3] or "{}").get("vol_ratio") or 0)))
        rows = rows[:self.cfg.precursor_live_max]
        hot = set(self._fast_hot[1]) if self._fast_hot[0] == d0 else set()
        have = {x[0] for x in rows}
        for s in sorted(hot - have):                     # 빠른 훑기로 잡은 종목은 목록 밖이어도 1분 감시
            if s in all_rows:
                tiers.setdefault(s, tier(json.loads(all_rows[s][3] or "{}"), shares.get(s), all_rows[s][4]))
                rows.append(all_rows[s])
        keep = self._keep_watch(d0, trade_date, all_rows)   # 오르고 빠진 종목: 재가속할 수 있으니 계속 감시
        runners = self._runners(d0)
        self._rb_levels = {s: r["level"] for s, r in runners.items() if r["days"] >= 1}   # 실시간 재돌파 감시선
        if getattr(self, "_rb_d0", None) != d0:          # 새 거래일이면 '오늘 보냄' 표시 초기화
            self._rb_d0, self._rb_sent = d0, set()
        have = {x[0] for x in rows}
        # 며칠 전 급등주는 실시간 체결로 이미 보고 있으면 1분봉 조회는 건너뜀 (신호가 오면 그때 받는다) — 조회 수 절약
        rt_ok = bool(self._rt and self._rt.healthy)
        rt_only = {s for s in keep - have if rt_ok and s not in self._keep_today and s in self._rt_syms}
        for s in sorted(keep - have - rt_only):
            tiers.setdefault(s, tier(json.loads(all_rows[s][3] or "{}"), shares.get(s), all_rows[s][4]))
            rows.append(all_rows[s])
        ranked_in = set()                            # 목록 밖, 랭킹으로 들어온 종목 (전날 일봉에 티가 안 난 급등)
        if self.cfg.precursor_rank_max:
            for r in self._rank_entrants(d0, {x[0] for x in rows}, all_rows):
                tiers.setdefault(r[0], tier(json.loads(r[3] or "{}"), shares.get(r[0]), r[4]))
                ranked_in.add(r[0])
                rows.append(r)
        day_start = self.cal.bounds(trade_date, DAY)
        start = iso(day_start[0]) if day_start else iso(now - timedelta(hours=12))
        prev_alert = dict(self.store.query("SELECT symbol, alerted FROM precursor_live WHERE d0=? AND alerted IS NOT NULL", (d0,)))
        out = []
        for sym, score, sig, met, close in rows:
            try:
                cs = (self.c.candles(sym, "1m", 200) or {}).get("candles", [])
            except Exception as e:
                log.debug("전조 1분봉 실패 %s: %s", sym, e)
                continue
            bars = sorted((iso(datetime.fromisoformat(cd["timestamp"].replace("Z", "+00:00"))), float(cd["openPrice"]),
                           float(cd["highPrice"]), float(cd["lowPrice"]), float(cd["closePrice"]), float(cd["volume"]))
                          for cd in cs)
            today = [b for b in bars if b[0] >= start]
            lv = live(today, close, now, shares.get(sym))
            lv["metrics"]["session"] = session
            # 알림 = 1분 거래(회전율 0.5%+ 또는 $5만+, +5%↑)가 처음 터진 그 분 (최근 3분 안 — 재시작 직후 지난 신호로 몰려 오지 않게).
            # 정규장에 처음 터진 건 거의 안 이어져서(10/23 정규장 알림 65건 중 의미 있게 움직인 것 2건) 알림 없이 기록만.
            k = signal_k(today, close, shares.get(sym))
            burst = k is not None and k >= len(today) - 3
            if burst:
                lv["signals"].append("burst")
                lv["metrics"]["grade"], lv["metrics"]["grade_why"] = alert_grade(
                    today[k][0], tiers[sym], close, json.loads(met or "{}").get("vol_ratio"))
            fire = (burst and sym not in prev_alert and not self._alerted(self.store, d0, sym)
                    and not (self._rt and self._rt.healthy))   # 실시간 체결이 살아 있으면 알림은 실시간 쪽이 맡는다
            out.append({"d0": d0, "symbol": sym, "ts": iso(now), "signals": json.dumps(lv["signals"]),
                        "metrics": json.dumps(lv["metrics"]), "alerted": iso(now) if fire else prev_alert.get(sym)})
            if fire:
                self._alert(self.store, now, session, sym, score, sig, met, lv, tiers[sym],
                            (["랭킹 진입"] if sym in ranked_in else []) + (["빠른 포착"] if sym in hot else []))
            elif (sym in keep or sym in prev_alert) and not (self._rt and self._rt.healthy):
                self._reaccel_check(self.store, now, session, d0, sym, today, close, shares.get(sym), start)
            if sym in self._rb_levels and len(today) > 2 and not (self._rt and self._rt.healthy):
                self._approach_bars(now, session, sym, today, close)
            if self.cfg.vsignal and session != AFTER:      # 가상 매매는 알림과 따로: 1분 거래 터짐 → 아래 지정가
                self._vtrade(now, d0, sym, score, close, today, shares.get(sym), session, start)
        # 구독 우선: 오늘 알림·+30% → 감시 상위 → 며칠 전 급등주 (최근 것부터)
        old = sorted((s for s in keep if s in runners and s not in self._keep_today), key=lambda s: runners[s]["days"])
        first = [x[0] for x in rows if x[0] in keep and x[0] not in old]
        self._rt_set(first + [x[0] for x in rows if x[0] not in keep] + old)
        for r in out:                                    # 루프 도는 사이 실시간 쪽이 보낸 알림 표시를 덮어쓰지 않게
            if not r["alerted"]:
                r["alerted"] = self._alerted(self.store, d0, r["symbol"])
        self.store.put_precursor_live(out)

    @staticmethod
    def _alerted(store, d0: str, sym: str):
        r = store.query("SELECT alerted FROM precursor_live WHERE d0=? AND symbol=? AND alerted IS NOT NULL", (d0, sym))
        return r[0][0] if r else None

    def _keep_watch(self, d0: str, trade_date, all_rows: dict) -> set:
        """오르고 빠졌다고 감시에서 빼지 않는다: 오늘 알림 나간 종목 · 오늘 전일 대비 +30% 찍은 종목 ·
        최근 3거래일 안에 고가 +50% 찍은 종목 (2일차·3일차 재가속). 일봉 점수(all_rows)가 있는 것만."""
        out = {s for (s,) in self.store.query("SELECT symbol FROM precursor_live WHERE d0=? AND alerted IS NOT NULL", (d0,))}
        if self._fast_hot[0] == d0:
            out |= set(self._fast_hot[1])
        td = trade_date.isoformat() if trade_date else None
        if td:
            for s, mx in self.store.query("SELECT symbol, MAX(last) FROM prices WHERE trade_date=? GROUP BY symbol", (td,)):
                r = all_rows.get(s)
                if r and r[4] and mx and mx >= r[4] * 1.3:
                    out.add(s)
        self._keep_today = {s for s in out if s in all_rows}
        out |= set(self._runners(d0))                    # 며칠 전 급등주 (63% 가 30거래일 안에 또 +30% 급등, 절반 넘게 4일 이후)
        return {s for s in out if s in all_rows}

    def _runners(self, d0: str) -> dict:
        """최근 keep_days 거래일 안에 고가 +keep_min_run% 찍은 종목 → {급등일, 급등폭, 며칠 전, 그 뒤 최고(=재돌파선), 그 뒤 최저}. 기준일마다 한 번."""
        c = getattr(self, "_runner_cache", None)
        if c and c[0] == d0:
            return c[1]
        ds = [d for (d,) in self.store.query("SELECT DISTINCT d FROM daily_bars WHERE d<=? ORDER BY d DESC LIMIT ?", (d0, self.cfg.keep_days))]
        out: dict = {}
        if ds:
            idx = {d: i for i, d in enumerate(ds)}
            k = 1 + self.cfg.keep_min_run / 100
            for s, d, chg in self.store.query(
                    """SELECT b.symbol, b.d, (b.high / p.close - 1) * 100 FROM daily_bars b JOIN daily_bars p ON p.symbol=b.symbol
                         AND p.d=(SELECT MAX(d) FROM daily_bars WHERE symbol=b.symbol AND d<b.d)
                       WHERE b.d>=? AND b.d<=? AND p.close>0 AND b.high>=p.close*? ORDER BY b.d""", (ds[-1], d0, k)):
                out[s] = {"run_d": d, "run_chg": chg, "days": idx[d] + 1}      # 가장 최근 급등일 (d0 = 장중 기준 전 거래일 → 1일 전)
            for s, r in out.items():
                hl = self.store.query("SELECT MAX(high), MIN(low) FROM daily_bars WHERE symbol=? AND d>=? AND d<=?", (s, r["run_d"], d0))
                after = self.store.query("SELECT MIN(low) FROM daily_bars WHERE symbol=? AND d>? AND d<=?", (s, r["run_d"], d0))
                r["level"], r["low"] = hl[0][0], (after[0][0] if after and after[0][0] else hl[0][1])
        self._runner_cache = (d0, out)
        return out

    def _approach(self, store, now, session, sym: str, price: float, stage: int, dollar: float | None, pc: float | None) -> bool:
        """며칠 전 급등주가 그 뒤 최고가에 다가옴 → 1단계 '👀 접근'(15% 아래) · 2단계 '🎯 임박'(5% 아래). 단계마다 하루 한 번, 넘기 전에만."""
        from ..precursor import APPROACH, alert_buttons, approach_text
        from ..trading.strategies import SESSION_KO
        r = (getattr(self, "_runner_cache", None) or (None, {}))[1].get(sym)
        if not r or not r["days"] or price > r["level"] * 1.02:
            return False                                 # 이미 넘은 뒤면 늦음
        title = APPROACH[stage - 1][3]
        trade_date, _ = self.cal.classify(now)
        b = self.cal.bounds(trade_date, DAY) if trade_date else None
        start = iso(b[0]) if b else iso(now - timedelta(hours=12))
        if store.query("SELECT 1 FROM events WHERE symbol=? AND kind='approach' AND ts>=? AND text LIKE ?", (sym, start, title[:2] + "%")):
            return False
        if pc is None:
            row = store.query("SELECT close FROM daily_bars WHERE symbol=? ORDER BY d DESC LIMIT 1", (sym,))
            pc = row[0][0] if row else None
        self._ev_last.pop((sym, "approach"), None)       # 1단계 직후 2단계도 남게 (10분 제한 무시)
        self._event(now, sym, "approach", f"{title[:2]} {sym} {title[2:].strip()} (고점 ${r['level']:.4g}까지 {(r['level'] / price - 1) * 100:+.1f}%"
                    f" · {r['days']}일 전 +{r['run_chg']:.0f}%)", (price / pc - 1) * 100 if pc else None)
        log.info("고점 %s %s $%.4g → $%.4g (%d일 전 급등)", title, sym, price, r["level"], r["days"])
        self._fast_hot[1].add(sym)
        if self.notifier and self._on("approach"):
            link = None
            try:
                link = self.link() if self.link else None
            except Exception:
                pass
            oh = self._overhead(store, sym, price)
            self.notifier.send(approach_text(sym, (self.universe.get(sym) or {}).get("name"), r, stage, price, pc,
                                             SESSION_KO.get(session, session), dollar, [oh] if oh else None),
                               buttons=alert_buttons(sym, link))
        return True

    def _approach_bars(self, now, session, sym: str, today: list, pc) -> None:
        """웹소켓이 없을 때 1분봉으로: 최근 2분에 선을 처음 넘어왔으면 (그 전엔 종가가 선 아래)."""
        from ..precursor import APPROACH
        lvl = self._rb_levels[sym]
        before, last2 = today[:-2], today[-2:]
        hi2 = max(b[2] for b in last2)
        for stage, k, need_x, _, _ in reversed(APPROACH):
            line = lvl * k
            if (sym, stage) in self._rb_sent or hi2 < line or (before and max(b[4] for b in before) >= line):
                continue
            base = sum(b[5] for b in before[-30:]) / max(1, len(before[-30:]))
            if need_x and last2[-1][5] < need_x * max(base, 1.0):
                continue
            if self._approach(self.store, now, session, sym, last2[-1][4], stage, sum(b[4] * b[5] for b in last2), pc):
                self._rb_sent.update({(sym, s_) for s_ in range(1, stage + 1)})
            break

    def _reaccel_check(self, store, now, session, d0, sym, today, pc, shares, start) -> bool:
        """급등 뒤 15% 눌렸다 10% 다시 오르고 최근 2분 거래가 붙으면 '🔁 재가속' (한 파동에 한 번, 20분 간격, 하루 5번까지).
        big300 비교(+20%대 그친 날 vs +300% 간 날): 그날 재가속이 거듭될수록 +300% 비율이 뚜렷이 올라가
        (1번째 18%·2번째 30%·3번째 44%·4번째 64%·5번째 77%, 기준 3.3%) 뒤쪽일수록 더 의미 있는 신호라 3번에서 끊지 않는다."""
        from ..precursor import reaccel, reaccel_text
        from ..trading.strategies import SESSION_KO
        if len(today) < 5:
            return False
        evs = [r[0] for r in store.query("SELECT ts FROM events WHERE symbol=? AND kind='reaccel' AND ts>=? ORDER BY ts", (sym, start))]
        if len(evs) >= 5 or (evs and now - datetime.fromisoformat(evs[-1]) < timedelta(minutes=20)):
            return False
        alerted = self._alerted(store, d0, sym)
        since = max([x for x in (start, alerted, evs[-1] if evs else None) if x])  # 직전 알림 뒤의 고점부터 다시 센다
        r = reaccel(today, since)
        if not r:
            return False
        last2 = today[-2:]
        d2, v2 = sum(b[4] * b[5] for b in last2), sum(b[5] for b in last2)
        if not (d2 >= 5e4 or (shares and v2 / shares * 100 >= 0.5)):
            return False                                 # 반등에 거래가 안 붙으면 아직
        n = len(evs) + 1
        self._event(now, sym, "reaccel", f"🔁 {sym} 재가속 {n}번째 (고점 ${r['high']:.4g} → {r['drop']:+.0f}% → 반등 {r['bounce']:+.0f}%"
                    f" · 2분 ${d2 / 1e3:.0f}K)", (r["price"] / pc - 1) * 100 if pc else None)
        log.info("재가속 %s %d번째 (눌림 %.0f%% → 반등 %.0f%%)", sym, n, r["drop"], r["bounce"])
        self._fast_hot[1].add(sym)
        if self.notifier and self._on("reaccel"):
            from ..precursor import alert_buttons
            link = None
            try:
                link = self.link() if self.link else None
            except Exception:
                pass
            oh = self._overhead(store, sym, r["price"])
            self.notifier.send(reaccel_text(sym, (self.universe.get(sym) or {}).get("name"), r, pc, SESSION_KO.get(session, session), n,
                                            d2, [oh] if oh else None), buttons=alert_buttons(sym, link))
        return True

    def _overhead(self, store, sym: str, price) -> str | None:
        """'매물대 두꺼움 35% · 저항 $2.09 (+3%)' — +10% 돌파 뒤 +30% 연장률: 두꺼움 7% vs 그 외 11% (일봉 8.5천 건)."""
        from .. import overhead
        try:
            return overhead.label(overhead.for_symbol(self._mq(store), sym, price))
        except Exception as e:
            log.debug("매물대 실패 %s: %s", sym, e)
            return None

    def _perf_note(self, kind: str) -> str:
        """알림 근거 옆 숫자: 끝난 거래일 실제 결과가 20건 넘게 쌓이면 그걸, 아니면 연구 결과."""
        import time
        from .. import alertperf
        c = getattr(self, "_perf_cache", None)
        if not c or time.time() - c[0] > 3600:
            try:
                c = self._perf_cache = (time.time(), alertperf.stats(self._mq(), self._rq()))
            except Exception as e:
                log.debug("알림 성과 실패: %s", e)
                c = self._perf_cache = (time.time(), {})
        s = c[1].get(kind) or {}
        if s.get("n", 0) >= 20:
            return f" (과거 {s['n']}건 적중률 {s['win'] / s['n'] * 100:.0f}% · 당일 +30% {s['hit30'] / s['n'] * 100:.0f}%)"
        return " (연구: +30% 선도달 약 50%)"

    def _alert(self, store, now, session, sym, score, sig, met, lv, tier_, extra_tags) -> None:
        """거래 터짐 한 건: 급등 전조 조건(presurge)이면 '🚨 급등 전조' 전체, 아니면 '📶 거래 터짐' 짧은 신호 (세션 상관없이)."""
        from ..precursor import TIER_LABEL, alert_buttons, alert_text, ask_depth, presurge, runner_tag, signal_text
        r = (getattr(self, "_runner_cache", None) or (None, {}))[1].get(sym)
        if r and r["days"]:                              # 며칠 전 급등주면 알림에 표시
            extra_tags = [runner_tag(r)] + list(extra_tags)
        m = lv["metrics"]
        dm = json.loads(met or "{}")
        m["presurge"], why = presurge(iso(now), tier_, m.get("price") and m["price"] / (1 + (m.get("chg") or 0) / 100),
                                      dm.get("vol_ratio"), m.get("chg"))
        if not m["presurge"]:
            if self.notifier and self._on("burst"):
                link = None
                try:
                    link = self.link() if self.link else None
                except Exception:
                    pass
                from ..trading.strategies import SESSION_KO
                oh = self._overhead(store, sym, m.get("price"))
                self.notifier.send(signal_text(sym, (self.universe.get(sym) or {}).get("name"), lv, SESSION_KO.get(session, session),
                                               extra_tags + ([TIER_LABEL[tier_]] if tier_ else []) + ([oh] if oh else [])),
                                   buttons=alert_buttons(sym, link))
            log.info("거래 터짐 신호 %s (%s)", sym, session)
            self._event(now, sym, "burst", f"📶 {sym} 거래 터짐 ({m.get('chg', 0):+.1f}% · 3분 ${m.get('dollar_3m', 0) / 1e3:.0f}K)",
                        m.get("chg"))
            return
        m["grade_why"] = why
        from ..trading.strategies import SESSION_KO
        try:
            lv["metrics"]["ask10"] = ask_depth(self.c.orderbook(sym), lv["metrics"].get("price"))
        except Exception as e:
            log.debug("호가 실패 %s: %s", sym, e)
        m = lv["metrics"]
        if self.notifier and self._on("presurge"):
            day = {"score": score, "signals": json.loads(sig or "[]"), "metrics": json.loads(met or "{}")}
            link = None
            try:
                link = self.link() if self.link else None
            except Exception:
                pass
            sh = store.query("SELECT shares FROM stock_info WHERE symbol=?", (sym,))
            tg = store.query("SELECT tags FROM stock_tags WHERE symbol=? ORDER BY d0 DESC LIMIT 1", (sym,))
            pats = [lab for k, lab in (("compress", "응축"), ("accum", "매집 의심"), ("base", "바닥 다지기")) if k in day["signals"]]
            self.notifier.send(alert_text(sym, (self.universe.get(sym) or {}).get("name"), day, lv,
                                          SESSION_KO.get(session, session), link, now,
                                          shares=sh[0][0] if sh and sh[0][0] else None,
                                          tags=["근거: " + " · ".join(m["grade_why"]) + self._perf_note("precursor")]
                                          + ([oh] if (oh := self._overhead(store, sym, m.get("price"))) else [])
                                          + ([TIER_LABEL[tier_]] if tier_ else []) + extra_tags
                                          + (["호가 얇음"] if (m.get("ask10") or 9e18) < (m.get("dollar_3m") or 0) else [])
                                          + (json.loads(tg[0][0]) if tg else []) + pats),
                               buttons=alert_buttons(sym, link))
        log.info("급등 전조 알림 %s (%s)", sym, " · ".join(m["grade_why"]))
        self._event(now, sym, "precursor", f"🚨 {sym} 급등 전조 ({' · '.join(m['grade_why'])} · "
                    f"{m.get('chg', 0):+.1f}% · 3분 ${m.get('dollar_3m', 0) / 1e3:.0f}K)", m.get("chg"))

    # ---------- 실시간: 웹소켓 체결 → 최근 60초 거래대금·회전율·평소 대비 거래량 → 바로 알림 (등락률은 안 봄) ----------
    def _rt_set(self, symbols) -> None:
        """구독할 종목 (1분 감시 목록 + 랭킹 훑기로 새로 잡은 종목). 새 종목은 1분봉으로 평소 거래량을 채운다."""
        hot = self._fast_hot[1] if self._fast_hot[0] else set()
        want = list(dict.fromkeys(list(hot) + list(symbols)))
        if self._rt:
            want = want[:self._rt.max_symbols]
            self._rt.pin(want)
        for s in want:
            if s not in self._rt_seeded:
                self._rt_seeded.add(s)
                self._rt_q.put(("seed", s))
        self._rt_syms = set(want)

    def _on_trade(self, sym: str, price: float, vol: float, t: float | None = None) -> None:
        """웹소켓 스레드에서 체결마다. 무거운 일(1분봉 조회·알림·정리)은 큐로 넘긴다."""
        from collections import deque
        t = t if t is not None else time.time()
        for o in self._vorders.get(sym, ()):            # 걸려 있는 가상 지정가: 체결가가 지정가 이하로 찍히면 바로 체결
            if not o.get("_filling") and o["entry"] and price <= o["entry"]:
                o["_filling"] = True
                self._rt_q.put(("vfill", o["id"], price))
        for v in self._vopen.get(sym, ()):              # 보유 중 가상 포지션: 체결가로 바로 판단
            hi = max(self._vhi.get(v["id"], 0.0), v["high"] or 0.0, price)
            self._vhi[v["id"]] = hi
            if not v.get("_closing") and self._vexit(v, price, hi, self.clock()) not in (None, "애프터장 마감 정리"):
                v["_closing"] = True
                self._rt_q.put(("vclose", v["id"], price))
        w = self._rt_win.setdefault(sym, deque())
        w.append((t, price, vol))
        while w and t - w[0][0] > 60:
            w.popleft()
        mins = self._rt_min.setdefault(sym, {})
        m = int(t // 60)
        mins[m] = mins.get(m, 0.0) + vol
        if len(mins) > 45:
            for k in sorted(mins)[:-40]:
                mins.pop(k, None)
        lvl = self._rb_levels.get(sym)
        if lvl:                                          # 며칠 전 급등주: 그 뒤 최고가의 15%·5% 아래 선을 넘어오는 순간
            from ..precursor import APPROACH
            prev, self._rt_last[sym] = self._rt_last.get(sym), price
            if prev is not None and price <= lvl * 1.02:
                d60, v60 = sum(p * v for _, p, v in w), sum(v for _, _, v in w)
                base = sum(v for k, v in mins.items() if m - 31 <= k <= m - 2) / 30
                for stage, k, need_x, _, _ in reversed(APPROACH):
                    line = lvl * k
                    if not (prev < line <= price) or (sym, stage) in self._rb_sent:
                        continue
                    if d60 < (1e4 if stage == 2 else 2e4) or (need_x and v60 < need_x * max(base, 1.0) * 1.0):
                        continue                         # 거래가 안 붙은 선 넘기는 아직
                    self._rb_sent.update({(sym, s_) for s_ in range(1, stage + 1)})   # 2단계면 1단계도 건너뜀
                    self._rt_q.put(("approach", sym, price, stage, d60))
                    break
        if t - self._rt_fired.get(sym, 0) < 600 or not self._settings.get().get("realtime", True):
            return
        d60 = sum(p * v for _, p, v in w)
        v60 = sum(v for _, _, v in w)
        sh = self._rt_shares.get(sym)
        if not (d60 >= self.cfg.rt_min_dollar or (sh and d60 >= self.cfg.rt_min_turn_dollar
                                                    and self.cfg.rt_min_turnover <= v60 / sh * 100 <= 1000)):
            return
        base = sum(v for k, v in mins.items() if m - 31 <= k <= m - 2) / 30   # 직전 30분 분당 평균 (지금 분·직전 분 제외)
        if sym not in self._rt_seeded or v60 < self.cfg.rt_surge * max(base, 1.0):
            return
        self._rt_fired[sym] = t
        self._rt_q.put(("alert", sym, d60, v60, price))

    def _rt_worker(self) -> None:
        import queue
        store = Store(self.cfg.db_path)                  # 스레드 전용 연결
        self._rt_shares = dict(store.query("SELECT symbol, shares FROM stock_info WHERE shares > 0"))
        last_refresh = 0.0
        while True:
            if time.time() - last_refresh >= 5:          # 보유 중 가상 포지션 목록 (실시간 정리용) + 그 종목 구독
                self._vopen_refresh(store)
                last_refresh = time.time()
            try:
                job = self._rt_q.get(timeout=5)
            except queue.Empty:
                continue
            try:
                if job[0] == "seed":
                    self._rt_seed(job[1])
                elif job[0] == "vclose":
                    self._rt_vclose(store, job[1], job[2])
                elif job[0] == "vfill":
                    self._rt_vfill(store, job[1], job[2])
                elif job[0] == "approach":
                    now = self.clock()
                    _, session = self.cal.classify(now)
                    self._approach(store, now, session, job[1], job[2], job[3], job[4], None)
                else:
                    self._rt_alert(store, *job[1:])
            except Exception as e:
                log.warning("실시간 처리 실패 %s: %s", job[:2], str(e)[:160])

    def _vopen_refresh(self, store) -> None:
        by: dict = {}
        orders: dict = {}
        for r in store.query("SELECT * FROM vsignals WHERE status IN ('OPEN','ORDER')"):
            v = self._vrow(r)
            (by if v["status"] == "OPEN" else orders).setdefault(v["symbol"], []).append(v)
        self._vopen, self._vorders = by, orders
        new = (set(by) | set(orders)) - self._rt_syms
        if new:
            self._rt_set(list(new) + list(self._rt_syms))

    def _rt_vfill(self, store, vid: int, price: float) -> None:
        """실시간 체결가가 가상 지정가에 닿음 → 지정가로 체결 (실제 주문이면 줄 선 순서에 따라 안 될 수도 있다)."""
        r = store.query("SELECT * FROM vsignals WHERE id=? AND status='ORDER'", (vid,))
        if not r:
            return
        v = self._vrow(r[0])
        now = self.clock()
        _, session = self.cal.classify(now)
        lim = v["entry"]
        with store.db:
            cur = store.db.execute("UPDATE vsignals SET status='OPEN', opened=?, entry=?, high=?, low=?, last=? WHERE id=? AND status='ORDER'",
                                   (iso(now), lim, lim, min(lim, price), price, vid))
        if cur.rowcount:
            self._vnotify("buy", vid, session, store)
            self._vopen_refresh(store)

    def _rt_vclose(self, store, vid: int, price: float) -> None:
        r = store.query("SELECT * FROM vsignals WHERE id=? AND status='OPEN'", (vid,))
        if not r:
            return
        now = self.clock()
        _, session = self.cal.classify(now)
        self._vupdate(store, self._vrow(r[0]), price, now, session, hi=self._vhi.get(vid))

    def _rt_seed(self, sym: str) -> None:
        """구독 시작할 때 최근 40분 1분봉으로 평소 거래량을 채워 둔다 (재시작 직후 몰림 방지)."""
        try:
            cs = (self.c.candles(sym, "1m", 40) or {}).get("candles", [])
        except Exception as e:
            log.debug("실시간 평소 거래량 실패 %s: %s", sym, e)
            return
        mins = self._rt_min.setdefault(sym, {})
        for cd in cs:
            end = datetime.fromisoformat(cd["timestamp"].replace("Z", "+00:00")).timestamp()
            k = int((end - 30) // 60)
            mins[k] = max(mins.get(k, 0.0), float(cd["volume"]))

    def _rt_alert(self, store, sym: str, d60: float, v60: float, price: float) -> None:
        from ..trading.messages import money_s
        now = self.clock()
        trade_date, session = self.cal.classify(now)
        if session not in (DAY, PRE, REGULAR, AFTER):
            return
        d0 = store.query("SELECT MAX(d0) FROM precursor_daily")
        if not d0 or not d0[0][0]:
            return
        d0 = d0[0][0]
        pc = store.query("SELECT close FROM daily_bars WHERE symbol=? AND d=?", (sym, d0))
        pc = pc[0][0] if pc and pc[0][0] else price
        sh = self._rt_shares.get(sym)
        tag = f"⚡ 실시간 1분 {money_s(d60)}" + (f" · 회전율 {v60 / sh * 100:.1f}%" if sh else "")
        log.info("실시간 거래 터짐 %s 60초 $%.0f 회전율 %s", sym, d60, f"{v60 / sh * 100:.2f}%" if sh else "-")
        self._fast_confirm(now, trade_date, session, store, d0, sym, pc, check=False, tags=[tag])

    def _watch_now(self, symbols, store=None) -> None:
        """종목을 지금 바로 실시간 구독·1분 감시·가상 매매 대상에 넣는다 (일봉 점수가 없으면 계산)."""
        store = store or self.store
        d0 = store.query("SELECT MAX(d0) FROM precursor_daily")
        if not d0 or not d0[0][0]:
            return
        d0 = d0[0][0]
        if self._fast_hot[0] != d0:
            self._fast_hot = (d0, set())
        self._fast_hot[1].update(symbols)
        for s in symbols:
            if not store.query("SELECT 1 FROM precursor_daily WHERE d0=? AND symbol=?", (d0, s)):
                row = self._precursor_row(s, d0, store, src="fast")
                if row:
                    store.put_precursor_daily([row])
        self._rt_set(list(self._rt_syms))

    def pre_open_watch(self, now: datetime, trade_date) -> list[str]:
        """동부 03:55 (데이마켓 끝나기 직전) 한 번: 데이마켓에서 거래가 터졌거나 +15% 넘게 오른 종목 → '프리 개장 주목' 알림.
        10/23 LABT 는 데이마켓 23:03 에 +20% 였다가 04:01 프리 개장 1분 만에 +122% — 개장 순간에 끝나는 경우가 있다."""
        from ..trading.messages import pct, px
        t = now.astimezone(ET)
        if not (t.hour == 3 and t.minute >= 55) or getattr(self, "_pre_open_sent", None) == trade_date:
            return []
        self._pre_open_sent = trade_date
        d0 = self.store.query("SELECT MAX(d0) FROM precursor_daily")
        if not d0 or not d0[0][0]:
            return []
        d0 = d0[0][0]
        day_start = self.cal.bounds(trade_date, DAY)
        start = iso(day_start[0]) if day_start else iso(now - timedelta(hours=8))
        burst = {r[0] for r in self.store.query("SELECT symbol FROM precursor_live WHERE d0=? AND alerted>=?", (d0, start))}
        pcs = dict(self.store.query("SELECT symbol, close FROM daily_bars WHERE d=?", (d0,)))
        rows = []
        for s, v in self.universe.items():
            pc, last = pcs.get(s), v.get("last")
            if pc and last and (s in burst or last >= pc * 1.15):
                rows.append((s, v.get("name") or "", last, (last / pc - 1) * 100, s in burst))
        rows.sort(key=lambda r: -r[3])
        rows = rows[:15]
        if not rows:
            return []
        self._watch_now([r[0] for r in rows])
        if self.notifier and self._on("pre_open"):
            self.notifier.send("\n".join(["🌅 프리마켓 개장(04:00 ET) 주목 — 데이마켓에서 움직인 종목", "──────────────"]
                                         + [f"{s} {n} · {px(p)} ({pct(c)})" + (" · 거래 터짐" if b else "") for s, n, p, c, b in rows]
                                         + ["", "개장 순간 한 번에 터지는 경우가 있어 실시간 구독에 고정했습니다"]))
        log.info("프리 개장 주목 %d종목: %s", len(rows), ", ".join(r[0] for r in rows))
        return [r[0] for r in rows]

    # ---------- 랭킹 훑기: 거래대금·거래량 실시간 랭킹에 새로 든 종목을 바로 실시간 구독 ----------
    def _fast_loop(self) -> None:
        store = Store(self.cfg.db_path)                  # 스레드 전용 연결
        while True:
            t0 = time.time()
            try:
                now = self.clock()
                trade_date, session = self.cal.classify(now)
                if session in (DAY, PRE, REGULAR) and self.universe:
                    self.fast_scan(now, trade_date, session, store)
            except Exception as e:
                log.warning("랭킹 훑기 실패: %s", str(e)[:160])
            time.sleep(max(1.0, self.cfg.fast_scan_seconds - (time.time() - t0)))

    def fast_scan(self, now: datetime, trade_date, session: str, store=None) -> list[str]:
        """거래대금·거래량 실시간 랭킹(시장·토스) 상위에 있는데 아직 구독 안 한 유니버스 종목 → 실시간 구독에 넣는다. → 새로 넣은 종목."""
        store = store or self.store
        d0 = store.query("SELECT MAX(d0) FROM precursor_daily")
        if not d0 or not d0[0][0]:
            return []
        d0 = d0[0][0]
        if self._fast_hot[0] != d0:
            self._fast_hot = (d0, set())
        seen = []
        for type_ in ("MARKET_TRADING_AMOUNT", "TOSS_SECURITIES_TRADING_AMOUNT", "MARKET_TRADING_VOLUME", "TOSS_SECURITIES_TRADING_VOLUME"):
            try:
                res = self.c.rankings(type_, "realtime") or {}
            except Exception as e:
                log.debug("랭킹 실패 %s: %s", type_, e)
                continue
            for r in res.get("rankings", [])[:50]:
                s = r.get("symbol")
                if s in self.universe and s not in seen:
                    seen.append(s)
        new = [s for s in seen if s not in self._rt_syms and s not in self._fast_hot[1]]
        if new:
            self._fast_hot[1].update(new)
            self._rt_set(list(self._rt_syms))
            for s in new:                                # 1분 감시·가상 매매용 일봉 점수 (없으면)
                if not store.query("SELECT 1 FROM precursor_daily WHERE d0=? AND symbol=?", (d0, s)):
                    row = self._precursor_row(s, d0, store, src="fast")
                    if row:
                        store.put_precursor_daily([row])
            log.info("랭킹 새로 든 종목 → 실시간 구독: %s", ", ".join(new[:10]))
        return new

    def _fast_confirm(self, now, trade_date, session, store, d0, sym, pc, check: bool = True, tags=None) -> bool:
        from ..precursor import alert_grade, live, tier
        from ..vstrat import in_hours
        try:
            cs = (self.c.candles(sym, "1m", 200) or {}).get("candles", [])
        except Exception as e:
            log.debug("빠른 확인 1분봉 실패 %s: %s", sym, e)
            return False
        bars = sorted((iso(datetime.fromisoformat(cd["timestamp"].replace("Z", "+00:00"))), float(cd["openPrice"]),
                       float(cd["highPrice"]), float(cd["lowPrice"]), float(cd["closePrice"]), float(cd["volume"])) for cd in cs)
        day_start = self.cal.bounds(trade_date, DAY)
        start = iso(day_start[0]) if day_start else iso(now - timedelta(hours=12))
        today = [b for b in bars if b[0] >= start]
        if len(today) < 2:
            return False
        sh = store.query("SELECT shares FROM stock_info WHERE symbol=?", (sym,))
        shares = sh[0][0] if sh and sh[0][0] else None
        last2 = today[-2:]
        d2, v2 = sum(b[4] * b[5] for b in last2), sum(b[5] for b in last2)
        if check and not (d2 >= 5e4 or (shares and 0.5 <= v2 / shares * 100 <= 1000)):
            return False                                 # 거래가 얇음
        self._fast_hot[1].add(sym)                       # 다음 1분 감시·가상 매매에 넣는다
        if self._alerted(store, d0, sym):                # 오늘 이미 알림 → 눌렸다 다시 붙는 거면 재가속 알림
            self._reaccel_check(store, now, session, d0, sym, today, pc, shares, start)
            return True
        row = store.query("SELECT symbol, score, signals, metrics, close FROM precursor_daily WHERE d0=? AND symbol=?", (d0, sym))
        if not row:
            new = self._precursor_row(sym, d0, store, src="fast")
            if new:
                store.put_precursor_daily([new])
                row = [(sym, new["score"], new["signals"], new["metrics"], new["close"])]
        _, score, sig, met, _ = row[0] if row else (sym, 0, "[]", "{}", pc)
        dm = json.loads(met or "{}")
        lv = live(today, pc, now, shares)
        lv["signals"].append("burst")
        lv["metrics"]["session"] = session
        t_ = tier(dm, shares, pc)
        lv["metrics"]["grade"], lv["metrics"]["grade_why"] = alert_grade(iso(now), t_, pc, dm.get("vol_ratio"))
        store.put_precursor_live([{"d0": d0, "symbol": sym, "ts": iso(now), "signals": json.dumps(lv["signals"]),
                                   "metrics": json.dumps(lv["metrics"]), "alerted": iso(now)}])
        self._alert(store, now, session, sym, score, sig, met, lv, t_, tags or ["빠른 포착"])
        if self.cfg.vsignal and session != AFTER:          # 가상 매매 전략도 지금 바로 (지정가 주문·바로 매수)
            try:
                self._vtrade(now, d0, sym, score, pc, today, shares, session, start, store)
                self._vopen_refresh(store)
            except Exception as e:
                log.warning("실시간 가상 매매 실패 %s: %s", sym, str(e)[:120])
        return True

    # ---------- 가상 매수·매도 (알림만, 실제 주문 없음) ----------
    def _day_bars(self, sym: str, recent: list[tuple], start: str) -> list[tuple]:
        """그날 데이마켓 첫 봉부터의 1분봉 (VWAP 용). 최근 200분으로 모자라면 앞쪽을 한 번 받아 두고, 이후엔 이어 붙인다."""
        have = self._day_cache.get(sym)
        if have and have[0] == start:
            merged = {b[0]: b for b in have[1]}
        elif recent and recent[0][0] <= start:
            merged = {}
        else:
            merged, before = {}, (recent[0][0] if recent else None)
            for _ in range(8):
                try:
                    res = self.c.candles(sym, "1m", 200, before) or {}
                except Exception as e:
                    log.debug("1분봉 앞쪽 실패 %s: %s", sym, e)
                    break
                page = res.get("candles", [])
                for cd in page:
                    ts = iso(datetime.fromisoformat(cd["timestamp"].replace("Z", "+00:00")))
                    merged[ts] = (ts, float(cd["openPrice"]), float(cd["highPrice"]), float(cd["lowPrice"]),
                                  float(cd["closePrice"]), float(cd["volume"]))
                before = res.get("nextBefore")
                if not page or not before or min(merged) <= start:
                    break
        merged.update({b[0]: b for b in recent})
        bars = sorted(b for t, b in merged.items() if t >= start)
        self._day_cache[sym] = (start, bars)
        return bars

    def _vtrade(self, now, d0, sym, score, prev_close, today, shares, session, start=None, store=None) -> None:
        """전략마다 종목당 하루 한 번: 신호 → 가상 지정가(ORDER) → 닿으면 체결(OPEN), 바로 사는 전략은 곧장 OPEN.
        방금(최근 3분) 일어난 것만 새로 기록한다 (수집기가 늦게 켜졌을 때 지난 신호로 사지 않게)."""
        from ..vstrat import STRATEGIES, plan, signal_k
        store = store or self.store
        on = self._settings.get()["strategies"]
        strats = [(k, STRATEGIES[k]) for k in self.cfg.vsignal_strategies if k in STRATEGIES and on.get(k, True)]
        state = {r[0]: (r[1], r[2]) for r in store.query(
            "SELECT strat, id, status FROM vsignals WHERE symbol=? AND d0=? AND strat IS NOT NULL ORDER BY id", (sym, d0))}
        live = [(k, st) for k, st in strats if k not in state or state[k][1] == "ORDER"]
        if not live or (not state and signal_k(today, prev_close, shares) is None):
            return
        if start and self.c:
            today = self._day_bars(sym, today, start)    # VWAP·신호는 그날 첫 봉부터
        fresh = lambda k: k is not None and k >= len(today) - 3
        for key, st in live:
            p = plan(today, prev_close, shares, st)
            if not p or p.get("waiting"):
                continue
            if key not in state:
                if not fresh(p["trig_k"]) and not fresh(p["fill_k"]):
                    continue
                b = today[p["trig_k"]]
                turn = f" · 1분 회전율 {b[5] / shares * 100:.1f}%" if shares else ""
                reason = (f"1분 거래대금 ${b[4] * b[5] / 1e3:.0f}K{turn} · {datetime.fromisoformat(b[0]).astimezone(ET):%H:%M} ET"
                          f" · 신호가 {b[4]:.4g} (전날 대비 {(b[4] / prev_close - 1) * 100:+.0f}%) · 전조 {score}점")
                filled = p["fill_k"] is not None
                f = today[p["fill_k"]] if filled else None
                with store.db:
                    c = store.db.execute(
                        "INSERT INTO vsignals (symbol, d0, opened, session, entry, score, reason, status, high, low, last, tp, sl, strat) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (sym, d0, f[0] if filled else b[0], session, p["limit"], score, reason, "OPEN" if filled else "ORDER",
                         p["limit"], min(f[3], p["limit"]) if filled and st["entry"] in ("vwap", "signal", "high") else p["limit"], today[-1][4],
                         st["tp"], st["sl"], key))
                self._vnotify("buy" if filled else "order", c.lastrowid, session, store)
                continue
            vid = state[key][0]
            if p["fill_k"] is not None:
                f = today[p["fill_k"]]
                with store.db:
                    store.db.execute("UPDATE vsignals SET status='OPEN', opened=?, entry=?, high=?, low=?, last=? WHERE id=?",
                                          (f[0], p["fill_px"], p["fill_px"], min(f[3], p["fill_px"]), f[4], vid))
                self._vnotify("buy", vid, session, store)
            else:
                with store.db:                      # 지정가는 기준(VWAP·고점)을 따라 움직인다
                    store.db.execute("UPDATE vsignals SET entry=?, high=?, low=?, last=? WHERE id=?",
                                          (p["limit"], p["limit"], p["limit"], today[-1][4], vid))

    def _vnotify(self, kind: str, vid: int, session: str, store=None) -> None:
        from ..precursor import vsignal_text
        from ..trading.strategies import SESSION_KO
        v = self._vrow((store or self.store).query("SELECT * FROM vsignals WHERE id=?", (vid,))[0])
        log.info("가상 %s %s $%.4g (%s)", "지정가 주문" if kind == "order" else "매수 체결", v["symbol"], v["entry"], v["reason"])
        if not self.notifier or not self._on("vtrade"):
            return
        link = self.link() if self.link else None
        name = (self.universe.get(v["symbol"]) or {}).get("name")
        self.notifier.send(vsignal_text(kind, v["symbol"], name, v, self.cfg, SESSION_KO.get(session, session)),
                           buttons=[[*([{"text": "📊 대시보드", "url": f"{link}/?s={v['symbol']}"}] if link else []),
                                     {"text": f"📋 {v['symbol']} 복사", "copy_text": {"text": v["symbol"]}}]])

    @staticmethod
    def _vrow(r) -> dict:
        keys = ["id", "symbol", "d0", "opened", "session", "entry", "score", "reason", "status", "closed", "exit_price",
                "exit_reason", "high", "low", "last", "high_at", "tp", "strat", "sl"]
        return dict(zip(keys, r))

    def _last_price(self, sym: str, fallback: float | None) -> float | None:
        """현재가: 유니버스(수집기 1분 현재가) → 없으면(가격이 범위 밖으로 나가 유니버스에서 빠진 경우) prices 표의 마지막 값."""
        last = (self.universe.get(sym) or {}).get("last")
        if last:
            return last
        r = self.store.query("SELECT last FROM prices WHERE symbol=? ORDER BY ts DESC LIMIT 1", (sym,))
        return r[0][0] if r and r[0][0] else fallback

    def check_vsignals(self, now: datetime, session: str, eod: bool = False, older_hours: float | None = None) -> None:
        """보유 중 가상 포지션: 1분 현재가로 익절·손절 → 가상 매도 알림. eod=True(애프터장 마감)면 남은 것 모두 정리
        (older_hours 를 주면 그보다 오래된 것만 — 수집기를 다시 켰을 때)."""
        from ..precursor import vsignal_text
        from ..trading.strategies import SESSION_KO
        from ..vstrat import STRATEGIES
        with self.store.db:                              # 안 닿은 지정가 주문은 유효 시간이 지나면 취소
            for vid, opened, strat in self.store.query("SELECT id, opened, strat FROM vsignals WHERE status='ORDER'"):
                mins = STRATEGIES.get(strat or "", {}).get("minutes", 60)
                if now - datetime.fromisoformat(opened) >= timedelta(minutes=mins) or session == AFTER or eod:
                    self.store.db.execute("UPDATE vsignals SET status='EXPIRED', closed=? WHERE id=?", (iso(now), vid))
        rows = [self._vrow(r) for r in self.store.query("SELECT * FROM vsignals WHERE status='OPEN'")]
        if older_hours is not None:
            rows = [v for v in rows if now - datetime.fromisoformat(v["opened"]) >= timedelta(hours=older_hours)]
        if eod:
            log.info("가상 포지션 정리 (%s): %d건", "애프터장 마감" if older_hours is None else f"{older_hours:g}시간 지난 것", len(rows))
        for v in rows:
            last = self._last_price(v["symbol"], v.get("last") if eod else None)
            if not last:
                continue
            self._vupdate(self.store, v, last, now, session, eod=eod)

    def _vexit(self, v: dict, last: float, hi: float, now: datetime, eod: bool = False) -> str | None:
        """정리 사유: 익절 · 손절 · 목표 근처에서 못 뚫고 밀림 · 애프터장 마감."""
        ret = (last / v["entry"] - 1) * 100
        tp, sl = v.get("tp") or 15.0, v.get("sl") or 35.0
        g = (hi / v["entry"] - 1) * 100
        if ret >= tp:
            return f"익절 +{tp:g}%"
        if ret <= -sl:
            return f"손절 −{sl:g}%"
        back = self.cfg.vsignal_near_back
        if back and g >= tp - self.cfg.vsignal_near_tp and ret <= g - back:
            return f"목표 근처 +{g:.1f}% 에서 밀려 정리"         # 예: 익절 +20% 인데 +19.5% 까지 갔다가 +17.5%
        if eod or now - datetime.fromisoformat(v["opened"]) >= timedelta(hours=24):
            return "애프터장 마감 정리"                       # 보유 안 함: 그날(애프터장까지) 안에 판다
        return None

    def _vupdate(self, store, v: dict, last: float, now: datetime, session: str, hi: float | None = None, eod: bool = False) -> str | None:
        """현재가(또는 실시간 체결가)로 고점·저점 갱신 → 정리 사유면 닫고 알림. → 사유."""
        from ..precursor import vsignal_text
        from ..trading.strategies import SESSION_KO
        hi = max(v["high"] or last, last, hi or 0)
        lo = min(v["low"] or last, last)
        hi_at = iso(now) if hi > (v["high"] or 0) else v.get("high_at")
        why = self._vexit(v, last, hi, now, eod)
        with store.db:
            if why:
                cur = store.db.execute("UPDATE vsignals SET status='CLOSED', closed=?, exit_price=?, exit_reason=?, high=?, low=?, last=?, "
                                       "high_at=? WHERE id=? AND status='OPEN'", (iso(now), last, why, hi, lo, last, hi_at, v["id"]))
                if not cur.rowcount:
                    return None                          # 다른 스레드가 먼저 닫음
            else:
                store.db.execute("UPDATE vsignals SET high=?, low=?, last=?, high_at=? WHERE id=?", (hi, lo, last, hi_at, v["id"]))
        if why:
            v.update(status="CLOSED", closed=iso(now), exit_price=last, exit_reason=why, high=hi, low=lo, last=last, high_at=hi_at)
            if self.notifier and self._on("vtrade"):
                link = self.link() if self.link else None
                name = (self.universe.get(v["symbol"]) or {}).get("name")
                self.notifier.send(vsignal_text("sell", v["symbol"], name, v, self.cfg, SESSION_KO.get(session, session)),
                                   buttons=[[{"text": "📊 대시보드", "url": f"{link}/?s={v['symbol']}"}]] if link else None)
            log.info("가상 매도 %s %s %+.1f%%", v["symbol"], why, (last / v["entry"] - 1) * 100)
        return why

    # ---------- 공매도·대차 ----------
    def poll_short_daily(self, now: datetime) -> None:
        """최근 평일 5일치 FINRA 공매도·SSR·Reg SHO 파일 중 아직 안 받은 것 (휴장일·미공개 파일은 다음에 다시)."""
        jobs = (("short_volume", self.short.finra, self.store.put_short_volume),
                ("ssr", self.short.ssr, self.store.put_ssr),
                ("reg_sho", self.short.threshold, self.store.put_reg_sho))
        for day in self.short.recent_days(now):
            for kind, fetch, put in jobs:
                key = (kind, day.isoformat())
                if key in self._short_done:
                    continue
                if self.store.query(f"SELECT 1 FROM {kind} WHERE trade_date=? LIMIT 1", key[1:]):
                    self._short_done.add(key)
                    continue
                rows = fetch(day)
                if not rows:
                    continue                       # 아직 안 나왔거나 휴장일
                self._short_done.add(key)
                mine = [r for r in rows if r["symbol"] in self.universe]
                put(mine)
                log.info("%s %s: %d종목 (유니버스)", kind, day, len(mine))

    def poll_borrow(self, now: datetime) -> None:
        _, rows = self.short.borrow()
        ts = iso(now)
        mine = [{"ts": ts, **r} for r in rows if r["symbol"] in self.universe]
        self.store.put_borrow(mine)
        log.debug("대차 %d종목", len(mine))

    def poll_halts(self) -> None:
        rows = [r for r in self.short.halts() if r["symbol"] in self.universe]
        new = [r for r in rows if not self.store.query(
            "SELECT 1 FROM halts WHERE symbol=? AND halt_ts=? AND resume_ts IS ?", (r["symbol"], r["halt_ts"], r["resume_ts"]))]
        if new:
            self.store.put_halts(new)
            for r in new:
                if not r["resume_ts"]:
                    self._event(self.clock(), r["symbol"], "halt", f"⏸ {r['symbol']} 거래정지 ({r['reason_ko']})")
                if self.cfg.halt_alerts and "뉴스" in (r["reason_ko"] or ""):
                    self._halt_alert(r)
            log.info("거래정지 %d건: %s", len(new), ", ".join(f"{r['symbol']}({r['reason_ko']})" for r in new[:10]))

    def _halt_alert(self, r: dict) -> None:
        """뉴스 대기 거래정지 → 바로 알림, 재개 시각이 나오면 한 번 더. 정지된 종목은 실시간 구독에 넣어 재개 순간 거래 터짐을 잡는다.
        10/23: 뉴스 정지 3건 중 BOLT(재개 1분 만에 +57%, 고가 +86%)·AIOK(+267%) 급등, CATX 는 −29% — 방향은 뉴스 내용에 달림."""
        sym = r["symbol"]
        key = (sym, r["halt_ts"], bool(r["resume_ts"]))
        sent = getattr(self, "_halt_sent", set())
        if key in sent:
            return
        sent.add(key)
        self._halt_sent = sent
        self._watch_now([sym])
        f = lambda ts: datetime.fromisoformat(ts).astimezone(ET).strftime("%H:%M") if ts else "?"
        name = (self.universe.get(sym) or {}).get("name") or ""
        last = (self.universe.get(sym) or {}).get("last")
        if r["resume_ts"]:
            text = f"▶ {sym} {name} 거래 재개 예정 {f(r['resume_ts'])} ET (정지 {f(r['halt_ts'])} · {r['reason_ko']})\n재개 순간 거래가 터지면 바로 알림"
        else:
            text = (f"🛑 뉴스 대기 거래정지 ({sym} · {name})\n정지 {f(r['halt_ts'])} ET · 마지막 ${last:.4g}" if last else
                    f"🛑 뉴스 대기 거래정지 ({sym} · {name})\n정지 {f(r['halt_ts'])} ET") + \
                   "\n재개 시각이 나오면 다시 알림 · 실시간 구독에 넣었습니다 (급등일 수도, 악재일 수도)"
        if self.notifier and self._on("halt"):
            self.notifier.send(text)
        log.info("뉴스 정지 알림 %s (%s)", sym, "재개 예정" if r["resume_ts"] else "정지")

    def backfill_details(self, days: int = 14, limit: int = 300) -> None:
        """세부 숫자(가격·주식 수·워런트·Form 4 거래)가 없는 최근 공시를 채운다 (예전에 받은 공시용, 시작할 때 한 번)."""
        if not self.edgar:
            return
        from ..filing_detail import extract, form4
        since = iso(self.clock() - timedelta(days=days))
        rows = self.store.query("SELECT accession, cik, form FROM filings WHERE detail IS NULL AND accepted >= ? "
                                "ORDER BY accepted DESC LIMIT ?", (since, limit))
        done = 0
        for acc, cik, form in rows:
            f = self._lookup(cik, acc)
            if not f:
                continue
            info = {}
            if form == "4":
                xml = self._safe(lambda: self.edgar.form4_xml(cik, f))
                info = form4(xml) if xml else {}
            elif self.dilution:
                info = self.dilution.detail_for(acc)
                if info is None:                       # 예전 캐시(이벤트만) → 지우고 본문을 다시 읽어 세부까지
                    (self.dilution.dir / f"{acc}.json").unlink(missing_ok=True)
                    self._safe(lambda: self.dilution._events_for(cik, f))
                    info = self.dilution.detail_for(acc) or {}
            if not info and f.items:
                info = extract(f.form, f.items, "")
            self.store.db.execute("UPDATE filings SET detail=? WHERE accession=?",
                                  (json.dumps(info, ensure_ascii=False) if info else "{}", acc))
            done += 1
        self.store.db.commit()
        if done:
            log.info("공시 세부 보충 %d건", done)

    def _lookup(self, cik: int, accession: str) -> ed.Filing | None:
        filings = self._safe(lambda: self.edgar.filings(cik)) or []
        return next((f for f in filings if f.accession == accession), None)

    @staticmethod
    def _safe(fn):
        try:
            return fn()
        except Exception as e:
            log.debug("EDGAR 보충 조회 실패: %s", e)
            return None

    def summarize(self, trade_date: str) -> None:
        n = self.store.summarize(trade_date)
        log.info("세션 요약 %s: %d행", trade_date, n)
        self._run("runners", self.record_runners, trade_date)

    def catch_up_summaries(self, days: int = 5) -> None:
        """수집기가 애프터마켓 종료 순간에 꺼져 있었으면 그날 요약·급등주가 빠진다 → 시작할 때 메운다."""
        dates = [d for (d,) in self.store.query(
            "SELECT DISTINCT trade_date FROM prices WHERE session='AFTER' ORDER BY trade_date DESC LIMIT ?", (days,))]
        now = self.clock()
        for d in dates:
            b = self.cal.bounds(datetime.fromisoformat(d).date(), AFTER)
            if b and now < b[1]:
                continue      # 아직 진행 중인 영업일
            if not self.store.query("SELECT 1 FROM session_summary WHERE trade_date=? LIMIT 1", (d,)):
                log.info("빠진 세션 요약 보충: %s", d)
                self.summarize(d)

    def record_runners(self, trade_date: str) -> None:
        """정규장 종가 기준 급등주를 기록하고, 희석 프로필·펌프 신호를 함께 남긴다 (다음 날 결과와 비교용)."""
        r = self.store.query("SELECT MAX(trade_date) FROM prices WHERE trade_date < ? AND session='REGULAR'",
                             (trade_date,))
        prev = r[0][0] if r else None
        if not prev:
            return
        day = datetime.fromisoformat(trade_date).date()
        rows = []
        for sym, pc, close in self.store.runner_candidates(trade_date, prev, self.cfg.runner_min_pct):
            row = {"trade_date": trade_date, "symbol": sym, "prev_close": pc, "close": close,
                   "change_pct": (close / pc - 1) * 100, "high": None, "low": None, "volume": None,
                   "close_loc": None, "shares": None, "rotation": None, "dilution_score": None,
                   "dilution_bucket": None, "dilution_reasons": None, "pump_flags": None}
            try:
                for cd in (self.c.candles(sym, "1d", 3) or {}).get("candles", []):
                    if cd["timestamp"][:10] == trade_date:
                        h, lo, v = float(cd["highPrice"]), float(cd["lowPrice"]), float(cd["volume"])
                        row.update(high=h, low=lo, volume=v,
                                   close_loc=(close - lo) / (h - lo) if h > lo else None)
            except TossApiError as e:
                log.debug("급등주 일봉 실패 %s: %s", sym, e)
            if self.fund:
                sh = self.fund.shares(sym, day)
                row["shares"] = sh
                if sh and row["volume"]:
                    row["rotation"] = row["volume"] / sh
                prof = self.fund.profile(sym, close, day)
                if prof:
                    row.update(dilution_score=prof.score, dilution_bucket=prof.bucket,
                               dilution_reasons=", ".join(prof.reasons + prof.recent_positive))
                has_cat = bool(self.store.query("SELECT 1 FROM filings WHERE symbol=? AND trade_date=?",
                                                (sym, trade_date)))
                row["pump_flags"] = ", ".join(self.fund.flags(sym, day, has_cat))
            rows.append(row)
        self.store.put_runners(rows)
        log.info("급등주 %s: %d종목 (+%.0f%% 이상)", trade_date, len(rows), self.cfg.runner_min_pct)
