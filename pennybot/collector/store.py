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
CREATE TABLE IF NOT EXISTS runners (
    trade_date TEXT, symbol TEXT, prev_close REAL, close REAL, change_pct REAL,
    high REAL, low REAL, volume REAL, close_loc REAL, shares REAL, rotation REAL,
    dilution_score REAL, dilution_bucket TEXT, dilution_reasons TEXT, pump_flags TEXT,
    PRIMARY KEY (trade_date, symbol));
CREATE TABLE IF NOT EXISTS news (
    id TEXT, symbol TEXT, source TEXT, title TEXT, url TEXT, published TEXT,
    trade_date TEXT, session TEXT, events TEXT, direction INTEGER,
    PRIMARY KEY (id, symbol));
CREATE TABLE IF NOT EXISTS short_volume (
    trade_date TEXT, symbol TEXT, short_volume REAL, exempt_volume REAL, total_volume REAL,
    PRIMARY KEY (trade_date, symbol));
CREATE TABLE IF NOT EXISTS borrow (
    ts TEXT, symbol TEXT, fee_rate REAL, rebate_rate REAL, available REAL,
    PRIMARY KEY (ts, symbol));
CREATE TABLE IF NOT EXISTS ssr (trade_date TEXT, symbol TEXT, trigger_time TEXT, PRIMARY KEY (trade_date, symbol));
CREATE TABLE IF NOT EXISTS reg_sho (trade_date TEXT, symbol TEXT, PRIMARY KEY (trade_date, symbol));
CREATE TABLE IF NOT EXISTS halts (
    symbol TEXT, halt_ts TEXT, reason TEXT, reason_ko TEXT, resume_ts TEXT,
    PRIMARY KEY (symbol, halt_ts));
CREATE INDEX IF NOT EXISTS ix_news_sym ON news (symbol, published);
CREATE INDEX IF NOT EXISTS ix_borrow_sym ON borrow (symbol, ts);
CREATE INDEX IF NOT EXISTS ix_shortvol_sym ON short_volume (symbol, trade_date);
CREATE INDEX IF NOT EXISTS ix_bars_day ON bars_1m (trade_date, session);
CREATE INDEX IF NOT EXISTS ix_quotes_day ON quotes (trade_date, session);
CREATE INDEX IF NOT EXISTS ix_prices_day ON prices (trade_date, session);
"""


# 예전 DB 에 나중에 추가된 컬럼
MIGRATIONS = [
    ("filings", "events", "TEXT"),        # 희석 이벤트 (쉼표 구분)
    ("filings", "direction", "INTEGER"),  # +1 호재 / −1 악재 / 0
    ("filings", "amount", "REAL"),        # 공모 금액(달러)
]



def ro_uri(path) -> str:
    """읽기 전용 SQLite URI. 윈도우 드라이브 경로, 한글·공백·# 이 들어간 경로도 안전하게."""
    return Path(path).resolve().as_uri() + "?mode=ro"

class Store:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        for table, col, typ in MIGRATIONS:
            cols = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        self.db.commit()

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
    def put_runners(self, rows): return self._put("runners", rows)
    def put_news(self, rows): return self._put("news", rows)
    def put_short_volume(self, rows): return self._put("short_volume", rows)
    def put_borrow(self, rows): return self._put("borrow", rows)
    def put_ssr(self, rows): return self._put("ssr", rows)
    def put_reg_sho(self, rows): return self._put("reg_sho", rows)
    def put_halts(self, rows): return self._put("halts", rows)

    def runner_candidates(self, trade_date: str, prev_date: str, min_pct: float) -> list[tuple]:
        """정규장 마지막 현재가 기준으로 전 영업일 대비 min_pct% 이상 오른 종목 → (symbol, prev_close, close)."""
        return self.query("""
            WITH last AS (
              SELECT p.symbol, p.trade_date, p.last FROM prices p
              JOIN (SELECT symbol, trade_date, MAX(ts) AS ts FROM prices
                    WHERE session='REGULAR' AND trade_date IN (?, ?) GROUP BY symbol, trade_date) m
                ON p.symbol=m.symbol AND p.ts=m.ts)
            SELECT a.symbol, b.last, a.last FROM last a JOIN last b ON a.symbol=b.symbol
            WHERE a.trade_date=? AND b.trade_date=? AND b.last > 0 AND a.last / b.last - 1 >= ?""",
                          (trade_date, prev_date, trade_date, prev_date, min_pct / 100))

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
