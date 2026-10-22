"""24시간 세션별 데이터 수집기.

하는 일 (주기는 config.collector):
  - 유니버스: 토스 /stocks/all(NASDAQ·NYSE·AMEX 보통주) → 현재가로 가격대 필터 → 하루 1번
  - 현재가: 유니버스 전체를 200개씩 묶어 매분 (데이마켓 포함 4 세션 모두)
  - 감시 종목(watch): 공시 뜬 종목 > 랭킹 종목 > 급변 종목 순으로 N개 선정
      → 호가 스냅샷 매분, 1분봉은 주기적으로 + 세션이 바뀔 때마다 동기화
  - 랭킹: 시장/토스 거래대금, 급등/급락 스냅샷
  - SEC 최신 공시(Form 4, 8-K, 424B*): 접수 시각 → 어느 세션에 공개됐는지 기록
  - 애프터마켓이 끝나면 그 영업일 세션 요약(session_summary) 생성

한 작업이 실패해도 나머지는 계속 돈다. 재시작해도 같은 키는 덮어쓰므로 안전하다.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from ..config import CollectorConfig
from ..dilution import classify
from ..screener import edgar as ed
from ..sessions import AFTER, CLOSED, SessionCalendar
from ..toss_api import TossApiError
from .store import Store

log = logging.getLogger(__name__)

RANKINGS = [  # (type, duration)
    ("MARKET_TRADING_AMOUNT", "realtime"),
    ("TOSS_SECURITIES_TRADING_AMOUNT", "realtime"),
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
                 clock=lambda: datetime.now(timezone.utc), dilution=None, fundamentals=None, news=None):
        self.c, self.store, self.cal, self.cfg = client, store, calendar, cfg
        self.edgar = edgar_client
        self.dilution = dilution          # DilutionService: 공시 본문으로 희석 이벤트 판정
        self.fund = fundamentals          # Fundamentals: 급등주 희석 프로필·펌프 신호
        self.news = news                  # NewsClient: 보도자료 피드
        self._seen_news: set[str] = set()
        self.clock = clock
        self.universe: dict[str, dict] = {}       # symbol -> {market, name, last}
        self.cik_to_symbol: dict[int, str] = {}
        self.watch: list[str] = []
        self.recent_filers: dict[str, datetime] = {}
        self._next: dict[str, datetime] = {}
        self._last_session: tuple | None = None
        self._seen_accessions: set[str] = set()

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
        except Exception:
            log.exception("%s 실패", name)

    def tick(self) -> None:
        now = self.clock()
        trade_date, session = self.cal.classify(now)
        c = self.cfg

        if self._due("universe", now, timedelta(hours=c.universe_refresh_hours)) or not self.universe:
            self._run("universe", self.refresh_universe, now)

        if self._last_session is None:
            self._run("catch_up", self.catch_up_summaries)
        # 세션이 바뀌면: 직전 세션의 1분봉을 마저 받고, 애프터마켓이 끝났으면 요약
        cur = (trade_date, session)
        if self._last_session and cur != self._last_session:
            prev_date, prev_session = self._last_session
            log.info("세션 변경 %s/%s → %s/%s", prev_date, prev_session, trade_date, session)
            self._run("candles", self.sync_candles, now)
            if prev_session == AFTER and prev_date:
                self._run("summary", self.summarize, prev_date.isoformat())
        self._last_session = cur

        if self.edgar and self._due("edgar", now, timedelta(minutes=c.edgar_poll_minutes)):
            self._run("edgar", self.poll_edgar, now)
        if self.news and self._due("news", now, timedelta(minutes=c.news_poll_minutes)):
            self._run("news", self.poll_news, now)
        if session == CLOSED:
            return
        if self._due("rankings", now, timedelta(minutes=c.ranking_poll_minutes)):
            self._run("rankings", self.poll_rankings, now, trade_date, session)
            self._run("watch", self.select_watch, now, trade_date)
        if self._due("prices", now, timedelta(seconds=c.price_poll_seconds)):
            self._run("prices", self.poll_prices, now, trade_date, session)
        if self._due("orderbook", now, timedelta(seconds=c.orderbook_poll_seconds)):
            self._run("orderbook", self.poll_orderbooks, now, trade_date, session)
        if self._due("candles", now, timedelta(minutes=c.candle_sync_minutes)):
            self._run("candles", self.sync_candles, now)

    def run(self, sleep=time.sleep) -> None:
        log.info("수집기 시작 (DB=%s)", self.cfg.db_path)
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

    def poll_prices(self, now: datetime, trade_date, session: str) -> None:
        rows = []
        for p in self.c.prices(sorted(self.universe)):
            rows.append({"symbol": p["symbol"], "ts": iso(now), "trade_date": trade_date.isoformat(),
                         "session": session, "last": float(p["lastPrice"]), "price_ts": p.get("timestamp")})
            if p["symbol"] in self.universe:
                self.universe[p["symbol"]]["last"] = float(p["lastPrice"])
        self.store.put_prices(rows)

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
                buy = None
                if f["form"] == "4" and detail:
                    buy = self._safe(lambda: ed.insider_buy_usd(self.edgar.form4_xml(f["cik"], detail)))
                evs = []
                if detail and f["form"] != "4":
                    if self.dilution:
                        evs = self._safe(lambda: self.dilution._events_for(f["cik"], detail)) or []
                    else:
                        evs = classify(detail.form, detail.items, "", detail.filed, detail.accession)
                direction = (1 if sum(e.direction for e in evs) > 0 else -1 if sum(e.direction for e in evs) < 0
                             else 0) if evs else (1 if buy and buy > 0 else 0)
                rows.append({"accession": f["accession"], "cik": f["cik"], "symbol": sym, "form": f["form"],
                             "items": detail.items if detail else "", "accepted": iso(f["accepted"]),
                             "trade_date": d.isoformat() if d else None, "session": sess,
                             "insider_buy_usd": buy, "events": ",".join(sorted({e.kind for e in evs})),
                             "direction": direction,
                             "amount": max((e.amount or 0 for e in evs), default=0) or None})
                self.recent_filers[sym] = now
        if rows:
            self.store.put_filings(rows)
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
