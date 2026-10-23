"""로컬 대시보드 서버 (python -m pennybot dashboard → http://127.0.0.1:8050).

표준 라이브러리만 쓴다. state/trading.db(매매 기록)와 state/market.db(수집 데이터)를 읽기 전용으로 열고,
제어 버튼(킬스위치·전량 청산·전략 켜기/끄기)은 state/control.json 에만 쓴다.
토스 API 는 부르지 않는다 (토큰·호출 한도를 봇과 나눠 쓰지 않도록).

127.0.0.1 에만 열리고, 제어 요청은 같은 출처(Origin)에서 온 것만 받는다.
"""
from __future__ import annotations

import base64
import hmac
import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..collector.store import ro_uri
from ..sessions import SESSIONS, SessionCalendar
from ..trading.control import Control

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


class Data:
    def __init__(self, cfg):
        self.cfg = cfg
        self.trading_db = cfg.trading.db_path
        self.market_db = cfg.collector.db_path
        self.control = Control(cfg.state_dir)
        self.cal = SessionCalendar()      # 봇 하트비트가 없을 때만 쓰는 근사 캘린더

    def _q(self, db: str, sql: str, args=()) -> list[dict]:
        if not Path(db).exists():
            return []
        con = sqlite3.connect(ro_uri(db), uri=True)
        con.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in con.execute(sql, args).fetchall()]
        except sqlite3.OperationalError:
            return []
        finally:
            con.close()

    def t(self, sql, args=()):
        return self._q(self.trading_db, sql, args)

    def m(self, sql, args=()):
        return self._q(self.market_db, sql, args)

    # ---------- 개요 ----------
    def overview(self) -> dict:
        now = datetime.now(timezone.utc)
        hb = {r["component"]: r for r in self.t("SELECT * FROM heartbeat")}
        trader = hb.get("trader")
        info = json.loads(trader["info"]) if trader else {}
        age = (now - datetime.fromisoformat(trader["ts"])).total_seconds() if trader else None
        trade_date, session = self.cal.classify(now)
        if age is not None and age < 120 and info.get("session"):
            session, trade_date = info["session"], info.get("trade_date")
        last_price = self.m("SELECT MAX(ts) AS ts FROM prices")
        eq = self.t("SELECT * FROM equity ORDER BY ts DESC LIMIT 1")
        ctl = self.control.load()
        nxt = self.cal.next_change(now)
        return {
            "now": now.isoformat(timespec="seconds"),
            "session": session, "trade_date": str(trade_date) if trade_date else None,
            "next_change": nxt.isoformat() if nxt else None,
            "mode": info.get("mode") or self.cfg.mode,
            "trader": {"ts": trader["ts"] if trader else None, "age_s": age, "info": info},
            "collector": {"last_price_ts": last_price[0]["ts"] if last_price else None},
            "kill": ctl["kill"], "flatten": ctl["flatten"],
            "equity": eq[0] if eq else None,
            "open_trades": self.t("SELECT COUNT(*) AS n FROM trades WHERE status IN ('PENDING','OPEN','EXITING')")
            [0]["n"] if Path(self.trading_db).exists() else 0,
            "working_orders": info.get("working", 0),
            "mismatch": info.get("mismatch") or {},
            "regime": info.get("regime"),
        }

    def symbol(self, sym: str) -> dict:
        from ..brief import brief, with_filings
        return with_filings(brief(self.market_db, sym), self.dilution(), str(Path(self.cfg.state_dir) / "research.db"))

    def dilution(self):
        """SEC_USER_AGENT 가 있으면 공시 판정 (희석 점수·부양 동기). 처음 한 번만 만든다."""
        if not hasattr(self, "_dil"):
            self._dil = None
            if self.cfg.sec_user_agent and not self.cfg.state_dir.startswith("state_demo"):
                from ..dilution import DilutionService
                from ..screener.edgar import EdgarClient
                self._dil = DilutionService(EdgarClient(self.cfg.sec_user_agent), f"{self.cfg.state_dir}/edgar_cache")
        return self._dil

    def bigruns(self) -> dict | None:
        """python -m pennybot bigruns 의 최신 결과 (대박 급등 사례·비교군)."""
        runs = sorted(Path(self.cfg.state_dir, "research").glob("bigruns-*.json"), reverse=True)
        try:
            return json.loads(runs[0].read_text(encoding="utf-8")) if runs else None
        except ValueError:
            return None

    def chart30(self) -> dict | None:
        """python -m pennybot chart30 의 최신 결과 (급등 전 30일 차트 조건)."""
        runs = sorted(Path(self.cfg.state_dir, "research").glob("chart30-*.json"), reverse=True)
        try:
            return json.loads(runs[0].read_text(encoding="utf-8")) if runs else None
        except ValueError:
            return None

    def research_hist(self) -> dict | None:
        """python -m pennybot research 의 최신 결과."""
        runs = sorted(Path(self.cfg.state_dir, "research").glob("*/report.json"), reverse=True)
        if not runs:
            return None
        try:
            return json.loads(runs[0].read_text(encoding="utf-8"))
        except ValueError:
            return None

    def shorts(self) -> list[dict]:
        """감시 종목(최근 호가를 받는 종목) + 보유 종목의 공매도·대차, 대차 수수료 높은 순."""
        from ..brief import watch_shorts
        last = self.m("SELECT MAX(ts) AS ts FROM quotes")
        syms = []
        if last and last[0]["ts"]:
            since = (datetime.fromisoformat(last[0]["ts"]) - timedelta(minutes=15)).isoformat(timespec="seconds")
            syms = [r["symbol"] for r in self.m("SELECT DISTINCT symbol FROM quotes WHERE ts >= ?", (since,))]
        held = [r["symbol"] for r in self.t("SELECT DISTINCT symbol FROM trades WHERE status IN ('PENDING','OPEN','EXITING')")]
        rows = watch_shorts(self.market_db, list(dict.fromkeys(held + syms))[:150])
        for r in rows:
            r["held"] = r["symbol"] in held
        return sorted(rows, key=lambda r: (not r["held"], -(r["fee_rate"] or 0), -(r["short_ratio"] or 0)))

    def events(self, limit: int = 40) -> list[dict]:
        """'방금 일어난 일' (수집기 이벤트, 최신순)."""
        return self.m("SELECT ts, symbol, kind, text, value FROM events ORDER BY ts DESC LIMIT ?", (limit,))

    def daily(self, sym: str, n: int = 90) -> list[list]:
        """차트용 일봉 [d, o, h, l, c, v] (수집기 daily_bars, 없으면 과거 검증 DB)."""
        sym = sym.upper()
        rows = self.m("SELECT d, open, high, low, close, volume FROM daily_bars WHERE symbol=? ORDER BY d DESC LIMIT ?", (sym, n))
        if not rows:
            rows = self._q(str(Path(self.cfg.state_dir) / "research.db"),
                           "SELECT d, open, high, low, close, volume FROM daily_bars WHERE symbol=? ORDER BY d DESC LIMIT ?", (sym, n))
        return [[r["d"], r["open"], r["high"], r["low"], r["close"], r["volume"]] for r in reversed(rows)]

    def equity(self, days: int) -> list[dict]:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        return self.t("SELECT ts, equity, cash, day_pnl FROM equity WHERE ts >= ? ORDER BY ts", (since,))

    def strategies(self) -> list[dict]:
        ctl = self.control.load()["strategies"]
        rows = self.t("""SELECT strategy, COUNT(*) AS trades, SUM(pnl > 0) AS wins, SUM(pnl) AS pnl,
                         AVG(pnl) AS expectancy, AVG(pnl_pct) AS avg_pct
                         FROM trades WHERE status='CLOSED' GROUP BY strategy""")
        by = {r["strategy"]: r for r in rows}
        n = self.cfg.trading.auto_disable_trades
        out = []
        for name, params in self.cfg.trading.strategies.items():
            r = by.get(name, {})
            recent = self.t("SELECT pnl FROM trades WHERE status='CLOSED' AND strategy=? ORDER BY id DESC LIMIT ?",
                            (name, n))
            auto_off = len(recent) >= n and sum(x["pnl"] for x in recent) / n < 0
            out.append({"strategy": name, "config_enabled": params.get("enabled", True),
                        "toggle": ctl.get(name, True), "auto_disabled": auto_off,
                        "trades": r.get("trades", 0), "win_rate": (r["wins"] / r["trades"]) if r.get("trades") else None,
                        "pnl": r.get("pnl") or 0.0, "expectancy": r.get("expectancy"), "avg_pct": r.get("avg_pct"),
                        "open": self.t("SELECT COUNT(*) AS n FROM trades WHERE strategy=? AND status IN "
                                       "('PENDING','OPEN','EXITING')", (name,))[0]["n"]
                        if Path(self.trading_db).exists() else 0})
        return out

    def positions(self) -> list[dict]:
        rows = self.t("SELECT * FROM trades WHERE status IN ('PENDING','OPEN','EXITING') ORDER BY id")
        for r in rows:
            p = self.m("SELECT last, ts FROM prices WHERE symbol=? ORDER BY ts DESC LIMIT 1", (r["symbol"],))
            r["last"] = p[0]["last"] if p else None
            r["last_ts"] = p[0]["ts"] if p else None
            if r["last"] and r["entry_price"] and r["remaining"]:
                r["unrealized"] = (r["last"] - r["entry_price"]) * r["remaining"]
                r["unrealized_pct"] = (r["last"] / r["entry_price"] - 1) * 100
        return rows

    def orders(self, limit: int) -> list[dict]:
        return self.t("SELECT * FROM orders ORDER BY ts DESC LIMIT ?", (limit,))

    def trades(self, limit: int) -> list[dict]:
        return self.t("SELECT * FROM trades WHERE status IN ('CLOSED','CANCELED') ORDER BY id DESC LIMIT ?", (limit,))

    def signals(self, limit: int) -> list[dict]:
        return self.t("SELECT * FROM signals ORDER BY ts DESC, ok DESC, score DESC LIMIT ?", (limit,))

    # ---------- 수집 상태 ----------
    def collector(self) -> dict:
        r = self.m("SELECT MAX(trade_date) AS d FROM prices")
        d = r[0]["d"] if r else None
        per = {s: {"session": s, "prices": 0, "quotes": 0, "quotes_empty": 0, "bars": 0, "symbols_quoted": 0}
               for s in SESSIONS}
        if d:
            for x in self.m("SELECT session, COUNT(*) AS n FROM prices WHERE trade_date=? GROUP BY session", (d,)):
                per.get(x["session"], {})["prices"] = x["n"]
            for x in self.m("""SELECT session, COUNT(*) AS n, SUM(bid IS NULL OR ask IS NULL) AS empty,
                               COUNT(DISTINCT CASE WHEN bid IS NOT NULL AND ask IS NOT NULL THEN symbol END) AS syms
                               FROM quotes WHERE trade_date=? GROUP BY session""", (d,)):
                if x["session"] in per:
                    per[x["session"]].update(quotes=x["n"], quotes_empty=x["empty"] or 0, symbols_quoted=x["syms"])
            for x in self.m("SELECT session, COUNT(*) AS n FROM bars_1m WHERE trade_date=? GROUP BY session", (d,)):
                per.get(x["session"], {})["bars"] = x["n"]
        last = {}
        for table in ("prices", "quotes", "bars_1m", "rankings"):
            q = self.m(f"SELECT MAX(ts) AS ts FROM {table}")
            last[table] = q[0]["ts"] if q else None
        f = self.m("SELECT MAX(accepted) AS ts, COUNT(*) AS n FROM filings")
        u = self.m("SELECT snapshot_date, COUNT(*) AS n FROM universe GROUP BY snapshot_date "
                   "ORDER BY snapshot_date DESC LIMIT 1")
        spreads = {x["session"]: x["med"] for x in self.m(
            "SELECT session, AVG(spread_med_pct) AS med FROM session_summary WHERE trade_date=? GROUP BY session",
            (d,))} if d else {}
        for s in per:
            per[s]["spread_med_pct"] = spreads.get(s)
        return {"trade_date": d, "sessions": list(per.values()), "last": last,
                "filings": f[0] if f else None, "universe": u[0] if u else None}

    # ---------- 급등주 추적 ----------
    def runners(self, days: int = 10) -> dict:
        """급등주 기록 + 다음 영업일 결과(급등일 종가 대비 데이마켓 마지막·프리마켓 마지막·정규장 첫 가격)."""
        dates = [r["trade_date"] for r in self.m(
            "SELECT DISTINCT trade_date FROM runners ORDER BY trade_date DESC LIMIT ?", (days,))]
        rows = []
        for d in dates:
            nxt = self.m("SELECT MIN(trade_date) AS d FROM prices WHERE trade_date > ?", (d,))
            nd = nxt[0]["d"] if nxt else None
            for r in self.m("SELECT * FROM runners WHERE trade_date=? ORDER BY change_pct DESC", (d,)):
                out = {}
                if nd:
                    for key, sess, order in (("day", "DAY", "DESC"), ("pre", "PRE", "DESC"), ("open", "REGULAR", "ASC")):
                        q = self.m(f"SELECT last FROM prices WHERE symbol=? AND trade_date=? AND session=? "
                                   f"ORDER BY ts {order} LIMIT 1", (r["symbol"], nd, sess))
                        out[key] = (q[0]["last"] / r["close"] - 1) if q and r["close"] else None
                rows.append({**r, "next_date": nd, "ret": out})
        buckets: dict[str, list] = {}
        for r in rows:
            v = r["ret"].get("open")
            if v is not None:
                buckets.setdefault(r.get("dilution_bucket") or "unknown", []).append(v)
        summary = []
        for b in ("low", "mid", "high", "unknown"):
            xs = sorted(buckets.get(b, []))
            summary.append({"bucket": b, "n": len(xs), "mean": sum(xs) / len(xs) if xs else None,
                            "median": xs[len(xs) // 2] if xs else None,
                            "hit": sum(1 for x in xs if x > 0) / len(xs) if xs else None})
        return {"rows": rows, "by_bucket": summary}

    def dilution_events(self, limit: int) -> list[dict]:
        rows = self.m("""SELECT symbol, form, items, events, direction, amount, accepted, session, trade_date,
                         insider_buy_usd, NULL AS title, detail FROM filings
                         WHERE (events IS NOT NULL AND events != '') OR insider_buy_usd > 0
                            OR (detail IS NOT NULL AND detail NOT IN ('', '{}'))
                         ORDER BY accepted DESC LIMIT ?""", (limit,))
        from ..filing_detail import summary
        for r in rows:
            d = json.loads(r.pop("detail") or "{}")
            px = self.m("SELECT last FROM prices WHERE symbol=? AND ts <= ? ORDER BY ts DESC LIMIT 1", (r["symbol"], r["accepted"]))
            r["summary"] = summary(r["form"], d, px[0]["last"] if px else None)
            r["items_ko"] = d.get("items")
            r["headline"] = d.get("headline")
        rows += self.m("""SELECT symbol, 'NEWS' AS form, '' AS items, events, direction, NULL AS amount,
                          published AS accepted, session, trade_date, NULL AS insider_buy_usd, title FROM news
                          WHERE events IS NOT NULL AND events != '' ORDER BY published DESC LIMIT ?""", (limit,))
        return sorted(rows, key=lambda r: r["accepted"] or "", reverse=True)[:limit]

    # ---------- 핵심 가설 점검 ----------
    def research(self, days: int = 20) -> list[dict]:
        """영업일별: 직전 영업일 주목 종목(랭킹)의 '데이마켓 종가 → 정규장 시가' 수익률 vs 나머지 종목."""
        dates = [r["trade_date"] for r in self.m(
            "SELECT DISTINCT trade_date FROM session_summary ORDER BY trade_date DESC LIMIT ?", (days,))]
        out = []
        for i, d in enumerate(dates):
            prev = dates[i + 1] if i + 1 < len(dates) else None
            rows = self.m("""SELECT a.symbol, a.close AS day_close, b.open AS reg_open
                             FROM session_summary a JOIN session_summary b
                               ON a.symbol=b.symbol AND a.trade_date=b.trade_date
                             WHERE a.trade_date=? AND a.session='DAY' AND b.session='REGULAR'
                               AND a.close > 0 AND b.open > 0""", (d,))
            attn = {r["symbol"] for r in self.m(
                """SELECT DISTINCT symbol FROM rankings WHERE trade_date=? AND session IN ('REGULAR','AFTER')""",
                (prev,))} if prev else set()
            groups = {"attention": [], "others": []}
            for r in rows:
                groups["attention" if r["symbol"] in attn else "others"].append(r["reg_open"] / r["day_close"] - 1)
            item = {"trade_date": d}
            for g, xs in groups.items():
                xs.sort()
                item[g] = {"n": len(xs), "mean": sum(xs) / len(xs) if xs else None,
                           "median": xs[len(xs) // 2] if xs else None,
                           "hit": sum(1 for x in xs if x > 0) / len(xs) if xs else None}
            out.append(item)
        return list(reversed(out))


def _backtests(state_dir: str, limit: int = 10) -> list[dict]:
    """python -m pennybot backtest 결과 (state/backtests/*/report.json), 최신순."""
    out = []
    for f in sorted(Path(state_dir, "backtests").glob("*/report.json"), reverse=True)[:limit]:
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        r["run"] = f.parent.name
        if out:
            r.pop("trades", None)      # 최신 실행만 개별 매매까지
        out.append(r)
    return out


def _reports(state_dir: str, limit: int = 30) -> list[dict]:
    """일일 리포트 (state/reports/<영업일>.txt + .json), 최신순."""
    out = []
    for f in sorted(Path(state_dir, "reports").glob("*.txt"), reverse=True)[:limit]:
        row = {"trade_date": f.stem, "text": f.read_text(encoding="utf-8")}
        try:
            j = json.loads(f.with_suffix(".json").read_text(encoding="utf-8"))
            row.update(net=j["day"]["net"], trades=j["day"]["trades"], wins=j["day"]["wins"])
        except (OSError, ValueError, KeyError):
            pass
        out.append(row)
    return out


class Handler(BaseHTTPRequestHandler):
    data: Data = None  # 서버 생성 시 주입

    def log_message(self, fmt, *args):
        log.debug(fmt, *args)

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        """PENNYBOT_DASHBOARD_PASSWORD 가 있으면 HTTP Basic 비밀번호를 확인한다 (아이디는 아무거나)."""
        pw = self.data.cfg.dashboard_password
        if not pw:
            return True
        h = self.headers.get("Authorization") or ""
        try:
            given = base64.b64decode(h[6:]).decode("utf-8").partition(":")[2] if h.startswith("Basic ") else ""
        except ValueError:
            given = ""
        if hmac.compare_digest(given.encode(), pw.encode()):
            return True
        body = b'{"error": "password required"}'
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="pennybot", charset="UTF-8"')
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    def _readonly(self) -> bool:
        """비밀번호 없이 외부 주소(터널)로 들어온 요청은 보기만: 주소를 아는 누구나 청산·정지를 누를 수 있게 되므로."""
        host = urlparse("//" + (self.headers.get("Host") or "")).hostname
        return not self.data.cfg.dashboard_password and host not in ("127.0.0.1", "localhost")

    def do_GET(self):
        if not self._authorized():
            return
        u = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        n = lambda k, d: max(1, min(int(qs.get(k, d)), 1000))
        d = self.data
        routes = {
            "/api/overview": lambda: {**d.overview(), "readonly": self._readonly()},
            "/api/equity": lambda: d.equity(n("days", 7)),
            "/api/strategies": lambda: d.strategies(),
            "/api/positions": lambda: d.positions(),
            "/api/orders": lambda: d.orders(n("limit", 50)),
            "/api/trades": lambda: d.trades(n("limit", 50)),
            "/api/signals": lambda: d.signals(n("limit", 200)),
            "/api/collector": lambda: d.collector(),
            "/api/research": lambda: d.research(n("days", 20)),
            "/api/runners": lambda: d.runners(n("days", 10)),
            "/api/dilution": lambda: d.dilution_events(n("limit", 60)),
            "/api/backtests": lambda: _backtests(d.cfg.state_dir, n("limit", 10)),
            "/api/reports": lambda: _reports(d.cfg.state_dir, n("limit", 30)),
            "/api/shorts": lambda: d.shorts(),
            "/api/events": lambda: d.events(n("limit", 40)),
            "/api/daily": lambda: d.daily(qs.get("s", "")[:12], n("n", 90)),
            "/api/precursor": lambda: __import__("pennybot.precursor", fromlist=["ranked"]).ranked(d.market_db, n("limit", 80)),
            "/api/symbol": lambda: d.symbol(qs.get("s", "")[:12]),
            "/api/research_hist": lambda: d.research_hist(),
            "/api/bigruns": lambda: d.bigruns(),
            "/api/chart30": lambda: d.chart30(),
        }
        if u.path in routes:
            try:
                return self._json(routes[u.path]())
            except Exception as e:
                log.exception("API 오류 %s", u.path)
                return self._json({"error": str(e)}, 500)
        if u.path in ("/", "/index.html"):
            body = (STATIC / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._authorized():
            return
        if urlparse(self.path).path != "/api/control":
            return self._json({"error": "not found"}, 404)
        origin = self.headers.get("Origin") or ""
        host = self.headers.get("Host") or ""
        if not origin or urlparse(origin).netloc != host:
            return self._json({"error": "같은 출처의 요청만 허용합니다."}, 403)
        if self._readonly():
            return self._json({"error": "외부 주소에서는 보기만 됩니다. 조작은 이 컴퓨터나 텔레그램으로"
                                        " (.env 에 PENNYBOT_DASHBOARD_PASSWORD 를 넣으면 외부에서도 조작 가능)."}, 403)
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except ValueError:
            return self._json({"error": "잘못된 JSON"}, 400)
        ctl = self.data.control
        if "kill" in body:
            d = ctl.update(kill=bool(body["kill"]))
        elif body.get("flatten") is True:
            d = ctl.update(flatten=True)
        elif "strategy" in body and body["strategy"] in self.data.cfg.trading.strategies:
            d = ctl.update(strategy=(body["strategy"], bool(body.get("enabled"))))
        else:
            return self._json({"error": "알 수 없는 제어 요청"}, 400)
        log.info("제어 변경: %s", body)
        self._json(d)


def make_server(cfg, host: str = "127.0.0.1", port: int = 8050) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"data": Data(cfg)})
    return ThreadingHTTPServer((host, port), handler)


def serve(cfg, port: int = 8050, open_browser: bool = True) -> None:
    try:
        srv = make_server(cfg, port=port)
    except OSError as e:
        raise SystemExit(f"포트 {port} 를 열 수 없습니다 ({e}). 대시보드가 이미 켜져 있거나 다른 프로그램이 쓰는 중입니다."
                         f" 다른 포트로: dashboard --port 8060")
    url = f"http://127.0.0.1:{port}"
    print(f"대시보드: {url}  (이 창을 닫거나 Ctrl+C 로 종료)")
    if open_browser:
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
