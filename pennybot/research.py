"""과거 데이터로 가설 검증: 급등주 다음 날 흐름이 공매도·대차·희석·부양 동기에 따라 달라지는가.

수집기를 몇 주 돌릴 필요 없이 지금 받을 수 있는 과거 자료로 바로 본다.
  1. 일봉: 토스 1일봉 (상장 종목 중 현재가 $15 이하) → state/research.db daily_bars
  2. FINRA 일별 공매도 거래량 (과거 파일도 공개) → short_volume
  3. 급등일: 종가가 전일 대비 +30% 이상 ($0.2~$10) → 다음 날 시가·종가 수익률
  4. 급등일 기준(as-of) SEC 공시로 희석 점수·부양 동기 (그날까지 제출된 공시만 → 미래 정보 없음)
  5. 요인별로 묶어 평균·중앙값·상승 비율 → state/research/<시각>/report.json, 대시보드 '분석' 탭

한계 (리포트에도 적는다):
  - 일봉뿐이라 데이마켓 가격이 없다 → '급등일 종가에 사서 다음 날 시가/종가에 판다' 로 근사
  - 지금 상장된 종목만 (그사이 상폐된 종목은 빠짐 → 결과가 실제보다 좋게 나온다)
  - 대차 수수료(IBKR)는 과거 자료가 없어 여기서는 못 본다 (수집기가 쌓는 중)
"""
from __future__ import annotations

import json
import logging
import sqlite3
import statistics
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_bars (symbol TEXT, d TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (symbol, d));
CREATE TABLE IF NOT EXISTS short_volume (trade_date TEXT, symbol TEXT, short_volume REAL, exempt_volume REAL,
    total_volume REAL, PRIMARY KEY (trade_date, symbol));
CREATE TABLE IF NOT EXISTS finra_days (trade_date TEXT PRIMARY KEY, n INTEGER);
CREATE TABLE IF NOT EXISTS hist_runners (d TEXT, symbol TEXT, prev_close REAL, close REAL, change_pct REAL,
    close_loc REAL, vol_ratio REAL, next_open_ret REAL, next_close_ret REAL, next_high_ret REAL,
    short_ratio REAL, short_ratio_5d REAL, dilution_score REAL, motives TEXT, motive_detail TEXT,
    day_vwap REAL, day_volume REAL, day_dollar REAL, day_gap REAL, ret_pre REAL, ret_open REAL, ret_30m REAL,
    ret_best REAL, ret_worst REAL,
    PRIMARY KEY (d, symbol));
CREATE TABLE IF NOT EXISTS bars_1m (symbol TEXT, ts TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (symbol, ts));
CREATE TABLE IF NOT EXISTS bars_1m_done (symbol TEXT, d TEXT, PRIMARY KEY (symbol, d));
"""


class ResearchDB:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def put(self, table: str, rows: list[dict]) -> None:
        if rows:
            cols = list(rows[0])
            with self.db:
                self.db.executemany(f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                                    [tuple(r[c] for c in cols) for r in rows])

    def q(self, sql: str, args=()) -> list[tuple]:
        return self.db.execute(sql, args).fetchall()


# ---------- 1. 일봉 ----------
def backfill_daily(client, rdb: ResearchDB, symbols: list[str], days: int) -> int:
    need_from = (date.today() - timedelta(days=int(days * 1.5) + 10)).isoformat()
    done = 0
    for i, sym in enumerate(symbols):
        have = rdb.q("SELECT MIN(d), MAX(d) FROM daily_bars WHERE symbol=?", (sym,))[0]
        if have[0] and have[0] <= need_from and have[1] >= (date.today() - timedelta(days=4)).isoformat():
            continue                                    # 이미 받음
        rows, before = [], None
        for _ in range(4):                              # 200봉 × 4 = 800 거래일까지
            try:
                res = client.candles(sym, "1d", 200, before) or {}
            except Exception as e:
                log.debug("일봉 실패 %s: %s", sym, e)
                break
            page = res.get("candles", [])
            for cd in page:
                rows.append({"symbol": sym, "d": cd["timestamp"][:10], "open": float(cd["openPrice"]),
                             "high": float(cd["highPrice"]), "low": float(cd["lowPrice"]),
                             "close": float(cd["closePrice"]), "volume": float(cd["volume"])})
            before = res.get("nextBefore")
            if not page or not before or page[-1]["timestamp"][:10] <= need_from:
                break
        rdb.put("daily_bars", rows)
        done += 1
        if (i + 1) % 200 == 0:
            log.info("일봉 %d/%d 종목", i + 1, len(symbols))
    return done


# ---------- 2. FINRA 공매도 ----------
def backfill_finra(short, rdb: ResearchDB, start: date, end: date, symbols: set[str]) -> int:
    got = {r[0] for r in rdb.q("SELECT trade_date FROM finra_days")}
    d, n = start, 0
    while d <= end:
        if d.weekday() < 5 and d.isoformat() not in got:
            try:
                rows = short.finra(d)
            except Exception as e:
                log.debug("FINRA %s 실패: %s", d, e)
                rows = []
            if rows:                                    # 휴장일은 파일이 없다
                mine = [r for r in rows if r["symbol"] in symbols]
                rdb.put("short_volume", mine)
                rdb.put("finra_days", [{"trade_date": d.isoformat(), "n": len(mine)}])
                n += 1
        d += timedelta(days=1)
    return n


# ---------- 3. 급등일 ----------
def find_runners(rdb: ResearchDB, start: date, min_pct: float = 30.0, min_price: float = 0.2,
                 max_price: float = 10.0) -> list[dict]:
    out = []
    syms = [r[0] for r in rdb.q("SELECT DISTINCT symbol FROM daily_bars")]
    for sym in syms:
        bars = rdb.q("SELECT d, open, high, low, close, volume FROM daily_bars WHERE symbol=? ORDER BY d", (sym,))
        for i in range(21, len(bars) - 1):
            d, o, h, lo, c, v = bars[i]
            pc = bars[i - 1][4]
            if d < start.isoformat() or not pc or pc <= 0 or not (min_price <= c <= max_price):
                continue
            chg = (c / pc - 1) * 100
            if chg < min_pct:
                continue
            nd = bars[i + 1]
            avg_vol = statistics.mean(b[5] for b in bars[i - 20:i]) or None
            out.append({"d": d, "next_d": nd[0], "symbol": sym, "prev_close": pc, "close": c, "change_pct": chg,
                        "close_loc": (c - lo) / (h - lo) if h > lo else None,
                        "vol_ratio": v / avg_vol if avg_vol else None,
                        "next_open_ret": (nd[1] / c - 1) * 100, "next_close_ret": (nd[4] / c - 1) * 100,
                        "next_high_ret": (nd[2] / c - 1) * 100,
                        "_closes": [(date.fromisoformat(b[0]), b[4]) for b in bars[max(0, i - 60):i + 1]]})
    return out


# ---------- 3b. 데이마켓 실제 재현 (토스 1분봉, 데이마켓 봉 포함 · 1년 이상 보관) ----------
def _et():
    from zoneinfo import ZoneInfo
    return ZoneInfo("America/New_York")


def window(d: str, next_d: str):
    """급등일 d 밤의 데이마켓(동부 20:00, 다음 영업일 기준 전날 밤) ~ 다음 영업일 10:30 (UTC 시각)."""
    et = _et()
    day_start = datetime.fromisoformat(d).replace(hour=20, tzinfo=et)
    nd = datetime.fromisoformat(next_d)
    return day_start, nd.replace(hour=4, tzinfo=et), nd.replace(hour=9, minute=30, tzinfo=et), \
        nd.replace(hour=10, minute=30, tzinfo=et)


def fetch_window(client, rdb: ResearchDB, sym: str, d: str, next_d: str) -> list[tuple]:
    """그 구간 1분봉 (캐시 우선). → [(ts_utc_iso, o, h, l, c, v)] 시간순."""
    start, _, _, end = window(d, next_d)
    s_utc, e_utc = start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()
    if not rdb.q("SELECT 1 FROM bars_1m_done WHERE symbol=? AND d=?", (sym, d)):
        before, rows = end.isoformat(timespec="milliseconds"), []
        for _ in range(8):                               # 거래가 많은 종목은 14시간 ≈ 870봉 → 5페이지
            try:
                res = client.candles(sym, "1m", 200, before) or {}
            except Exception as e:
                log.debug("1분봉 실패 %s %s: %s", sym, d, e)
                return []
            page = res.get("candles", [])
            for cd in page:
                ts = datetime.fromisoformat(cd["timestamp"].replace("Z", "+00:00")).astimezone(timezone.utc)
                rows.append({"symbol": sym, "ts": ts.isoformat(), "open": float(cd["openPrice"]),
                             "high": float(cd["highPrice"]), "low": float(cd["lowPrice"]),
                             "close": float(cd["closePrice"]), "volume": float(cd["volume"])})
            before = res.get("nextBefore")
            if not page or not before or datetime.fromisoformat(page[-1]["timestamp"].replace("Z", "+00:00")) <= start:
                break
        rdb.put("bars_1m", rows)
        rdb.put("bars_1m_done", [{"symbol": sym, "d": d}])
    return rdb.q("SELECT ts, open, high, low, close, volume FROM bars_1m WHERE symbol=? AND ts>=? AND ts<=? ORDER BY ts",
                 (sym, s_utc, e_utc))


def simulate(bars: list[tuple], d: str, next_d: str, close: float) -> dict:
    """데이마켓 거래량 가중 평균가(VWAP)에 사서 → 프리마켓 마지막 / 정규장 시가 / 개장 30분 뒤 에 판다 (%)."""
    _, pre_start, open_, end = window(d, next_d)
    cut = lambda t: t.astimezone(timezone.utc).isoformat()
    day = [b for b in bars if b[0] < cut(pre_start)]
    pre = [b for b in bars if cut(pre_start) <= b[0] < cut(open_)]
    reg = [b for b in bars if cut(open_) <= b[0] <= cut(end)]
    vol = sum(b[5] for b in day)
    out = {"day_vwap": None, "day_volume": vol, "day_dollar": None, "day_gap": None, "ret_pre": None,
           "ret_open": None, "ret_30m": None, "ret_best": None, "ret_worst": None}
    if not day or vol <= 0:
        return out                                       # 데이마켓 거래 없음 → 살 수 없었다
    vwap = sum((b[2] + b[3] + b[4]) / 3 * b[5] for b in day) / vol
    r = lambda px: (px / vwap - 1) * 100 if px else None
    after = pre + reg
    t30 = cut(open_ + timedelta(minutes=30))
    out.update(day_vwap=vwap, day_dollar=vwap * vol, day_gap=(vwap / close - 1) * 100,
               ret_pre=r(pre[-1][4]) if pre else None, ret_open=r(reg[0][1]) if reg else None,
               ret_30m=r(next((b[4] for b in reversed(reg) if b[0] <= t30), None)) if reg else None,
               ret_best=r(max(b[2] for b in after)) if after else None,
               ret_worst=r(min(b[3] for b in after)) if after else None)
    return out


def add_day_market(client, rdb: ResearchDB, runners: list[dict]) -> None:
    for i, r in enumerate(runners):
        bars = fetch_window(client, rdb, r["symbol"], r["d"], r["next_d"])
        r.update(simulate(bars, r["d"], r["next_d"], r["close"]))
        if (i + 1) % 100 == 0:
            log.info("데이마켓 재현 %d/%d", i + 1, len(runners))


def add_short(rdb: ResearchDB, runners: list[dict]) -> None:
    for r in runners:
        sv = rdb.q("SELECT short_volume, total_volume FROM short_volume WHERE symbol=? AND trade_date<=? "
                   "ORDER BY trade_date DESC LIMIT 5", (r["symbol"], r["d"]))
        ratios = [a / b for a, b in sv if b]
        r["short_ratio"] = ratios[0] if ratios and sv else None
        r["short_ratio_5d"] = statistics.mean(ratios) if ratios else None


# ---------- 4. 공시 기준 희석·부양 동기 (as-of) ----------
def add_filings(dil, runners: list[dict], max_texts: int = 15) -> None:
    """종목마다 submissions·재무 XBRL 을 한 번만 받고, 급등일마다 그날까지의 공시로 판정한다."""
    from .dilution import build_profile, financials
    from .motive import motives
    from .screener import edgar as ed
    tick = {str(r["ticker"]).upper(): int(r["cik"]) for r in dil.edgar.ticker_map()}
    dil.max_texts = max_texts
    by_sym: dict[str, list[dict]] = {}
    for r in runners:
        by_sym.setdefault(r["symbol"], []).append(r)
    for k, (sym, rows) in enumerate(sorted(by_sym.items())):
        cik = tick.get(sym)
        if not cik:
            continue
        try:
            subs = dil.edgar.submissions(cik)
        except Exception as e:
            log.debug("submissions 실패 %s: %s", sym, e)
            continue
        try:
            facts = dil.edgar.companyfacts(cik)
        except Exception:
            facts = None
        filings = ed.parse_submissions(subs)
        for r in rows:
            asof = date.fromisoformat(r["d"])
            dil._subs[cik] = (asof, subs)               # 같은 submissions 를 as-of 날짜로 다시 쓴다
            try:
                evs = dil.events(cik, asof)
            except Exception as e:
                log.debug("공시 판정 실패 %s %s: %s", sym, asof, e)
                continue
            fin = financials(facts, asof) if facts else None
            prof = build_profile(evs, asof, None, fin, r["close"], None)
            ms = motives(evs, [f for f in filings if f.filed <= asof], fin, asof, r["close"], r.get("_closes"))
            r["dilution_score"] = prof.score
            r["motives"] = ",".join(m.key for m in ms)
            r["motive_detail"] = " · ".join(m.label for m in ms)
        if (k + 1) % 25 == 0:
            log.info("공시 판정 %d/%d 종목", k + 1, len(by_sym))


# ---------- 5. 요인별 묶기 ----------
def _stats(rows: list[dict]) -> dict:
    """데이마켓 VWAP 매수 기준 (데이마켓 거래가 있던 건만) + 참고용 일봉 기준."""
    out = {"n": len(rows)}
    traded = [r for r in rows if r.get("day_vwap")]
    out["n_day"] = len(traded)
    for key in ("ret_pre", "ret_open", "ret_30m", "next_open_ret"):
        xs = [r[key] for r in (traded if key.startswith("ret_") else rows) if r.get(key) is not None]
        out[key] = {"mean": statistics.mean(xs), "median": statistics.median(xs),
                    "hit": sum(x > 0 for x in xs) / len(xs), "n": len(xs)} if xs else None
    return out


def factor_table(runners: list[dict]) -> list[dict]:
    """(요인, 그룹, 통계). 같은 요인 안에서 그룹끼리 비교한다."""
    groups: list[tuple[str, str, callable]] = [
        ("전체", "급등주 전체", lambda r: True),
        ("데이마켓 갭", "급등일 종가보다 높게", lambda r: (r.get("day_gap") or 0) > 0),
        ("데이마켓 갭", "급등일 종가 이하", lambda r: r.get("day_gap") is not None and r["day_gap"] <= 0),
        ("데이마켓 거래대금", "$5만 이상", lambda r: (r.get("day_dollar") or 0) >= 5e4),
        ("데이마켓 거래대금", "$5만 미만", lambda r: r.get("day_dollar") is not None and r["day_dollar"] < 5e4),
        ("공매도 비율", "50% 이상", lambda r: (r.get("short_ratio") or 0) >= 0.5),
        ("공매도 비율", "50% 미만", lambda r: r.get("short_ratio") is not None and r["short_ratio"] < 0.5),
        ("마감 위치", "고가권 (상위 30%)", lambda r: (r.get("close_loc") or 0) >= 0.7),
        ("마감 위치", "밀려서 마감", lambda r: r.get("close_loc") is not None and r["close_loc"] < 0.7),
        ("거래량", "평소의 10배 이상", lambda r: (r.get("vol_ratio") or 0) >= 10),
        ("거래량", "10배 미만", lambda r: r.get("vol_ratio") is not None and r["vol_ratio"] < 10),
        ("희석 위험", "낮음 (<35)", lambda r: r.get("dilution_score") is not None and r["dilution_score"] < 35),
        ("희석 위험", "높음 (≥60)", lambda r: (r.get("dilution_score") or 0) >= 60),
        ("부양 동기", "하나 이상", lambda r: bool(r.get("motives"))),
        ("부양 동기", "없음", lambda r: r.get("motives") == ""),
    ]
    for key, label in (("delisting", "상폐 기한"), ("below_1", "$1 미만 지속"), ("atm", "ATM 진행 중"),
                       ("offering", "공모 대기"), ("warrants", "워런트 행사가 근처"), ("cash", "현금 6개월 미만"),
                       ("reverse_split", "액면병합"), ("meeting", "주총 예정"), ("periodic", "정기보고서 기한 임박")):
        groups.append(("부양 동기별", label, lambda r, k=key: k in (r.get("motives") or "").split(",")))
    return [{"factor": f, "group": g, **_stats([r for r in runners if fn(r)])} for f, g, fn in groups]


def run(cfg, days: int = 180, edgar: bool = True, max_texts: int = 15, min_pct: float = 30.0,
        rate_share: float | None = None, should_stop=None) -> dict | None:
    """rate_share: 기본 0.3(수동 1회 실행 — 429 는 재시도로 버틴다). 자동 실행(예: 휴장 중 주기 갱신)은
    설정된 '기타' 몫을 넘지 않도록 호출 쪽에서 낮춰서 넘긴다.
    should_stop: 주기적으로 불러 True 면 중단 (예: 장이 다시 열림). 이미 받은 만큼은 캐시에 남아 다음 번에 이어받는다."""
    from .__main__ import _fundamentals, make_client
    from .shortdata import ShortData
    client = make_client(cfg, "other")
    client.rate_share = rate_share if rate_share is not None else 0.3
    rdb = ResearchDB(Path(cfg.state_dir) / "research.db")
    start = date.today() - timedelta(days=days)

    listed = {}
    for m in cfg.collector.markets:
        for s in client.stocks_all(m) or []:
            listed[s["symbol"]] = m
    prices = {p["symbol"]: float(p["lastPrice"]) for p in client.prices(sorted(listed))}
    symbols = sorted(s for s, p in prices.items() if p <= 15)
    log.info("과거 검증: %d일, 대상 %d종목 (상장 %d 중 현재가 $15 이하)", days, len(symbols), len(listed))
    backfill_daily(client, rdb, symbols, days)
    log.info("일봉 완료")
    if should_stop and should_stop():
        log.info("과거 검증 중단 (장 재개) — 받은 일봉은 캐시에 남아 다음 번에 이어받음")
        return None
    n = backfill_finra(ShortData(cfg.sec_user_agent or "pennybot/1.0"), rdb, start - timedelta(days=10), date.today(),
                       set(symbols))
    log.info("FINRA 공매도 %d일치 추가", n)
    if should_stop and should_stop():
        log.info("과거 검증 중단 (장 재개) — FINRA 까지 캐시에 남아 다음 번에 이어받음")
        return None
    runners = find_runners(rdb, start, min_pct)
    log.info("급등일 %d건 (+%.0f%% 이상)", len(runners), min_pct)
    add_short(rdb, runners)
    add_day_market(client, rdb, runners)
    log.info("데이마켓 재현 완료")
    if should_stop and should_stop():
        log.info("과거 검증 중단 (장 재개) — 데이마켓까지 캐시에 남아 다음 번에 이어받음")
        return None
    if edgar:
        dil, _ = _fundamentals(cfg, client)
        if dil:
            add_filings(dil, runners, max_texts)
    for r in runners:
        r.setdefault("dilution_score", None)
        r.setdefault("motives", None)
        r.setdefault("motive_detail", None)
    rdb.put("hist_runners", [{k: v for k, v in r.items() if not k.startswith("_") and k != "next_d"} for r in runners])
    rep = {"created": datetime.now(timezone.utc).isoformat(timespec="seconds"), "days": days, "start": start.isoformat(),
           "end": date.today().isoformat(), "min_pct": min_pct, "symbols": len(symbols), "runners": len(runners),
           "edgar": edgar, "factors": factor_table(runners),
           "caveats": ["매수가 = 데이마켓 거래량 가중 평균가(VWAP): 실제 지정가 체결은 이보다 불리할 수 있음",
                       "지금 상장된 종목만: 그사이 상폐된 종목이 빠져 결과가 실제보다 좋게 나옴",
                       "대차 수수료는 과거 자료가 없어 제외 (수집기가 쌓는 중)", "수수료·스프레드 미반영",
                       "희석 점수·부양 동기는 급등일까지 제출된 공시만 사용 (미래 정보 없음)"],
           "top": sorted(({k: v for k, v in r.items() if not k.startswith("_")} for r in runners),
                         key=lambda r: r["d"], reverse=True)[:200]}
    out = Path(cfg.state_dir) / "research" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log.info("결과: %s", out / "report.json")
    return rep


def format_report(rep: dict) -> str:
    cell = lambda s: f"{s['mean']:+7.1f}% {s['hit'] * 100:3.0f}%" if s else f"{'-':>12}"
    lines = [f"과거 검증 {rep['start']} ~ {rep['end']} · 급등일 {rep['runners']}건 (+{rep['min_pct']:.0f}%↑, {rep['symbols']}종목)",
             "데이마켓 VWAP 에 사서 → 평균 수익률 · 오른 비율",
             f"{'요인':<12} {'그룹':<18} {'건수':>5} {'데이 거래':>6}  {'→프리 마지막':>12}  {'→정규장 시가':>12}  {'→개장 30분':>12}"]
    for f in rep["factors"]:
        if not f["n"]:
            continue
        lines.append(f"{f['factor']:<12} {f['group']:<18} {f['n']:>5} {f['n_day']:>6}  {cell(f['ret_pre'])}  "
                     f"{cell(f['ret_open'])}  {cell(f['ret_30m'])}")
    lines += ["", "주의: " + " / ".join(rep["caveats"])]
    return "\n".join(lines)
