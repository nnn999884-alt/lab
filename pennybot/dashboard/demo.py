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

STRATS = ["opening_premium", "filing_reaction", "day_dip"]


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
    sessions = {"opening_premium": "PRE", "filing_reaction": "PRE", "day_dip": "PRE"}
    for i in range(36):
        s = STRATS[i % 3]
        sym = rnd.choice(syms)
        entry = round(rnd.uniform(0.6, 4.5), 4)
        drift = {"opening_premium": 0.012, "filing_reaction": 0.02, "day_dip": -0.004}[s]
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
    for sym, s, px in [("PENX", "opening_premium", 1.12), ("BIOZ", "filing_reaction", 2.31)]:
        tid = j.open_trade(strategy=s, symbol=sym, trade_date=now.date().isoformat(), planned_qty=200,
                           stop=px * 0.94, target=px * 1.08, sell_from=now + timedelta(hours=6),
                           deadline=now + timedelta(hours=8), reason="예시 데이터", now=now)
        j.record_entry_fill(tid, 200, px, "DAY", now)
        j.log_order({"order_id": f"demo-{tid}", "symbol": sym, "side": "BUY", "type": "LIMIT", "qty": 200,
                     "price": px, "status": "FILLED", "filled": 200, "avg_price": px},
                    trade_id=tid, purpose="entry", session="DAY", now=now, note="예시 데이터")
        st.put_prices([{"symbol": sym, "ts": now_iso(now), "trade_date": now.date().isoformat(), "session": "DAY",
                        "last": px * (1 + rnd.gauss(0.01, 0.02)), "price_ts": None}])
    tid = j.open_trade(strategy="day_dip", symbol="NANO", trade_date=now.date().isoformat(), planned_qty=300,
                       stop=0.7, target=0.9, sell_from=now + timedelta(hours=3), deadline=now + timedelta(hours=8),
                       reason="예시 데이터", now=now)
    j.log_order({"order_id": "demo-open", "symbol": "NANO", "side": "BUY", "type": "LIMIT", "qty": 300,
                 "price": 0.7812, "status": "OPEN", "filled": 0, "avg_price": None},
                trade_id=tid, purpose="entry", session="DAY", now=now, note="예시 데이터")

    # 전략 후보
    reasons_ok = ["전일 랭킹 3위, 데이마켓 +2.1%", "내부자 장내매수 $42,000, 반영 +1.2%", "공시 없이 -14.2% 급락 후 하락 멈춤"]
    reasons_no = ["데이마켓 호가 없음", "스프레드 4.2%", "희석/상폐 공시: 차단:424B5 증자공시 12일 전", "전일 종가 대비 +22.4% (범위 밖)",
                  "5일 +78% (군중 쏠림)", "리스크: 전략 동시 보유 한도"]
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
    (d / "control.json").write_text(json.dumps({"kill": False, "flatten": False, "strategies": {}}))
    return str(d / "trading.db"), str(d / "market.db")
