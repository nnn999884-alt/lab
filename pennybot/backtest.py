"""리플레이 백테스터: 수집기 DB(state/market.db)를 시간 순서대로 다시 흘려 실제 매매 엔진(Trader)을 그대로 돌린다.

- 재생 시각(clock)까지의 데이터만 보인다: MarketView 의 공시·뉴스·가격 흐름·랭킹 조회가 모두 as-of 로 잘린다.
- 체결: 그 시각 직전의 수집 호가(quotes)에 대해 모의 거래소와 같은 규칙으로. 호가 기록이 없으면
  현재가 ± 가정 스프레드, 잔량은 assumed_size 주로 보수적으로 둔다.
- 수수료(fee_pct, 기본 0.1%)는 모의 계좌 현금과 보고서 순손익 모두에 반영.
- 보고서: 전략별 / 학습 구간(앞 70%) · 검증 구간(뒤 30%) 으로 나눠 과최적화를 확인한다.

주의: 수집기가 실제로 돌았던 날만 의미가 있다. 데이마켓 1분봉·호가가 비어 있으면 그 시간대는 체결이 안 된다.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

from .sessions import CLOSED, ET, REGULAR, SessionCalendar, TradingDay, static_day
from .trading.control import Control
from .trading.exchange import Book, PaperExchange
from .trading.journal import Journal
from .trading.market import MarketView, _utc
from .trading.trader import Trader

log = logging.getLogger(__name__)


class _DB:
    def __init__(self, path: str):
        self.db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)

    def q(self, sql: str, args=()) -> list[tuple]:
        try:
            return self.db.execute(sql, args).fetchall()
        except sqlite3.OperationalError:
            return []


class ReplayMarket(_DB):
    """재생 시각 기준 호가. 수집 호가 → 현재가 ± 가정 스프레드 → 호가 없음."""

    def __init__(self, path: str, clock, stale_seconds: int = 180, half_spread_pct: float = 0.75,
                 assumed_size: float = 1000):
        super().__init__(path)
        self.clock, self.stale = clock, stale_seconds
        self.half = half_spread_pct / 100
        self.size = assumed_size

    def _fresh(self, ts: str, now: datetime) -> bool:
        return (now - datetime.fromisoformat(ts)).total_seconds() <= self.stale

    def book(self, symbol: str) -> Book:
        now = self.clock()
        asof = _utc(now)
        last = 0.0
        p = self.q("SELECT last, ts FROM prices WHERE symbol=? AND ts<=? ORDER BY ts DESC LIMIT 1", (symbol, asof))
        if p and p[0][0]:
            last = float(p[0][0])
        q = self.q("""SELECT bid, bid_size, ask, ask_size, ts FROM quotes WHERE symbol=? AND ts<=?
                      ORDER BY ts DESC LIMIT 1""", (symbol, asof))
        if q and self._fresh(q[0][4], now) and q[0][0] and q[0][2]:
            bid, bs, ask, as_, _ = q[0]
            return Book(symbol, float(bid), float(ask), float(bs or 0) or self.size, float(as_ or 0) or self.size,
                        last or (bid + ask) / 2)
        if p and last and self._fresh(p[0][1], now):
            return Book(symbol, last * (1 - self.half), last * (1 + self.half), self.size, self.size, last)
        return Book(symbol, 0, 0, 0, 0, last)


class ReplayClient(_DB):
    """MarketView·Fundamentals 가 토스 API 대신 쓰는 것: 일봉은 정규장 세션 요약으로 만든다 (재생일 이전만)."""

    def __init__(self, path: str, cal: SessionCalendar, clock):
        super().__init__(path)
        self.cal, self.clock = cal, clock

    def candles(self, symbol, interval="1d", count=10, before=None):
        td, _ = self.cal.classify(self.clock())
        today = (td or self.clock().astimezone(ET).date()).isoformat()
        rows = self.q("""SELECT trade_date, open, high, low, close, volume FROM session_summary
                         WHERE symbol=? AND session=? AND trade_date < ? ORDER BY trade_date DESC LIMIT ?""",
                      (symbol, REGULAR, today, count))
        return {"candles": [{"timestamp": f"{d}T00:00:00-04:00", "openPrice": o, "highPrice": h, "lowPrice": lo,
                             "closePrice": c, "volume": v or 0} for d, o, h, lo, c, v in rows], "nextBefore": None}

    def stocks(self, symbols):
        return []


def replay_calendar(db: _DB, start: date, end: date) -> SessionCalendar:
    """데이터가 있는 평일만 영업일로 (휴장일 자동 반영)."""
    have = {d for (d,) in db.q("SELECT DISTINCT trade_date FROM prices WHERE trade_date BETWEEN ? AND ?",
                               ((start - timedelta(days=10)).isoformat(), (end + timedelta(days=5)).isoformat()))}
    cal = SessionCalendar()
    d = start - timedelta(days=10)
    while d <= end + timedelta(days=5):
        cal.add(static_day(d) if (d.isoformat() in have or not have) else TradingDay(d))
        d += timedelta(days=1)
    return cal


def apply_params(tcfg, params: list[str]) -> dict:
    """'runner_overnight.take_profit_pct=15', 'max_open_positions=4' 형식."""
    applied = {}
    for p in params or []:
        k, _, v = p.partition("=")
        val = yaml.safe_load(v)
        if "." in k:
            strat, key = k.split(".", 1)
            if strat not in tcfg.strategies:
                raise SystemExit(f"알 수 없는 전략: {strat}")
            tcfg.strategies[strat][key] = val
        else:
            if not hasattr(tcfg, k):
                raise SystemExit(f"알 수 없는 설정: {k}")
            setattr(tcfg, k, val)
        applied[k] = val
    return applied


def _stats(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {"trades": 0, "win_rate": 0, "net_pnl": 0, "avg_pct": 0, "expectancy": 0, "max_drawdown": 0}
    cum = peak = dd = 0.0
    for r in rows:
        cum += r["net"]
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return {"trades": n, "win_rate": sum(1 for r in rows if r["net"] > 0) / n,
            "net_pnl": sum(r["net"] for r in rows), "avg_pct": sum(r["pnl_pct"] for r in rows) / n,
            "expectancy": sum(r["net"] for r in rows) / n, "max_drawdown": dd}


def report(journal: Journal, fee_pct: float, dates: list[str], train_frac: float) -> dict:
    fee = fee_pct / 100
    rows = journal.q("""SELECT id, strategy, symbol, trade_date, qty, entry_price, exit_value, pnl, pnl_pct,
                        exit_reason, entry_session, exit_session FROM trades WHERE status='CLOSED' ORDER BY id""")
    for r in rows:
        r["net"] = r["pnl"] - fee * (r["entry_price"] * r["qty"] + r["exit_value"])
    cut = dates[min(int(len(dates) * train_frac), len(dates) - 1)] if dates else None
    train = [r for r in rows if cut is None or r["trade_date"] < cut]
    test = [r for r in rows if cut is not None and r["trade_date"] >= cut]
    by = {}
    for name in sorted({r["strategy"] for r in rows}):
        by[name] = {"all": _stats([r for r in rows if r["strategy"] == name]),
                    "train": _stats([r for r in train if r["strategy"] == name]),
                    "test": _stats([r for r in test if r["strategy"] == name])}
    reasons: dict[str, int] = {}
    for r in rows:
        k = (r["exit_reason"] or "").split(" (")[0]
        reasons[k] = reasons.get(k, 0) + 1
    return {"all": _stats(rows), "train": _stats(train), "test": _stats(test), "split_date": cut,
            "by_strategy": by, "exit_reasons": reasons,
            "trades": [{k: r[k] for k in ("strategy", "symbol", "trade_date", "qty", "entry_price", "net",
                                          "pnl_pct", "exit_reason", "exit_session")} for r in rows]}


@dataclass
class Backtest:
    db_path: str
    tcfg: object
    start: date
    end: date
    out_dir: Path
    step_seconds: int = 60
    fee_pct: float = 0.1
    half_spread_pct: float = 0.75
    assumed_size: float = 1000
    train_frac: float = 0.7
    fund: object = None
    params: dict = field(default_factory=dict)

    def run(self) -> dict:
        out = Path(self.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        for f in ("backtest.db", "backtest.db-wal", "backtest.db-shm", "paper.json", "control.json"):
            (out / f).unlink(missing_ok=True)
        clock = [datetime.combine(self.start, datetime.min.time(), tzinfo=ET)]
        now_fn = lambda: clock[0]
        db = _DB(self.db_path)
        cal = replay_calendar(db, self.start, self.end)
        market = ReplayMarket(self.db_path, now_fn, half_spread_pct=self.half_spread_pct,
                              assumed_size=self.assumed_size)
        ex = PaperExchange(market, self.tcfg.paper_cash, out / "paper.json", fee_pct=self.fee_pct)
        mv = MarketView(self.db_path, ReplayClient(self.db_path, cal, now_fn), cal, ex, clock=now_fn)
        journal = Journal(out / "backtest.db", mode="backtest")
        tr = Trader(ex, journal, cal, mv, self.tcfg, Control(out), clock=now_fn, fund=self.fund)
        tr.snapshot = lambda now, td: journal.snapshot_equity(ex.cash(), sum(
            q * (market.book(s).bid or a) for s, (q, a) in ex.positions().items()),
            journal.realized_pnl(str(td)) if td else 0.0, now)

        first = cal.days.get(self.start)
        clock[0] = first.start if first and first.start else clock[0]
        last = cal.days.get(self.end)
        end_ts = last.end if last and last.end else datetime.combine(self.end, datetime.max.time(), tzinfo=ET)
        step = timedelta(seconds=self.step_seconds)
        ticks = 0
        while clock[0] <= end_ts:
            tr.tick()
            ticks += 1
            _, session = cal.classify(clock[0])
            nxt = cal.next_change(clock[0]) if session == CLOSED and not tr.working else None
            clock[0] = max(nxt, clock[0] + step) if nxt else clock[0] + step
        # 끝까지 못 판 물량은 마지막 가격으로 평가만 (실제로 팔리지 않았으므로 보고서에 '미청산'으로)
        open_left = [{"symbol": t["symbol"], "remaining": t["remaining"], "strategy": t["strategy"]}
                     for t in journal.open_trades()]
        dates = [d for (d,) in db.q("""SELECT DISTINCT trade_date FROM prices WHERE trade_date BETWEEN ? AND ?
                                        ORDER BY 1""", (self.start.isoformat(), self.end.isoformat()))]
        rep = report(journal, self.fee_pct, dates, self.train_frac)
        rep.update({"created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "start": self.start.isoformat(), "end": self.end.isoformat(), "days": len(dates),
                    "ticks": ticks, "params": self.params, "fee_pct": self.fee_pct,
                    "half_spread_pct": self.half_spread_pct, "end_cash": ex.cash(), "open_left": open_left,
                    "start_cash": self.tcfg.paper_cash})
        (out / "report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1, default=str))
        return rep


def format_report(rep: dict) -> str:
    def line(name, s):
        return (f"{name:<20} {s['trades']:>4}건  승률 {s['win_rate'] * 100:5.1f}%  순손익 {s['net_pnl']:+9.2f}$  "
                f"평균 {s['avg_pct']:+5.1f}%  최대낙폭 {s['max_drawdown']:.2f}$")
    out = [f"백테스트 {rep['start']} ~ {rep['end']} ({rep['days']}일, 학습/검증 경계 {rep['split_date']})",
           line("전체", rep["all"]), line("  학습 구간", rep["train"]), line("  검증 구간", rep["test"])]
    for name, s in rep["by_strategy"].items():
        out += [line(name, s["all"]), line("  검증 구간", s["test"])]
    out.append("청산 사유: " + ", ".join(f"{k} {v}" for k, v in rep["exit_reasons"].items()))
    if rep["open_left"]:
        out.append(f"⚠️ 미청산 {len(rep['open_left'])}건 (기간 끝까지 못 팖): " +
                   ", ".join(t["symbol"] for t in rep["open_left"]))
    if rep["all"]["trades"] < 30:
        out.append("⚠️ 거래 수가 30건 미만이라 통계적으로 의미가 약합니다. 수집 기간을 더 늘리세요.")
    return "\n".join(out)
