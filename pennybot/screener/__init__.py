"""Coil & Catalyst 스크리너 파이프라인.

1) 유니버스: 미 거래소 상장 전 종목 (OTC 제외)
2) 일봉 → 응축 팩터 → 하드필터 → 후보군 내 백분위 점수
3) 상위 후보만 SEC 공시 조회 → 촉매 가점 / 희석 감점 / 차단
4) 최종 점수 상위 N개를 state/watchlist.json 에 저장 (피벗 가격 포함)
   전체 후보 팩터는 state/screens/날짜.csv 에 기록 → 나중에 내 알고리즘 성과 검증용
"""
from __future__ import annotations

import csv
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from ..config import ScreenerConfig
from ..models import Bar
from . import edgar as ed
from .factors import CoilFeatures, compute
from .scorer import coil_scores, hard_filter

log = logging.getLogger(__name__)

US_EXCHANGES = {"NASDAQ", "NYSE", "NYSE AMERICAN", "NYSE ARCA", "CBOE", "AMEX"}


@dataclass
class Pick:
    symbol: str
    score: float
    coil: float
    catalyst: float
    pivot: float
    price: float
    avg_volume: float = 0.0   # 20일 평균 거래량 (장중 거래량 페이스 계산용)
    reasons: list[str] = field(default_factory=list)


class DailyBarCache:
    def __init__(self, feed, root: Path):
        self.feed = feed
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def get(self, symbol: str, today: date, days: int = 180) -> list[Bar]:
        p = self.root / f"{symbol}.json"
        if p.exists():
            j = json.loads(p.read_text())
            if j.get("fetched") == today.isoformat():
                return [Bar(datetime.fromisoformat(r[0]), *r[1:]) for r in j["bars"]]
        bars = self.feed.get_daily_bars(symbol, days)
        p.write_text(json.dumps({"fetched": today.isoformat(), "bars": [
            [b.ts.isoformat(), b.open, b.high, b.low, b.close, b.volume] for b in bars]}))
        return bars


def load_universe(cfg: ScreenerConfig, ticker_rows: list[dict] | None) -> list[str]:
    if cfg.universe_file and Path(cfg.universe_file).exists():
        syms = [l.strip().upper() for l in Path(cfg.universe_file).read_text().splitlines()]
        return [s for s in syms if s and not s.startswith("#")]
    if not ticker_rows:
        raise SystemExit("유니버스가 없습니다: screener.universe_file 을 지정하거나 SEC_USER_AGENT 를 설정하세요.")
    out = []
    for r in ticker_rows:
        t, ex = str(r.get("ticker", "")), str(r.get("exchange") or "").upper()
        # 우선주/워런트/유닛(특수문자 포함) 제외
        if ex in US_EXCHANGES and t.isalpha():
            out.append(t.upper())
    return sorted(set(out))


def run_screen(cfg: ScreenerConfig, feed, edgar_client: ed.EdgarClient | None,
               state_dir: str = "state", today: date | None = None) -> list[Pick]:
    today = today or date.today()
    fcfg = cfg.filter_dict()
    ticker_rows = edgar_client.ticker_map() if edgar_client else None
    cik_of = {str(r["ticker"]).upper(): int(r["cik"]) for r in (ticker_rows or [])}
    universe = load_universe(cfg, ticker_rows)
    log.info("유니버스 %d종목", len(universe))

    cache = DailyBarCache(feed, Path(state_dir) / "cache" / "daily")

    def featurize(sym: str) -> tuple[str, CoilFeatures | None, str | None]:
        try:
            f = compute(cache.get(sym, today))
        except Exception as e:  # 개별 종목 실패는 건너뜀
            return sym, None, f"데이터 오류 {type(e).__name__}"
        if f is None:
            return sym, None, "봉 부족"
        return sym, f, hard_filter(f, fcfg)

    with ThreadPoolExecutor(max_workers=cfg.workers) as ex:
        results = list(ex.map(featurize, universe))

    passed = [(s, f) for s, f, why in results if f is not None and why is None]
    log.info("하드필터 통과 %d / %d", len(passed), len(universe))
    if not passed:
        return []

    coils = coil_scores([f for _, f in passed], cfg.weights)
    ranked = sorted(zip(passed, coils), key=lambda x: x[1], reverse=True)

    picks: list[Pick] = []
    rows = []
    for (sym, f), coil in ranked[:cfg.catalyst_top_k]:
        rep = ed.CatalystReport()
        if edgar_client and sym in cik_of:
            try:
                cik = cik_of[sym]
                filings = edgar_client.filings(cik)
                buys = sum(ed.insider_buy_usd(edgar_client.form4_xml(cik, f4))
                           for f4 in ed.recent_form4(filings, today))
                rep = ed.evaluate(filings, today, buys, cfg.catalyst)
            except Exception as e:
                log.warning("%s 공시 조회 실패: %s", sym, e)
        p = Pick(sym, round(coil + rep.score, 2), round(coil, 2), rep.score, f.pivot, f.price,
                 f.dollar_vol_20 / f.price if f.price else 0.0, rep.reasons)
        rows.append({"symbol": sym, "score": p.score, "coil": p.coil, "catalyst": rep.score,
                     "blocked": rep.blocked, "reasons": "; ".join(rep.reasons), **f.to_dict()})
        if not rep.blocked:
            picks.append(p)

    picks.sort(key=lambda p: p.score, reverse=True)
    picks = picks[:cfg.top_n]
    _save(picks, rows, Path(state_dir), today)
    return picks


def _save(picks: list[Pick], rows: list[dict], root: Path, today: date) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "watchlist.json").write_text(json.dumps({
        "date": today.isoformat(),
        "picks": [p.__dict__ for p in picks],
    }, ensure_ascii=False, indent=2))
    if rows:
        d = root / "screens"
        d.mkdir(exist_ok=True)
        with open(d / f"{today.isoformat()}.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)


def load_watchlist(state_dir: str, today: date) -> dict[str, dict]:
    """오늘(또는 직전 거래일 밤) 스크린 결과 → {심볼: pick}. 3일 넘게 지난 결과는 무시."""
    p = Path(state_dir) / "watchlist.json"
    if not p.exists():
        return {}
    j = json.loads(p.read_text(encoding="utf-8"))
    if (today - date.fromisoformat(j["date"])).days > 3:
        log.warning("watchlist.json 이 오래됐습니다 (%s). 스크리너를 다시 실행하세요.", j["date"])
        return {}
    return {x["symbol"]: x for x in j["picks"]}
