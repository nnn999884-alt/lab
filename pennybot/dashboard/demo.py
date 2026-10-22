"""대시보드 미리보기용 가짜 데이터 (python -m pennybot dashboard --demo).

실제 데이터와 섞이지 않도록 state_demo/ 에만 쓴다. 숫자는 모두 무작위로 만든 예시다.
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..collector.store import Store
from ..trading.journal import Journal, now_iso

STRATS = ["runner_overnight", "dilution_catalyst", "day_ignition"]


def build(state_dir: str | Path = "state_demo", seed: int = 7) -> tuple[str, str]:
    rnd = random.Random(seed)
    d = Path(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    for f in ("trading.db", "market.db", "control.json"):
        (d / f).unlink(missing_ok=True)
    j = Journal(d / "trading.db", mode="paper")
    st = Store(d / "market.db")
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

    # 계좌 평가액 7일치 (5분 간격)
    eq = 10_000.0
    t = now - timedelta(days=7)
    while t <= now:
        eq += rnd.gauss(0.4, 6)
        j.snapshot_equity(eq * 0.7, eq * 0.3, rnd.gauss(5, 20), t)
        t += timedelta(minutes=5)

    # 청산된 매매
    syms = ["ABCD", "PENX", "LOWQ", "MICR", "TINY", "BIOZ", "NANO", "QBIT", "ZAPP", "GLDN"]
    sessions = {s: "PRE" for s in STRATS}
    for i in range(36):
        s = STRATS[i % 3]
        sym = rnd.choice(syms)
        entry = round(rnd.uniform(0.6, 4.5), 4)
        drift = {"runner_overnight": 0.015, "dilution_catalyst": 0.02, "day_ignition": -0.004}[s]
        exit_ = round(entry * (1 + rnd.gauss(drift, 0.05)), 4)
        qty = int(250 / entry)
        day = now - timedelta(days=7 - i // 6)
        tid = j.open_trade(strategy=s, symbol=sym, trade_date=day.date().isoformat(), planned_qty=qty,
                           stop=entry * 0.94, target=entry * 1.08, sell_from=day, deadline=day + timedelta(hours=8),
                           reason="예시 데이터", now=day)
        j.record_entry_fill(tid, qty, entry, "DAY", day)
        j.set_exiting(tid, rnd.choice(["매도 구간", "익절", "손절", "강제 청산 시각"]))
        j.record_exit_fill(tid, qty, exit_, sessions[s], day + timedelta(hours=7))

    # 보유 중 2건 + 대기 주문 1건
    for sym, s, px in [("PENX", "runner_overnight", 1.12), ("BIOZ", "dilution_catalyst", 2.31)]:
        tid = j.open_trade(strategy=s, symbol=sym, trade_date=now.date().isoformat(), planned_qty=200,
                           stop=px * 0.94, target=px * 1.08, sell_from=now + timedelta(hours=6),
                           deadline=now + timedelta(hours=8), reason="예시 데이터", now=now)
        j.record_entry_fill(tid, 200, px, "DAY", now)
        j.set_backstop(tid, f"demo-stop-{tid}", 200, px * 0.94 * 0.97)
        j.log_order({"order_id": f"demo-{tid}", "symbol": sym, "side": "BUY", "type": "LIMIT", "qty": 200,
                     "price": px, "status": "FILLED", "filled": 200, "avg_price": px},
                    trade_id=tid, purpose="entry", session="DAY", now=now, note="예시 데이터")
        st.put_prices([{"symbol": sym, "ts": now_iso(now), "trade_date": now.date().isoformat(), "session": "DAY",
                        "last": px * (1 + rnd.gauss(0.01, 0.02)), "price_ts": None}])
    tid = j.open_trade(strategy="day_ignition", symbol="NANO", trade_date=now.date().isoformat(), planned_qty=300,
                       stop=0.7, target=0.9, sell_from=now + timedelta(hours=3), deadline=now + timedelta(hours=8),
                       reason="예시 데이터", now=now)
    j.log_order({"order_id": "demo-open", "symbol": "NANO", "side": "BUY", "type": "LIMIT", "qty": 300,
                 "price": 0.7812, "status": "OPEN", "filled": 0, "avg_price": None},
                trade_id=tid, purpose="entry", session="DAY", now=now, note="예시 데이터")

    # 전략 후보
    reasons_ok = ["전날 +62%, 회전 3.1회, 데이마켓 +2.1%, 희석 18점", "ATM 종료 (데이마켓 공개), 반영 +4.2%, 희석 12점 [ATM_TERMINATED]",
                  "내부자 장내매수, 데이마켓 +7.5%, 희석 25점"]
    reasons_no = ["데이마켓 호가 없음", "스프레드 4.2%", "희석 위험 75점 (ATM 진행 중, 최근 6일 내 증자)", "급등 후 증자 공시 나옴",
                  "펌프 위험 신호: 상장 1년 이내, 해외 발행사(K3), 발행주식 1,500만 주 미만", "전날 +41%였지만 고가 대비 밀려 마감",
                  "리스크: 전략 동시 보유 한도"]
    j.log_signals([{"ts": now_iso(now), "trade_date": now.date().isoformat(), "session": "DAY", "strategy": STRATS[i % 3],
                    "symbol": rnd.choice(syms), "ok": int(i < 3), "reason": reasons_ok[i] if i < 3 else rnd.choice(reasons_no),
                    "score": rnd.uniform(0, 50), "price": rnd.uniform(0.5, 4)} for i in range(18)])
    j.beat("trader", {"session": "DAY", "trade_date": now.date().isoformat(), "mode": "paper", "working": 1,
                      "regime": "정상", "last_scan": now_iso(now), "candidates": 3, "mismatch": {}}, now)

    # 수집 현황: 세션별 호가 확보율 (데이마켓이 가장 낮게)
    td = now.date().isoformat()
    for sess, have in [("DAY", 0.55), ("PRE", 0.8), ("REGULAR", 0.99), ("AFTER", 0.7)]:
        rows = []
        for k in range(400):
            ok = rnd.random() < have
            bid = rnd.uniform(0.5, 4)
            rows.append({"symbol": f"S{k % 80}", "ts": f"{td}T{['01','09','15','21'][['DAY','PRE','REGULAR','AFTER'].index(sess)]}:{k // 60:02d}:{k % 60:02d}+00:00",
                         "trade_date": td, "session": sess, "bid": bid if ok else None, "bid_size": 100,
                         "ask": bid * (1 + rnd.uniform(0.002, 0.04)) if ok else None, "ask_size": 100,
                         "levels": 1, "book_ts": None})
        st.put_quotes(rows)
    # 가설 점검: 영업일별 데이 종가 → 정규장 시가
    for k in range(15):
        day = (now - timedelta(days=21 - k)).date()
        if day.weekday() >= 5:
            continue
        ds, prev = day.isoformat(), (day - timedelta(days=1 if day.weekday() else 3)).isoformat()
        attn = [f"A{n}" for n in range(12)]
        st.put_rankings([{"ts": f"{prev}T15:00:00+00:00", "trade_date": prev, "session": "REGULAR", "type": "MARKET_TRADING_AMOUNT",
                          "duration": "realtime", "rank": n + 1, "symbol": s, "last": 1, "base": 1, "change_rate": 0,
                          "volume": 1, "amount": 1, "ranked_at": None} for n, s in enumerate(attn)])
        for s in attn + [f"O{n}" for n in range(30)]:
            dc = rnd.uniform(0.6, 4)
            ro = dc * (1 + rnd.gauss(0.012 if s.startswith("A") else 0.0, 0.03))
            for sess, o, c in [("DAY", dc * 0.99, dc), ("REGULAR", ro, ro * 1.01)]:
                st.db.execute("INSERT OR REPLACE INTO session_summary (symbol, trade_date, session, open, high, low, close, "
                              "volume, bars) VALUES (?,?,?,?,?,?,?,?,?)", (s, ds, sess, o, max(o, c), min(o, c), c, 1000, 10))
        st.db.commit()
    # 급등주와 다음 날 흐름: 희석 위험 낮은 급등주가 더 잘 버티는 것처럼 보이게 만든 예시
    events = [("ATM_TERMINATED", 1), ("REGISTERED_DIRECT,SECURITIES_PURCHASE_AGREEMENT", -1), ("ATM_NEW", -1),
              ("REGISTRATION_WITHDRAWN", 1), ("WARRANT_INDUCEMENT,WARRANT_REPRICE", -1), ("MATERIAL_CONTRACT", 1)]
    for k in range(10):
        day = (now - timedelta(days=12 - k)).date()
        if day.weekday() >= 4:
            continue
        ds, nd = day.isoformat(), (day + timedelta(days=1)).isoformat()
        runners, prices = [], []
        for n in range(rnd.randint(2, 5)):
            sym = f"R{k}{n}"
            score = rnd.choice([10, 20, 30, 50, 70, 85])
            bucket = "low" if score < 35 else "high" if score >= 60 else "mid"
            close = rnd.uniform(0.8, 4)
            runners.append({"trade_date": ds, "symbol": sym, "prev_close": close / 1.6, "close": close,
                            "change_pct": rnd.uniform(30, 180), "high": close * 1.05, "low": close / 1.7,
                            "volume": 8e6, "close_loc": rnd.uniform(0.4, 1), "shares": 4e6, "rotation": rnd.uniform(0.5, 6),
                            "dilution_score": score, "dilution_bucket": bucket,
                            "dilution_reasons": {"low": "ATM_TERMINATED", "mid": "증자 등록(S-3) 유효",
                                                 "high": "ATM 진행 중, 최근 5일 내 증자"}[bucket],
                            "pump_flags": rnd.choice(["", "", "상장 1년 이내", "해외 발행사(K3), 발행주식 1,500만 주 미만"])})
            drift = {"low": 0.04, "mid": 0.0, "high": -0.06}[bucket]
            for sess, hh, m in [("DAY", "07", 0.5), ("PRE", "13", 0.8), ("REGULAR", "13", 1.0)]:
                prices.append({"symbol": sym, "ts": f"{nd}T{hh}:{'29' if sess != 'REGULAR' else '31'}:00+00:00",
                               "trade_date": nd, "session": sess, "last": close * (1 + rnd.gauss(drift * m, 0.05)),
                               "price_ts": None})
        st.put_runners(runners)
        st.put_prices(prices)
    st.put_filings([{"accession": f"demo-{i}", "cik": i, "symbol": rnd.choice(syms), "form": "8-K", "items": "",
                     "accepted": now_iso(now - timedelta(hours=3 * i)), "trade_date": now.date().isoformat(),
                     "session": rnd.choice(["DAY", "AFTER", "PRE"]), "insider_buy_usd": None,
                     "events": ev, "direction": dr, "amount": 4.5e6 if dr < 0 else None}
                    for i, (ev, dr) in enumerate(events)])
    (d / "control.json").write_text(json.dumps({"kill": False, "flatten": False, "strategies": {}}), encoding="utf-8")
    _demo_backtests(d, rnd, now)
    _demo_reports(d, j, now)
    return str(d / "trading.db"), str(d / "market.db")


def _demo_reports(d: Path, j: Journal, now) -> None:
    """일일 리포트 예시: 예시 매매 기록으로 최근 영업일 몇 개."""
    from ..trading.report import build, render, save
    days = [r["trade_date"] for r in j.q("SELECT DISTINCT trade_date FROM trades WHERE status='CLOSED' "
                                         "ORDER BY trade_date DESC LIMIT 6")]
    for td in days:
        rep = build(j, td, now)
        save(rep, render(rep), d / "reports")


def _demo_backtests(d: Path, rnd, now) -> None:
    """백테스트 결과 예시 (report.json 과 같은 모양)."""
    from ..backtest import _stats
    for k, params in enumerate([{}, {"runner_overnight.trail_pct": 6}]):
        trades = []
        for i in range(40):
            strat = rnd.choice(["runner_overnight", "runner_overnight", "dilution_catalyst", "day_ignition"])
            pct = rnd.gauss(1.5 if strat != "day_ignition" else -0.5, 9)
            entry, qty = rnd.uniform(0.6, 4), rnd.randint(50, 280)
            trades.append({"strategy": strat, "symbol": f"B{i:02d}", "qty": qty, "entry_price": entry,
                           "trade_date": (now.date() - timedelta(days=40 - i)).isoformat(),
                           "net": entry * qty * pct / 100, "pnl_pct": pct, "exit_session": rnd.choice(["PRE", "REGULAR"]),
                           "exit_reason": rnd.choice(["매도 구간", "손절", "익절(분할) + 트레일링 스톱", "강제 청산 시각"])})
        cut = trades[28]["trade_date"]
        tr, te = [t for t in trades if t["trade_date"] < cut], [t for t in trades if t["trade_date"] >= cut]
        rep = {"start": trades[0]["trade_date"], "end": trades[-1]["trade_date"], "days": 40, "split_date": cut,
               "all": _stats(trades), "train": _stats(tr), "test": _stats(te), "params": params, "fee_pct": 0.1,
               "open_left": [], "exit_reasons": {},
               "by_strategy": {s: {"all": _stats([t for t in trades if t["strategy"] == s]),
                                   "train": _stats([t for t in tr if t["strategy"] == s]),
                                   "test": _stats([t for t in te if t["strategy"] == s])}
                               for s in sorted({t["strategy"] for t in trades})},
               "trades": trades}
        out = d / "backtests" / f"demo-{k + 1}"
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.json").write_text(json.dumps(rep, ensure_ascii=False), encoding="utf-8")
