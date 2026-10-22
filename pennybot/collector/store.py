"""세션별 시장 데이터 저장소 (SQLite).

모든 행에 trade_date(미국 영업일)와 session(DAY/PRE/REGULAR/AFTER) 꼬리표를 붙인다.
시각은 UTC ISO 문자열. 같은 키로 다시 들어오면 덮어쓴다(INSERT OR REPLACE) — 재시작·중복 수집에 안전.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS universe (
    snapshot_date TEXT, symbol TEXT, market TEXT, name TEXT, last REAL,
    PRIMARY KEY (snapshot_date, symbol));
CREATE TABLE IF NOT EXISTS prices (
    symbol TEXT, ts TEXT, trade_date TEXT, session TEXT, last REAL, price_ts TEXT,
    PRIMARY KEY (symbol, ts));
CREATE TABLE IF NOT EXISTS quotes (
    symbol TEXT, ts TEXT, trade_date TEXT, session TEXT,
    bid REAL, bid_size REAL, ask REAL, ask_size REAL, levels INTEGER, book_ts TEXT,
    PRIMARY KEY (symbol, ts));
CREATE TABLE IF NOT EXISTS bars_1m (
    symbol TEXT, ts TEXT, trade_date TEXT, session TEXT,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (symbol, ts));
CREATE TABLE IF NOT EXISTS rankings (
    ts TEXT, trade_date TEXT, session TEXT, type TEXT, duration TEXT, rank INTEGER, symbol TEXT,
    last REAL, base REAL, change_rate REAL, volume REAL, amount REAL, ranked_at TEXT,
    PRIMARY KEY (ts, type, duration, rank));
CREATE TABLE IF NOT EXISTS filings (
    accession TEXT PRIMARY KEY, cik INTEGER, symbol TEXT, form TEXT, items TEXT,
    accepted TEXT, trade_date TEXT, session TEXT, insider_buy_usd REAL);
CREATE TABLE IF NOT EXISTS session_summary (
    symbol TEXT, trade_date TEXT, session TEXT,
    open REAL, high REAL, low REAL, close REAL, volume REAL, vwap REAL,
    first_trade TEXT, last_trade TEXT, bars INTEGER,
    spread_med_pct REAL, quotes INTEGER, quotes_empty INTEGER,
    PRIMARY KEY (symbol, trade_date, session));
CREATE INDEX IF NOT EXISTS ix_bars_day ON bars_1m (trade_date, session);
CREATE INDEX IF NOT EXISTS ix_quotes_day ON quotes (trade_date, session);
CREATE INDEX IF NOT EXISTS ix_prices_day ON prices (trade_date, session);
"""


class Store:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def _put(self, table: str, rows: list[dict]) -> int:
        if not rows:
            return 0
        cols = list(rows[0])
        sql = f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
        with self.db:
            self.db.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
        return len(rows)

    def put_universe(self, rows): return self._put("universe", rows)
    def put_prices(self, rows): return self._put("prices", rows)
    def put_quotes(self, rows): return self._put("quotes", rows)
    def put_bars(self, rows): return self._put("bars_1m", rows)
    def put_rankings(self, rows): return self._put("rankings", rows)
    def put_filings(self, rows): return self._put("filings", rows)

    def query(self, sql: str, args=()) -> list[tuple]:
        return self.db.execute(sql, args).fetchall()

    def latest_bar_ts(self, symbol: str) -> str | None:
        r = self.query("SELECT MAX(ts) FROM bars_1m WHERE symbol=?", (symbol,))
        return r[0][0] if r else None

    def summarize(self, trade_date: str) -> int:
        """1분봉 + 호가 스냅샷 → 세션 요약. 체결이 없는 세션도 호가 기록이 있으면 남긴다."""
        with self.db:
            self.db.execute("DELETE FROM session_summary WHERE trade_date=?", (trade_date,))
            self.db.execute("""
                INSERT INTO session_summary (symbol, trade_date, session, open, high, low, close, volume,
                       vwap, first_trade, last_trade, bars)
                SELECT symbol, trade_date, session,
                       (SELECT open FROM bars_1m b2 WHERE b2.symbol=b.symbol AND b2.trade_date=b.trade_date
                          AND b2.session=b.session ORDER BY ts LIMIT 1),
                       MAX(high), MIN(low),
                       (SELECT close FROM bars_1m b3 WHERE b3.symbol=b.symbol AND b3.trade_date=b.trade_date
                          AND b3.session=b.session ORDER BY ts DESC LIMIT 1),
                       SUM(volume),
                       CASE WHEN SUM(volume) > 0 THEN SUM(close * volume) / SUM(volume) END,
                       MIN(CASE WHEN volume > 0 THEN ts END), MAX(CASE WHEN volume > 0 THEN ts END),
                       COUNT(*)
                FROM bars_1m b WHERE trade_date=? GROUP BY symbol, trade_date, session""", (trade_date,))
            # 호가 통계: 스프레드 중앙값(%)과 빈 호가 횟수
            rows = self.db.execute("""
                SELECT symbol, session, bid, ask FROM quotes WHERE trade_date=?""", (trade_date,)).fetchall()
            stats: dict[tuple, list] = {}
            for sym, sess, bid, ask in rows:
                st = stats.setdefault((sym, sess), [[], 0, 0])
                st[1] += 1
                if bid and ask and bid > 0 and ask >= bid:
                    st[0].append((ask - bid) / ((ask + bid) / 2) * 100)
                else:
                    st[2] += 1
            for (sym, sess), (spreads, n, empty) in stats.items():
                med = sorted(spreads)[len(spreads) // 2] if spreads else None
                self.db.execute("""
                    INSERT INTO session_summary (symbol, trade_date, session, spread_med_pct, quotes, quotes_empty)
                    VALUES (?,?,?,?,?,?)
                    ON CONFLICT(symbol, trade_date, session) DO UPDATE SET
                      spread_med_pct=excluded.spread_med_pct, quotes=excluded.quotes,
                      quotes_empty=excluded.quotes_empty""", (sym, trade_date, sess, med, n, empty))
        return self.query("SELECT COUNT(*) FROM session_summary WHERE trade_date=?", (trade_date,))[0][0]
