"""종목 한눈에 보기: 수집기 DB(state/market.db)에 쌓인 것을 종목 하나로 모은다.

현재가·세션 흐름, 호가, 공매도 비율(FINRA), 대차 수수료·가능 수량(IBKR), SSR·Reg SHO, 거래정지,
공시·보도자료, 급등주 기록. 대시보드 '종목 조회'와 텔레그램 /종목 이 같이 쓴다. 토스 API 는 부르지 않는다.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .collector.store import ro_uri

SESS = {"DAY": "데이마켓", "PRE": "프리마켓", "REGULAR": "정규장", "AFTER": "애프터마켓", "CLOSED": "휴장"}


class _DB:
    """연결 하나로 여러 번 조회 (종목 150개 × 여러 조회를 매번 새로 열면 느리다)."""

    def __init__(self, path: str):
        self.path = path
        self.con = None
        if Path(path).exists():
            self.con = sqlite3.connect(ro_uri(path), uri=True)
            self.con.row_factory = sqlite3.Row

    def q(self, sql: str, args=()) -> list[dict]:
        if self.con is None:
            return []
        try:
            return [dict(r) for r in self.con.execute(sql, args).fetchall()]
        except sqlite3.OperationalError:          # 예전 DB 에 아직 없는 표
            return []

    def __del__(self):
        if self.con is not None:
            self.con.close()

    def one(self, sql: str, args=()) -> dict | None:
        r = self.q(sql, args)
        return r[0] if r else None


def short_ratio(r: dict | None) -> float | None:
    return r["short_volume"] / r["total_volume"] if r and r.get("total_volume") else None


def brief(db_path: str, symbol: str, now: datetime | None = None) -> dict:
    db, s = _DB(db_path), symbol.upper().strip()
    now = now or datetime.now(timezone.utc)
    out: dict = {"symbol": s}
    u = db.one("SELECT name, market, last FROM universe WHERE symbol=? ORDER BY snapshot_date DESC LIMIT 1", (s,))
    out["name"], out["market"] = (u or {}).get("name"), (u or {}).get("market")
    last = db.one("SELECT ts, trade_date, session, last FROM prices WHERE symbol=? ORDER BY ts DESC LIMIT 1", (s,))
    out["price"] = last
    if last:
        # 전 영업일 정규장 마지막 가격 = 기준가
        prev = db.one("""SELECT last FROM prices WHERE symbol=? AND session='REGULAR' AND trade_date < ?
                         ORDER BY ts DESC LIMIT 1""", (s, last["trade_date"]))
        out["prev_close"] = prev["last"] if prev else None
        out["change_pct"] = (last["last"] / prev["last"] - 1) * 100 if prev and prev["last"] else None
        # 이번 영업일 세션별 마지막 가격 (데이 → 프리 → 정규 → 애프터)
        out["sessions"] = db.q("""SELECT p.session, p.last, p.ts FROM prices p JOIN (
                                      SELECT session, MAX(ts) AS ts FROM prices WHERE symbol=? AND trade_date=? GROUP BY session) m
                                  ON p.session=m.session AND p.ts=m.ts WHERE p.symbol=? ORDER BY p.ts""",
                               (s, last["trade_date"], s))
    out["quote"] = db.one("SELECT ts, bid, ask, bid_size, ask_size FROM quotes WHERE symbol=? ORDER BY ts DESC LIMIT 1", (s,))
    sv = db.q("SELECT * FROM short_volume WHERE symbol=? ORDER BY trade_date DESC LIMIT 5", (s,))
    ratios = [x for x in (short_ratio(r) for r in sv) if x is not None]
    out["short"] = {"latest": sv[0] if sv else None, "ratio": short_ratio(sv[0]) if sv else None,
                    "ratio_5d": sum(ratios) / len(ratios) if ratios else None, "days": len(sv)}
    b = db.q("SELECT ts, fee_rate, rebate_rate, available FROM borrow WHERE symbol=? ORDER BY ts DESC LIMIT 1", (s,))
    day_ago = (now - timedelta(hours=24)).isoformat(timespec="seconds")
    b0 = db.one("SELECT fee_rate, available FROM borrow WHERE symbol=? AND ts <= ? ORDER BY ts DESC LIMIT 1", (s, day_ago))
    out["borrow"] = {**b[0], "fee_24h_ago": b0["fee_rate"] if b0 else None,
                     "avail_24h_ago": b0["available"] if b0 else None} if b else None
    ssr_day = db.one("SELECT MAX(trade_date) AS d FROM ssr")
    out["ssr"] = bool(ssr_day and ssr_day["d"] and db.one("SELECT 1 AS x FROM ssr WHERE trade_date=? AND symbol=?",
                                                          (ssr_day["d"], s)))
    out["ssr_date"] = ssr_day["d"] if ssr_day else None
    th_day = db.one("SELECT MAX(trade_date) AS d FROM reg_sho")
    out["reg_sho"] = bool(th_day and th_day["d"] and db.one("SELECT 1 AS x FROM reg_sho WHERE trade_date=? AND symbol=?",
                                                            (th_day["d"], s)))
    out["reg_sho_days"] = len(db.q("SELECT trade_date FROM reg_sho WHERE symbol=?", (s,)))
    week = (now - timedelta(days=7)).isoformat(timespec="seconds")
    out["halts"] = db.q("SELECT halt_ts, reason, reason_ko, resume_ts FROM halts WHERE symbol=? AND halt_ts >= ? "
                        "ORDER BY halt_ts DESC LIMIT 5", (s, week))
    out["events"] = db.q("""SELECT accepted AS ts, form, events, direction, insider_buy_usd, amount, NULL AS title, session, detail
                            FROM filings WHERE symbol=?
                            UNION ALL
                            SELECT published AS ts, 'NEWS' AS form, events, direction, NULL, NULL, title, session, NULL
                            FROM news WHERE symbol=? ORDER BY ts DESC LIMIT 8""", (s, s))
    import json as _json
    from .filing_detail import summary
    for e in out["events"]:
        d = _json.loads(e.pop("detail") or "{}")
        e["summary"] = summary(e["form"], d, (last or {}).get("last"))
        e["items_ko"] = d.get("items")
        e["headline"] = d.get("headline")
    out["runner"] = db.one("SELECT * FROM runners WHERE symbol=? ORDER BY trade_date DESC LIMIT 1", (s,))
    rk = db.one("""SELECT type, MIN(rank) AS rank FROM rankings WHERE symbol=? AND ts >= ?
                   GROUP BY type ORDER BY MIN(rank) LIMIT 1""", (s, (now - timedelta(hours=24)).isoformat(timespec="seconds")))
    out["rank"] = rk
    out["found"] = bool(u or last or sv or b)
    return out


def watch_shorts(db_path: str, symbols: list[str]) -> list[dict]:
    """공매도·대차 표: 감시·보유 종목 각각의 최신 값."""
    db = _DB(db_path)
    ssr_d = (db.one("SELECT MAX(trade_date) AS d FROM ssr") or {}).get("d")
    th_d = (db.one("SELECT MAX(trade_date) AS d FROM reg_sho") or {}).get("d")
    ssr = {r["symbol"] for r in db.q("SELECT symbol FROM ssr WHERE trade_date=?", (ssr_d,))} if ssr_d else set()
    th = {r["symbol"] for r in db.q("SELECT symbol FROM reg_sho WHERE trade_date=?", (th_d,))} if th_d else set()
    bts = (db.one("SELECT MAX(ts) AS t FROM borrow") or {}).get("t")
    borrow = {r["symbol"]: r for r in db.q("SELECT symbol, fee_rate, available FROM borrow WHERE ts=?", (bts,))} if bts else {}
    out = []
    for s in symbols:
        sv = db.q("SELECT short_volume, total_volume, trade_date FROM short_volume WHERE symbol=? "
                  "ORDER BY trade_date DESC LIMIT 5", (s,))
        ratios = [x for x in (short_ratio(r) for r in sv) if x is not None]
        halt = db.one("SELECT halt_ts, reason_ko, resume_ts FROM halts WHERE symbol=? ORDER BY halt_ts DESC LIMIT 1", (s,))
        b = borrow.get(s)
        out.append({"symbol": s, "short_ratio": short_ratio(sv[0]) if sv else None,
                    "short_ratio_5d": sum(ratios) / len(ratios) if ratios else None,
                    "short_date": sv[0]["trade_date"] if sv else None,
                    "fee_rate": b["fee_rate"] if b else None, "available": b["available"] if b else None,
                    "ssr": s in ssr, "reg_sho": s in th, "halt": halt})
    return out


# ---------- 텔레그램용 글 ----------
def _px(v):
    return "-" if v is None else f"${v:.4f}" if v < 1 else f"${v:.2f}"


def _qty(v):
    if v is None:
        return "-"
    return f"{v / 1e6:.1f}M" if v >= 1e6 else f"{v / 1e3:.0f}K" if v >= 1e3 else f"{v:.0f}"


def render(b: dict) -> str:
    s = b["symbol"]
    if not b["found"]:
        return f"🔎 {s}: 수집기 기록이 없습니다 (유니버스 $0.2~$10 밖이거나 아직 수집 전)"
    p = b.get("price")
    head = f"🔎 {s}" + (f" · {b['name']}" if b.get("name") else "")
    lines = [head]
    if p:
        chg = f" ({b['change_pct']:+.1f}%)" if b.get("change_pct") is not None else ""
        lines.append(f"{_px(p['last'])}{chg} · {SESS.get(p['session'], p['session'])} · 영업일 {p['trade_date']}")
        if b.get("sessions"):
            lines.append(" → ".join(f"{SESS.get(x['session'], x['session'])} {_px(x['last'])}" for x in b["sessions"]))
    st = b.get("stats") or {}
    if st:
        from .trading.messages import money_s, qty_s
        part = [f"거래량 {qty_s(st.get('volume'))}" + (f" (평소 {st['vol_ratio']:.1f}배)" if st.get("vol_ratio") else ""),
                f"거래대금 {money_s(st.get('dollar'))}"]
        if st.get("ret_30") is not None:
            part.append(f"30일 {st['ret_30']:+.0f}%")
        lines.append("📊 " + " · ".join(part))
    q = b.get("quote")
    if q and q.get("bid") and q.get("ask"):
        lines.append(f"호가 {_px(q['bid'])} / {_px(q['ask'])} (스프레드 {(q['ask'] / q['bid'] - 1) * 100:.1f}%)")
    sh, bo = b["short"], b.get("borrow")
    parts = []
    if sh["ratio"] is not None:
        parts.append(f"공매도 비율 {sh['ratio'] * 100:.0f}%" + (f" (5일 평균 {sh['ratio_5d'] * 100:.0f}%)" if sh["days"] > 1 else ""))
    if bo:
        parts.append(f"대차 수수료 {bo['fee_rate']:.1f}% · 가능 {_qty(bo['available'])}주")
    if parts:
        lines.append("📉 " + " · ".join(parts))
    flags = [x for x, on in (("SSR(공매도 호가 제한)", b["ssr"]), (f"Reg SHO 결제불이행 ({b['reg_sho_days']}일째 기록)", b["reg_sho"])) if on]
    if flags:
        lines.append("🚩 " + " · ".join(flags))
    for h in b["halts"][:2]:
        lines.append(f"⏸ 거래정지 {h['halt_ts'][5:16].replace('T', ' ')} ET {h['reason_ko']}"
                     + (f" → 재개 {h['resume_ts'][11:16]}" if h.get("resume_ts") else ""))
    for e in b["events"][:4]:
        arrow = "▲" if (e["direction"] or 0) > 0 else "▼" if (e["direction"] or 0) < 0 else "·"
        what = e["title"] or e.get("headline") or ", ".join(e.get("items_ko") or []) or e["events"] or e["form"]
        lines.append(f"{arrow} {e['ts'][5:10]} {'보도자료' if e['form'] == 'NEWS' else e['form']}: {what}"[:140])
        if e.get("summary"):
            lines.append(f"   {e['summary']}"[:200])
    f = b.get("filings")
    if f:
        if f.get("motives"):
            lines.append("🎯 부양 동기: " + " · ".join(m["label"] for m in f["motives"]))
        lines.append(f"💧 희석 위험 {f['dilution_score']:.0f}점" + (f" ({', '.join(f['dilution_reasons'][:3])})" if f["dilution_reasons"] else ""))
    else:
        r = b.get("runner")
        if r and r.get("dilution_score") is not None:
            lines.append(f"💧 희석 위험 {r['dilution_score']:.0f}점 ({r['trade_date']} 급등 기록)")
    return "\n".join(lines)


def with_filings(b: dict, dil, research_db: str | None = None) -> dict:
    """희석 점수·부양 동기를 붙인다 (EDGAR 조회, 실패해도 나머지는 그대로)."""
    if not b.get("found") or dil is None:
        return b
    from .motive import daily_closes, live
    try:
        price = (b.get("price") or {}).get("last")
        b["filings"] = live(dil, b["symbol"], price, closes=daily_closes(research_db, b["symbol"]) if research_db else None)
    except Exception as e:                       # EDGAR 장애·미등록 종목
        b["filings"] = None
        b["filings_error"] = str(e)[:200]
    return b
