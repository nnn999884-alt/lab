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

import json
import logging
import time
from datetime import datetime, timedelta, timezone

from ..config import CollectorConfig
from ..dilution import classify
from ..screener import edgar as ed
from ..sessions import AFTER, CLOSED, DAY, PRE, REGULAR, SessionCalendar
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
                 short=None, notifier=None, link=None):
        self.c, self.store, self.cal, self.cfg = client, store, calendar, cfg
        self.edgar = edgar_client
        self.dilution = dilution          # DilutionService: 공시 본문으로 희석 이벤트 판정
        self.fund = fundamentals          # Fundamentals: 급등주 희석 프로필·펌프 신호
        self.news = news                  # NewsClient: 보도자료 피드
        self.short = short                # ShortData: 공매도·대차·SSR·Reg SHO·거래정지
        self.notifier = notifier          # 급등 전조 알림 (텔레그램·웹훅)
        self.link = link                  # () -> 대시보드 외부 주소
        self._pre_thread = None
        self._pre_tried = None
        self._ev_last: dict = {}                  # (종목, 종류) → 마지막 이벤트 시각
        self._rank_prev: dict = {}                # 랭킹 종류 → {종목: 순위}
        self._short_done: set[tuple[str, str]] = set()   # (종류, 날짜) 이미 받은 일별 파일
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
            self._run("filing_details", self.backfill_details)
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
        if self.short and c.short_data:      # 휴장 중에도 (일별 파일은 장 마감 뒤에 나온다)
            if self._due("short_daily", now, timedelta(minutes=c.short_daily_poll_minutes)):
                self._run("short_daily", self.poll_short_daily, now)
            if self._due("borrow", now, timedelta(minutes=c.borrow_poll_minutes)):
                self._run("borrow", self.poll_borrow, now)
            if self._due("halts", now, timedelta(minutes=c.halts_poll_minutes)):
                self._run("halts", self.poll_halts)
        if c.precursor:
            self._run("precursor_daily", self.ensure_precursor_daily, trade_date, session)
            if session in (DAY, PRE, REGULAR) and self._due("precursor_live", now, timedelta(seconds=60)):
                self._run("precursor_live", self.poll_precursor_live, now, trade_date, session)
        if session == CLOSED:
            return
        every = timedelta(seconds=c.ranking_poll_live_seconds) if session != AFTER else timedelta(minutes=c.ranking_poll_minutes)
        if self._due("rankings", now, every):
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
                    from ..trading.messages import money_s, qty_s
                    vol = f" · 2분 거래량 {qty_s(v)}주 ({money_s(d)})"
                except Exception:
                    pass
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
        from ..precursor import daily
        store = Store(self.cfg.db_path)                # 스레드 전용 연결
        rows, n = [], 0
        for sym in symbols:
            try:
                cs = (self.c.candles(sym, "1d", 70) or {}).get("candles", [])
            except Exception as e:
                log.debug("전조 일봉 실패 %s: %s", sym, e)
                continue
            bars = sorted((cd["timestamp"][:10], float(cd["openPrice"]), float(cd["highPrice"]), float(cd["lowPrice"]),
                           float(cd["closePrice"]), float(cd["volume"])) for cd in cs)
            bars = [b for b in bars if b[0] <= d0.isoformat()]     # 기준일 뒤(진행 중인 봉)는 뺀다
            store.put_daily_bars([{"symbol": sym, "d": b[0], "open": b[1], "high": b[2], "low": b[3], "close": b[4],
                                   "volume": b[5]} for b in bars[-70:]])
            r = daily(bars)
            if r and r["d0"] == d0.isoformat() and r["score"] >= 2:
                rows.append({"d0": r["d0"], "symbol": sym, "score": r["score"], "signals": json.dumps(r["signals"]),
                             "metrics": json.dumps(r["metrics"]), "close": r["close"], "created": iso(self.clock())})
            n += 1
        store.put_precursor_daily(rows)
        log.info("급등 전조 일봉 점수 %s: %d종목 계산, 2점 이상 %d, %d점 이상 %d", d0, n, len(rows),
                 self.cfg.precursor_min_score, sum(1 for r in rows if r["score"] >= self.cfg.precursor_min_score))
        top = [r["symbol"] for r in sorted(rows, key=lambda r: -r["score"])][:200]
        self.build_stock_tags(d0, top, store)
        return len(rows)

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
            rs = recent({"REVERSE_SPLIT"}, 365)
            if rs:
                tags.append(f"액면병합 {rs.filed:%y/%m/%d}")
                info["reverse_split"] = rs.filed.isoformat()
            if recent({"CONVERTIBLE"}, 180):
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
        from ..precursor import alert_buttons, alert_text, live
        from ..trading.strategies import SESSION_KO
        d0 = self.store.query("SELECT MAX(d0) FROM precursor_daily")
        if not d0 or not d0[0][0]:
            return
        d0 = d0[0][0]
        rows = self.store.query("SELECT symbol, score, signals, metrics, close FROM precursor_daily WHERE d0=? AND score>=? "
                                "ORDER BY score DESC, json_extract(metrics, '$.vol_ratio') DESC LIMIT ?",
                                (d0, self.cfg.precursor_min_score, self.cfg.precursor_live_max))
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
            lv = live([b for b in bars if b[0] >= start], close, now)
            lv["metrics"]["session"] = session
            fire = {"wake", "creep"} <= set(lv["signals"]) and sym not in prev_alert
            out.append({"d0": d0, "symbol": sym, "ts": iso(now), "signals": json.dumps(lv["signals"]),
                        "metrics": json.dumps(lv["metrics"]), "alerted": iso(now) if fire else prev_alert.get(sym)})
            if fire and self.notifier:
                day = {"score": score, "signals": json.loads(sig), "metrics": json.loads(met)}
                link = None
                try:
                    link = self.link() if self.link else None
                except Exception:
                    pass
                sh = self.store.query("SELECT shares FROM stock_info WHERE symbol=?", (sym,))
                tg = self.store.query("SELECT tags FROM stock_tags WHERE symbol=? ORDER BY d0 DESC LIMIT 1", (sym,))
                pats = [lab for k, lab in (("compress", "응축"), ("accum", "매집 의심"), ("base", "바닥 다지기")) if k in day["signals"]]
                self.notifier.send(alert_text(sym, (self.universe.get(sym) or {}).get("name"), day, lv,
                                              SESSION_KO.get(session, session), link, now,
                                              shares=sh[0][0] if sh and sh[0][0] else None,
                                              tags=(json.loads(tg[0][0]) if tg else []) + pats),
                                   buttons=alert_buttons(sym, link))
                log.info("급등 전조 알림 %s (%s)", sym, ", ".join(lv["signals"]))
            if fire:
                self._event(now, sym, "precursor", f"🔥 {sym} 급등 전조 발동 ({score}점 · 1시간 거래량 "
                            f"{lv['metrics'].get('vol_x', 0):.1f}배 · {lv['metrics'].get('chg', 0):+.1f}%)", lv["metrics"].get("chg"))
        self.store.put_precursor_live(out)

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
            log.info("거래정지 %d건: %s", len(new), ", ".join(f"{r['symbol']}({r['reason_ko']})" for r in new[:10]))

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
