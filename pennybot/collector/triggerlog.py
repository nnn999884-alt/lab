"""트리거 순간 호가 로거 (2026-11-15). 연구용 '거래량 첫 폭발(V0)' 트리거를 실시간(트레이딩뷰 스냅샷)으로 감지해, 그 순간의 최우선 매수·매도 호가와 잔량을 저장한다.

목적: 호가가 없어서 체결 가능성을 확인하지 못하던 연구(research/)에 '실시간으로 감지한 신호 + 기준 시각이 확인된 호가'를 쌓는다. **매매·알림에는 쓰이지 않는다.**

규칙
- 시각 세 가지를 따로 저장: 요청 시작(req_start_ts) · 응답 수신(resp_ts) · 호가 기준 시각(asof_ts, API 응답 timestamp). 수신 시각만으로 신선하다고 판정하지 않는다.
  status='ok' 는 기준 시각이 있고 0 ≤ (수신 − 기준) ≤ fresh_s(15초) 일 때만. 기준 시각 없음·낡음·시계 어긋남·오류·지연·한도는 모두 '미확인'(unconfirmed_*), 호가 값이 있어도 fresh=0.
- 과거·미래 호가로 채우지 않는다: 트리거마다 요청 1번, 실패하면 미확인으로 기록 (재시도 없음).
- origin='live' = 실시간 감시로 감지한 신호만 이 표에 들어간다. 소급 1분봉에서 찾은 신호는 이 표에 없고, research/trigger_join.py 가 둘을 맞춰 'live_logged' / 'retro_only' 로 가른다.
- 부하: 호가 요청은 분당 trigger_quote_max_per_min(기본 20)번 이하, 감지 뒤 start_delay_s(10초) 안에 못 보내면 보내지 않고 미확인(delayed/ratelimited)으로 기록. 요청은 retries=0 (긴 재시도로 호가가 낡지 않게),
  별도 스레드·별도 DB 연결, 모든 오류는 삼켜서 TV 루프·기존 감시·알림을 멈추지 않는다.
- 세션: 트레이딩뷰 누적 거래량(pre_vol/vol/post_vol)이 있는 PRE·REGULAR·AFTER 만. DAY(데이마켓)는 누적 거래량이 없어 감지하지 않음(건수만 센다).
트리거 정의는 research/early_study.py 의 V0 와 같은 임계값 (5배·$5k·5분 $2만·+10% 미만·ADV $25k·전날 종가 $0.05~$10) — 단, ① 봉은 TV 스냅샷의 누적 거래량 차이로 재구성한 근사값(bar_lag_s 로 품질 표시)
② 기준(직전 60분 중앙값) 이력이 3봉 미만이면, 감지기가 직전 60분을 끊김 없이 봤을 때만 '첫 활동'으로 인정(기준 0) — PRE 개장 직후·재시작·끊김 직후엔 트리거 안 냄 (데이마켓 이력은 TV 에 없음) ③ 세션 경계 분·빈 분은 무효 (누적 거래량 열이 세션마다 다름)."""
from __future__ import annotations

import logging
import queue
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime, timezone

log = logging.getLogger(__name__)

SCHEMA = """CREATE TABLE IF NOT EXISTS trigger_quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    origin TEXT NOT NULL,
    symbol TEXT NOT NULL, trade_date TEXT NOT NULL, session TEXT,
    bar_end_ts REAL, detected_ts REAL, bar_source TEXT, bar_lag_s REAL, warm_s REAL,
    pc REAL, trig_px REAL, dv_bar REAL, dv5 REAL, base_dv REAL, adv20 REAL,
    req_start_ts REAL, resp_ts REAL, latency_ms REAL,
    asof_ts REAL, asof_raw TEXT, asof_age_s REAL,
    bid REAL, bid_size REAL, ask REAL, ask_size REAL, levels INTEGER,
    status TEXT, fresh INTEGER, error TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS ux_trigger_quotes_day ON trigger_quotes(symbol, trade_date, origin);"""

CUM_FIELD = {"PRE": "pre_vol", "REGULAR": "vol", "AFTER": "post_vol"}


class TriggerDetector:
    """TV 스냅샷(종목별 현재가·세션 누적 거래량)에서 '분 단위 완성 봉'을 만들어 V0 트리거를 찾는다. 순수 로직(시계·DB 없음)."""

    def __init__(self, min_dv=5e3, burst_x=5.0, min_dv5=2e4, min_adv=25e3, max_rise=0.10):
        self.min_dv, self.burst_x, self.min_dv5, self.min_adv, self.max_rise = min_dv, burst_x, min_dv5, min_adv, max_rise
        self.day: str | None = None
        self.ref: dict[str, tuple[float, float]] = {}
        self.fired: set[str] = set()
        self.st: dict[str, dict] = {}
        self.skipped_day = 0
        self.warm_start: float | None = None     # 감지기가 스냅샷을 끊김 없이 보기 시작한 시각 (재시작·PRE 개장·끊김 뒤 다시 셈)
        self.last_feed: float | None = None

    def reset(self, day: str, ref: dict) -> None:
        self.day, self.ref, self.fired, self.st, self.skipped_day = day, ref, set(), {}, 0
        self.warm_start = self.last_feed = None

    def feed(self, snap: dict, t_now: float, session: str) -> list[dict]:
        field = CUM_FIELD.get(session)
        if field is None:
            self.skipped_day += 1
            return []
        if self.last_feed is not None and t_now - self.last_feed > 120:     # 스냅샷이 2분 넘게 끊김 → 그 사이 거래를 못 봤으니 '이력 없음'을 믿을 수 없다
            self.warm_start = t_now
        if self.warm_start is None:
            self.warm_start = t_now
        self.last_feed = t_now
        m_now = int(t_now // 60)
        out = []
        for sym, d in snap.items():
            cum, px = d.get(field), d.get("px")
            if cum is None or not px:
                continue
            s = self.st.get(sym)
            if s is None:                                  # 첫 관측: 이 분의 시작 누적을 몰라 이 분은 무효
                self.st[sym] = dict(m=m_now, t=t_now, px=px, cum=cum, base=None, hist=deque(), ses=session)
                continue
            if s.get("ses") != session:                    # 세션이 바뀌면 누적 거래량 열이 달라짐(pre_vol → vol → post_vol) → 경계 분은 무효
                s.update(m=m_now, t=t_now, px=px, cum=cum, base=None, ses=session)
                continue
            if m_now > s["m"]:
                consecutive = (m_now == s["m"] + 1)
                trig = self._finish(sym, s, session, t_now) if (consecutive and s["base"] is not None) else None
                if trig:
                    out.append(trig)
                s["base"] = s["cum"] if consecutive else None   # 빈 분이 끼면 새 분의 시작 누적을 모름 → 무효
                s["m"] = m_now
            s["t"], s["px"], s["cum"] = t_now, px, cum
        return out

    def _finish(self, sym: str, s: dict, session: str, t_detect: float):
        end = (s["m"] + 1) * 60
        vol = s["cum"] - s["base"]
        if vol <= 0:
            return None
        dv = vol * s["px"]
        h = s["hist"]
        h.append((end, dv))
        while h and h[0][0] < end - 3700:
            h.popleft()
        r = self.ref.get(sym)
        if not r or sym in self.fired:
            return None
        pc, adv = r
        c = s["px"]
        if not (0.05 <= pc <= 10) or adv < self.min_adv or not (0.10 <= c <= 10) or c >= (1 + self.max_rise) * pc:
            return None
        base_bars = [x for e, x in h if end - 3600 <= e < end - 300]
        warm_s = end - self.warm_start                  # 감지기가 스냅샷을 끊김 없이 본 시간 (3600 미만이면 직전 60분 기준이 불완전: PRE 개장 직후·재시작·끊김 직후)
        if len(base_bars) >= 3:
            base = sorted(base_bars)[len(base_bars) // 2]
        elif warm_s >= 3600:                           # 직전 60분을 끊김 없이 봤는데 이력이 없으면 진짜 '첫 활동' (소급 연구와 같은 기준 0)
            base = 0.0
        else:                                          # 이력도 없고 직전 60분도 못 봤으면 기준을 알 수 없음 → 트리거 아님
            return None
        dv5 = sum(x for e, x in h if end - 300 < e <= end)
        if dv < max(self.burst_x * base, self.min_dv) or dv5 < self.min_dv5:
            return None
        self.fired.add(sym)
        return dict(symbol=sym, session=session, trade_date=self.day, bar_end_ts=float(end), detected_ts=float(t_detect), bar_source="tv_cum",
                    bar_lag_s=float(end - s["t"]), warm_s=float(warm_s), pc=float(pc), trig_px=float(c), dv_bar=float(dv), dv5=float(dv5), base_dv=float(base), adv20=float(adv))


def load_ref(store, day: str) -> dict:
    """{종목: (전날 종가, 20일 평균 거래대금)} — day 이전 일봉만 (신호 시점에 알 수 있는 값)."""
    rows = store.query("SELECT symbol, d, close, volume FROM daily_bars WHERE d < ? AND d >= date(?, '-45 days') AND close > 0 ORDER BY symbol, d", (day, day))
    by: dict[str, list] = {}
    for s, d, c, v in rows:
        by.setdefault(s, []).append((c, c * (v or 0)))
    ref = {}
    for s, x in by.items():
        if len(x) >= 10:
            last20 = [z for _, z in x[-20:]]
            ref[s] = (x[-1][0], sum(last20) / len(last20))
    return ref


def parse_asof(raw) -> float | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except (ValueError, TypeError):
        return None


class TriggerQuoteLogger:
    def __init__(self, client, db_path: str, clock=time.time, *, max_per_min: int = 20, start_delay_s: float = 10.0, fresh_s: float = 15.0,
                 queue_max: int = 500, store_factory=None):
        self.c, self.db_path, self.clock = client, db_path, clock
        self.max_per_min, self.start_delay_s, self.fresh_s = max(1, int(max_per_min)), float(start_delay_s), float(fresh_s)
        self.det = TriggerDetector()
        self.q: queue.Queue = queue.Queue(maxsize=queue_max)
        self._ovf: deque = deque(maxlen=2000)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._store_factory = store_factory
        self._last_req = 0.0
        self._pause_until = 0.0
        self.counts: dict[str, int] = {}
        self.detected = 0
        self.last_trigger_ts: float | None = None
        self.last_error = ""
        self._warned = 0.0

    # ---- TV 스레드에서 호출 (절대 막지 않고, 절대 예외를 밖으로 내지 않는다) ----
    def feed(self, snap: dict, t_now: float, session: str, trade_date: str | None, store) -> int:
        if not trade_date:
            return 0
        if self.det.day != trade_date:
            self.det.reset(trade_date, load_ref(store, trade_date))
        n = 0
        for trig in self.det.feed(snap, t_now, session):
            self.detected += 1
            self.last_trigger_ts = trig["detected_ts"]
            try:
                self.q.put_nowait(trig)
            except queue.Full:
                self._ovf.append(trig)
            n += 1
        return n

    # ---- 작업 스레드 ----
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._worker, name="trigger-quotes", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _open_store(self):
        if self._store_factory:
            return self._store_factory()
        from .store import Store
        return Store(self.db_path)

    def _worker(self) -> None:
        try:
            store = self._open_store()
            with store.db:
                for stmt in SCHEMA.split(";"):
                    if stmt.strip():
                        store.db.execute(stmt)
        except Exception as e:                            # 표를 못 만들면 조용히 포기 (수집기엔 영향 없음)
            self.last_error = f"schema: {e}"
            log.warning("트리거 호가 로거 시작 실패(무시): %s", e)
            return
        while not self._stop.is_set():
            try:
                self._drain_overflow(store)
                try:
                    trig = self.q.get(timeout=1.0)
                except queue.Empty:
                    continue
                self._handle(store, trig)
            except Exception as e:                        # 어떤 오류도 스레드를 죽이지 않음
                self.last_error = str(e)[:200]
                if time.time() - self._warned > 300:
                    self._warned = time.time()
                    log.warning("트리거 호가 로거 오류(무시): %s", e)

    def _drain_overflow(self, store) -> None:
        while self._ovf:
            self._write(store, self._ovf.popleft(), status="unconfirmed_overflow")

    def _handle(self, store, trig: dict) -> None:
        now = self.clock()
        if trig.get("warm_s", 1e9) < 3600:                  # 직전 60분 기준이 불완전한 트리거(PRE 개장 직후·재시작·끊김 직후): 기록만 하고 호가는 요청하지 않음 (한도 절약, 연구에서 제외)
            return self._write(store, trig, status="unconfirmed_warmup")
        if now - trig["detected_ts"] > self.start_delay_s:
            return self._write(store, trig, status="unconfirmed_delayed")
        if now < self._pause_until:
            return self._write(store, trig, status="unconfirmed_ratelimited")
        gap = 60.0 / self.max_per_min
        wait = self._last_req + gap - now
        if wait > 0:
            if now + wait - trig["detected_ts"] > self.start_delay_s:
                return self._write(store, trig, status="unconfirmed_ratelimited")
            time.sleep(wait)
        self._last_req = self.clock()
        req_start = self.clock()
        try:
            ob = self.c.orderbook(trig["symbol"]) if hasattr(self.c, "orderbook") else self.c.request(
                "GET", "/api/v1/orderbook", params={"symbol": trig["symbol"]}, retries=0)
        except Exception as e:
            resp = self.clock()
            code = getattr(getattr(e, "response", None), "status_code", None) or getattr(e, "status", None)
            if code == 429 or "429" in str(e) or "rate" in str(e).lower():
                self._pause_until = self.clock() + 60.0
                return self._write(store, trig, status="unconfirmed_ratelimited", req_start_ts=req_start, resp_ts=resp, error=str(e)[:160])
            return self._write(store, trig, status="unconfirmed_error", req_start_ts=req_start, resp_ts=resp, error=str(e)[:160])
        resp = self.clock()
        self._write_quote(store, trig, ob, req_start, resp)

    def classify(self, ob: dict, resp: float) -> tuple[str, float | None, float | None]:
        raw = ob.get("timestamp")
        asof = parse_asof(raw)
        if asof is None:
            return "unconfirmed_no_asof", None, None
        age = resp - asof
        if age < -2.0:
            return "unconfirmed_clock", asof, age
        if age > self.fresh_s:
            return "unconfirmed_stale_asof", asof, age
        return "ok", asof, age

    def _write_quote(self, store, trig: dict, ob: dict, req_start: float, resp: float) -> None:
        bids, asks = ob.get("bids") or [], ob.get("asks") or []
        status, asof, age = self.classify(ob, resp)
        if not bids or not asks:
            status = "unconfirmed_error" if status == "ok" else status
        def f(x, k):
            try:
                return float(x[0][k]) if x else None
            except (KeyError, TypeError, ValueError, IndexError):
                return None
        self._write(store, trig, status=status, req_start_ts=req_start, resp_ts=resp, asof_ts=asof, asof_raw=ob.get("timestamp"), asof_age_s=age,
                    bid=f(bids, "price"), bid_size=f(bids, "volume"), ask=f(asks, "price"), ask_size=f(asks, "volume"), levels=min(len(bids), len(asks)),
                    error=None if (bids and asks) or status != "unconfirmed_error" else "빈 호가")

    def _write(self, store, trig: dict, *, status: str, req_start_ts=None, resp_ts=None, asof_ts=None, asof_raw=None, asof_age_s=None,
               bid=None, bid_size=None, ask=None, ask_size=None, levels=None, error=None) -> None:
        self.counts[status] = self.counts.get(status, 0) + 1
        row = (("live", trig["symbol"], trig["trade_date"], trig["session"], trig["bar_end_ts"], trig["detected_ts"], trig["bar_source"], trig["bar_lag_s"], trig.get("warm_s"),
                trig["pc"], trig["trig_px"], trig["dv_bar"], trig["dv5"], trig["base_dv"], trig["adv20"], req_start_ts, resp_ts,
                (resp_ts - req_start_ts) * 1000 if (req_start_ts and resp_ts) else None, asof_ts, asof_raw, asof_age_s,
                bid, bid_size, ask, ask_size, levels, status, 1 if status == "ok" else 0, error))
        for attempt in range(3):
            try:
                with store.db:
                    store.db.execute("INSERT OR IGNORE INTO trigger_quotes (origin, symbol, trade_date, session, bar_end_ts, detected_ts, bar_source, bar_lag_s, warm_s, pc, trig_px, dv_bar, dv5, base_dv, adv20, "
                                     "req_start_ts, resp_ts, latency_ms, asof_ts, asof_raw, asof_age_s, bid, bid_size, ask, ask_size, levels, status, fresh, error) "
                                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
                return
            except sqlite3.OperationalError as e:         # database is locked 등: 잠깐 쉬고 재시도, 끝내 안 되면 포기(과거·미래 값으로 안 채움)
                if attempt == 2:
                    self.last_error = f"db: {e}"
                    return
                time.sleep(0.5 * (attempt + 1))

    def status(self) -> dict:
        return {"on": bool(self._thread and self._thread.is_alive()), "detected": self.detected, "queue": self.q.qsize(), "overflow_pending": len(self._ovf),
                "counts": dict(self.counts), "last_trigger_age_s": round(self.clock() - self.last_trigger_ts, 1) if self.last_trigger_ts else None,
                "skipped_day_ticks": self.det.skipped_day, "error": self.last_error}
