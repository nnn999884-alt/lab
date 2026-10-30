"""매매 기록 (SQLite, state/trading.db). 대시보드가 이 파일을 읽는다.

trades  : 한 번의 매수→매도 왕복. 상태 PENDING(매수 주문 중) → OPEN → EXITING → CLOSED / CANCELED
orders  : 거래소에 낸 모든 주문과 체결 진행
signals : 전략이 검토한 모든 후보와 통과/탈락 사유 (나중에 전략 검증용)
equity  : 계좌 평가액 스냅샷
heartbeat: 수집기·매매 봇이 살아 있는지
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT, strategy TEXT, symbol TEXT, mode TEXT, trade_date TEXT,
    status TEXT, planned_qty INTEGER, qty INTEGER DEFAULT 0, remaining INTEGER DEFAULT 0,
    entry_price REAL, entry_ts TEXT, entry_session TEXT, attempt_session TEXT,
    stop REAL, target REAL, sell_from TEXT, deadline TEXT,
    exit_price REAL, exit_ts TEXT, exit_session TEXT, exit_reason TEXT,
    exit_value REAL DEFAULT 0, exited_qty INTEGER DEFAULT 0,
    pnl REAL, pnl_pct REAL, reason TEXT, created TEXT,
    scale_out REAL DEFAULT 1.0, trail_pct REAL DEFAULT 0, high_water REAL, scaled INTEGER DEFAULT 0,
    backstop_id TEXT, backstop_qty INTEGER, backstop_trigger REAL, fees REAL DEFAULT 0, context TEXT);
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY, trade_id INTEGER, ts TEXT, symbol TEXT, side TEXT, type TEXT,
    qty INTEGER, price REAL, status TEXT, filled INTEGER DEFAULT 0, avg_price REAL,
    purpose TEXT, session TEXT, note TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS signals (
    ts TEXT, trade_date TEXT, session TEXT, strategy TEXT, symbol TEXT, ok INTEGER,
    reason TEXT, score REAL, price REAL);
CREATE TABLE IF NOT EXISTS equity (
    ts TEXT PRIMARY KEY, cash REAL, positions_value REAL, equity REAL, day_pnl REAL, mode TEXT);
CREATE TABLE IF NOT EXISTS heartbeat (component TEXT PRIMARY KEY, ts TEXT, info TEXT);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS ix_trades_status ON trades (status);
CREATE INDEX IF NOT EXISTS ix_signals_ts ON signals (ts);
"""

OPEN_STATES = ("PENDING", "OPEN", "EXITING")


def now_iso(ts: datetime | None = None) -> str:
    return (ts or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")


class Journal:
    def __init__(self, path: str | Path, mode: str = "paper"):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = Path(path)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        have = {r[1] for r in self.db.execute("PRAGMA table_info(trades)")}
        for col, typ in (("scale_out", "REAL DEFAULT 1.0"), ("trail_pct", "REAL DEFAULT 0"),
                         ("high_water", "REAL"), ("scaled", "INTEGER DEFAULT 0"), ("backstop_id", "TEXT"),
                         ("backstop_qty", "INTEGER"), ("backstop_trigger", "REAL"), ("fees", "REAL DEFAULT 0"),
                         ("attempt_session", "TEXT"), ("context", "TEXT"), ("no_rebound_at", "TEXT")):
            if col not in have:        # 예전 DB 이전
                self.db.execute(f"ALTER TABLE trades ADD COLUMN {col} {typ}")
        self.mode = mode

    def _x(self, sql: str, args=()) -> sqlite3.Cursor:
        with self.db:
            return self.db.execute(sql, args)

    def q(self, sql: str, args=()) -> list[dict]:
        return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    # ---------- trades ----------
    def open_trade(self, *, strategy, symbol, trade_date, planned_qty, stop, target,
                   sell_from: datetime, deadline: datetime, reason: str, now: datetime,
                   scale_out: float = 1.0, trail_pct: float = 0.0, session: str | None = None,
                   context: dict | None = None) -> int:
        cur = self._x("""INSERT INTO trades (strategy, symbol, mode, trade_date, status, planned_qty,
                         stop, target, sell_from, deadline, reason, created, scale_out, trail_pct, attempt_session, context)
                         VALUES (?,?,?,?, 'PENDING', ?,?,?,?,?,?,?,?,?,?,?)""",
                      (strategy, symbol, self.mode, trade_date, planned_qty, stop, target,
                       now_iso(sell_from), now_iso(deadline), reason, now_iso(now), scale_out, trail_pct, session,
                       json.dumps(context, ensure_ascii=False, default=str) if context else None))
        return cur.lastrowid

    def trade(self, trade_id: int) -> dict:
        return self.q("SELECT * FROM trades WHERE id=?", (trade_id,))[0]

    def open_trades(self) -> list[dict]:
        return self.q(f"SELECT * FROM trades WHERE status IN {OPEN_STATES} AND mode=? ORDER BY id",
                      (self.mode,))

    def record_entry_fill(self, trade_id: int, qty: int, price: float, session: str, now: datetime,
                          fee: float = 0.0) -> None:
        t = self.trade(trade_id)
        new_qty = t["qty"] + qty
        avg = ((t["entry_price"] or 0) * t["qty"] + price * qty) / new_qty
        self._x("""UPDATE trades SET qty=?, remaining=remaining+?, entry_price=?, status='OPEN', fees=fees+?,
                   entry_ts=COALESCE(entry_ts, ?), entry_session=COALESCE(entry_session, ?) WHERE id=?""",
                (new_qty, qty, avg, fee, now_iso(now), session, trade_id))

    def entry_finished(self, trade_id: int) -> None:
        """매수 주문이 끝났는데 체결이 0이면 취소 처리."""
        self._x("UPDATE trades SET status='CANCELED' WHERE id=? AND qty=0", (trade_id,))

    def set_exiting(self, trade_id: int, reason: str) -> None:
        """사유는 쌓는다 (분할 익절 후 트레일링 청산 → '익절(분할) + 트레일링 스톱')."""
        self._x("""UPDATE trades SET status='EXITING', exit_reason=CASE
                   WHEN exit_reason IS NULL THEN ? WHEN instr(exit_reason, ?) > 0 THEN exit_reason
                   ELSE exit_reason || ' + ' || ? END WHERE id=?""", (reason, reason, reason, trade_id))

    def set_backstop(self, trade_id: int, stop_id: str | None, qty: int | None = None,
                     trigger: float | None = None) -> None:
        """서버 손절 안전망(토스 조건주문) 기록. None = 없음."""
        self._x("UPDATE trades SET backstop_id=?, backstop_qty=?, backstop_trigger=? WHERE id=?",
                (stop_id, qty, trigger, trade_id))

    def update_high(self, trade_id: int, price: float) -> None:
        self._x("UPDATE trades SET high_water=? WHERE id=? AND (high_water IS NULL OR high_water < ?)",
                (price, trade_id, price))

    def set_no_rebound(self, trade_id: int, ts: datetime) -> None:
        """3시간 규칙 판정 시각 (체결 시각 + no_rebound_minutes). 이미 있으면 그대로 둔다."""
        self._x("UPDATE trades SET no_rebound_at=? WHERE id=? AND no_rebound_at IS NULL",
                (now_iso(ts), trade_id))

    def set_scaled(self, trade_id: int, scaled: bool, stop: float | None = None) -> None:
        """분할 익절 표시. 나머지 물량의 손절가는 본전 이상으로 올린다."""
        self._x("UPDATE trades SET scaled=?, stop=MAX(stop, COALESCE(?, stop)) WHERE id=?",
                (int(scaled), stop, trade_id))

    def record_exit_fill(self, trade_id: int, qty: int, price: float, session: str, now: datetime,
                         fee: float = 0.0) -> dict:
        """pnl·pnl_pct 는 수수료를 뺀 순손익 (fees 에 매수·매도 수수료 합계)."""
        t = self.trade(trade_id)
        remaining = max(t["remaining"] - qty, 0)
        exited = t["exited_qty"] + qty
        value = t["exit_value"] + qty * price
        fees = (t["fees"] or 0) + fee
        self._x("UPDATE trades SET remaining=?, exited_qty=?, exit_value=?, fees=? WHERE id=?",
                (remaining, exited, value, fees, trade_id))
        if remaining == 0:
            exit_price = value / exited
            pnl = (exit_price - t["entry_price"]) * exited - fees
            self._x("""UPDATE trades SET status='CLOSED', exit_price=?, exit_ts=?, exit_session=?,
                       pnl=?, pnl_pct=? WHERE id=?""",
                    (exit_price, now_iso(now), session, pnl,
                     pnl / (t["entry_price"] * exited) * 100, trade_id))
        return self.trade(trade_id)

    def exit_unfilled(self, trade_id: int) -> None:
        """매도 주문이 안 팔리고 끝나면 다시 OPEN 으로 (다음 틱에 재주문)."""
        self._x("UPDATE trades SET status='OPEN' WHERE id=? AND status='EXITING' AND remaining>0",
                (trade_id,))

    def realized_pnl(self, trade_date: str) -> float:
        """끝난 매매 손익 + 아직 열린 매매에서 이미 판 부분 (분할 익절 등)."""
        r = self.q("""SELECT COALESCE(SUM(CASE WHEN status='CLOSED' THEN pnl
                                          ELSE exit_value - entry_price * exited_qty END), 0) AS p
                      FROM trades WHERE trade_date=? AND mode=? AND (status='CLOSED' OR exited_qty > 0)""",
                   (trade_date, self.mode))
        return r[0]["p"]

    def traded_today(self, symbol: str, trade_date: str) -> bool:
        return bool(self.q("SELECT 1 FROM trades WHERE symbol=? AND trade_date=? AND mode=? AND qty>0",
                           (symbol, trade_date, self.mode)))

    def unfilled_entries_today(self, symbol: str, trade_date: str) -> int:
        """오늘 이 종목에 건 매수가 한 번도 안 붙고(qty=0) 취소된 횟수 — 호가가 얇아 체결이 안 붙는 종목 거르는 데 씀."""
        r = self.q("SELECT COUNT(*) AS n FROM trades WHERE symbol=? AND trade_date=? AND mode=? "
                   "AND status='CANCELED' AND qty=0", (symbol, trade_date, self.mode))
        return r[0]["n"] if r else 0

    def strategies_today(self, symbol: str, trade_date: str) -> set[str]:
        rows = self.q("SELECT DISTINCT strategy FROM trades WHERE symbol=? AND trade_date=? AND mode=?",
                      (symbol, trade_date, self.mode))
        return {r["strategy"] for r in rows}

    def strategy_stats(self, strategy: str | None = None, last_n: int | None = None) -> list[dict]:
        rows = self.q("""SELECT strategy, pnl, pnl_pct FROM trades WHERE status='CLOSED' AND mode=?
                         ORDER BY id DESC""", (self.mode,))
        out: dict[str, list] = {}
        for r in rows:
            if strategy and r["strategy"] != strategy:
                continue
            lst = out.setdefault(r["strategy"], [])
            if last_n is None or len(lst) < last_n:
                lst.append(r)
        stats = []
        for name, lst in out.items():
            n = len(lst)
            wins = sum(1 for r in lst if r["pnl"] > 0)
            stats.append({"strategy": name, "trades": n, "win_rate": wins / n if n else 0,
                          "pnl": sum(r["pnl"] for r in lst),
                          "expectancy": sum(r["pnl"] for r in lst) / n if n else 0,
                          "avg_pct": sum(r["pnl_pct"] for r in lst) / n if n else 0})
        return stats

    # ---------- orders ----------
    def log_order(self, o: dict, *, trade_id: int, purpose: str, session: str, now: datetime, note: str = "") -> None:
        self._x("""INSERT OR REPLACE INTO orders (order_id, trade_id, ts, symbol, side, type, qty, price,
                   status, filled, avg_price, purpose, session, note, updated)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (o["order_id"], trade_id, now_iso(now), o["symbol"], o["side"], o["type"], o["qty"],
                 o.get("price"), o["status"], o.get("filled", 0), o.get("avg_price"), purpose, session,
                 note, now_iso(now)))

    def update_order(self, o: dict, now: datetime) -> None:
        self._x("UPDATE orders SET status=?, filled=?, avg_price=?, updated=? WHERE order_id=?",
                (o["status"], o.get("filled", 0), o.get("avg_price"), now_iso(now), o["order_id"]))

    # ---------- signals / equity / heartbeat ----------
    def log_signals(self, rows: list[dict]) -> None:
        if rows:
            with self.db:
                self.db.executemany("""INSERT INTO signals (ts, trade_date, session, strategy, symbol, ok,
                                       reason, score, price) VALUES (:ts,:trade_date,:session,:strategy,
                                       :symbol,:ok,:reason,:score,:price)""", rows)

    def snapshot_equity(self, cash: float, positions_value: float, day_pnl: float, now: datetime) -> None:
        self._x("INSERT OR REPLACE INTO equity VALUES (?,?,?,?,?,?)",
                (now_iso(now), cash, positions_value, cash + positions_value, day_pnl, self.mode))

    def get_meta(self, key: str) -> str | None:
        r = self.q("SELECT value FROM meta WHERE key=?", (f"{self.mode}:{key}",))
        return r[0]["value"] if r else None

    def set_meta(self, key: str, value: str) -> None:
        self._x("INSERT OR REPLACE INTO meta VALUES (?,?)", (f"{self.mode}:{key}", value))

    def beat(self, component: str, info: dict, now: datetime) -> None:
        self._x("INSERT OR REPLACE INTO heartbeat VALUES (?,?,?)",
                (component, now_iso(now), json.dumps(info, ensure_ascii=False, default=str)))
