"""알림 성과: 실제 호가 비용 반영 평균 수익률, 종목 중복 제거."""
from datetime import datetime, timedelta, timezone

import pytest

from pennybot import alertperf
from pennybot.collector import Store

T0 = datetime(2026, 11, 17, 14, 0, tzinfo=timezone.utc)      # 09:00 ET (프리마켓), 거래일 2026-11-17


def iso(t):
    return t.isoformat()


def bars(db, sym, closes, highs=None, lows=None):
    """1분봉 3개: [0] 알림 1분 전, [1] 알림 시각(= 감지가 p0 의 종가), [2] 알림 1분 뒤 (여기 고가·저가가 결과를 정함)."""
    for i, c in enumerate(closes):
        t = T0 - timedelta(minutes=1) + timedelta(minutes=i)
        h = (highs or {}).get(i, c)
        lo = (lows or {}).get(i, c)
        db.execute("INSERT INTO bars_1m (symbol, ts, trade_date, session, open, high, low, close, volume) "
                   "VALUES (?,?,?,?,?,?,?,?,?)", (sym, iso(t), "2026-11-17", "PRE", c, h, lo, c, 1000))


@pytest.fixture
def mq(tmp_path):
    st = Store(tmp_path / "m.db")
    db = st.db
    bars(db, "AAA", [1.00, 1.00, 1.00], highs={2: 1.21})                    # 알림 뒤 +21% 고가 → 적중
    bars(db, "BBB", [1.00, 1.00, 1.00], lows={2: 0.89})                     # −11% 저가 → 손절
    bars(db, "CCC", [2.00, 2.00, 2.10])                                     # 둘 다 안 닿음, 마지막 2.10 → +5%
    db.execute("INSERT INTO quotes (symbol, ts, trade_date, session, bid, ask) VALUES (?,?,?,?,?,?)",
               ("AAA", iso(T0 - timedelta(seconds=30)), "2026-11-17", "PRE", 0.99, 1.01))      # 스프레드 2.0202%
    db.execute("INSERT INTO quotes (symbol, ts, trade_date, session, bid, ask) VALUES (?,?,?,?,?,?)",
               ("BBB", iso(T0 - timedelta(minutes=20)), "2026-11-17", "PRE", 0.90, 1.10))      # 너무 오래된 호가 → 안 씀
    for sym, kind in [("AAA", "ignition"), ("BBB", "burst"), ("CCC", "burst"), ("CCC", "reaccel")]:
        db.execute("INSERT INTO events (ts, symbol, kind, text) VALUES (?,?,?,?)", (iso(T0), sym, kind, "x"))
    db.commit()

    def q(sql, args=()):
        cur = db.execute(sql, args)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    return q


def day_rows(mq):
    return alertperf.rows(mq, None, iso(T0 - timedelta(hours=1)), iso(T0 + timedelta(hours=1)), kinds=alertperf.ALL_KINDS)


def test_rows_attach_real_spread_only_when_quote_is_fresh(mq):
    by = {(r["symbol"], r["kind"]): r for r in day_rows(mq)}
    assert by[("AAA", "ignition")]["spread"] == pytest.approx(2.0202, abs=1e-3)
    assert by[("BBB", "burst")]["spread"] is None            # 20분 전 호가는 알림 시점 호가가 아님
    assert by[("CCC", "burst")]["spread"] is None


def test_sim_and_net_return(mq):
    by = {(r["symbol"], r["kind"]): r for r in day_rows(mq)}
    assert alertperf.sim_return(by[("AAA", "ignition")]) == 20.0
    assert alertperf.sim_return(by[("BBB", "burst")]) == -10.0
    assert alertperf.sim_return(by[("CCC", "burst")]) == pytest.approx(5.0)       # 안 닿으면 그날 마지막 가격
    assert alertperf.net_return(by[("AAA", "ignition")]) == pytest.approx(20 - 2.0202 - alertperf.FEE_ROUND_TRIP, abs=1e-3)
    assert alertperf.net_return(by[("BBB", "burst")]) is None


def test_stats_accumulate_cumulative_returns(mq):
    st = alertperf.stats(mq, None, today="2026-11-18")
    ig = st["ignition"]
    assert (ig["n"], ig["win"], ig["ret_n"], ig["net_n"]) == (1, 1, 1, 1)
    assert ig["ret_sum"] == 20.0 and ig["net_sum"] == pytest.approx(17.78, abs=0.01)
    bu = st["burst"]
    assert bu["n"] == 2 and bu["ret_n"] == 2 and bu["ret_sum"] == pytest.approx(-5.0) and bu["net_n"] == 0


def test_summary_has_cost_block_and_dedupes_symbols(mq):
    rs = day_rows(mq)
    txt = alertperf.summary_text(rs, alertperf.stats(mq, None, today="2026-11-18"), "2026-11-17")
    assert "💰" in txt
    assert "🔥 데이·프리 점화 오늘 +20.0% → +17.8% (1/1건) · 누적 +20.0% → +17.8%" in txt
    assert "📶 거래 터짐 오늘 -2.5% → 호가 기록 없음 · 누적 -2.5% → 호가 기록 없음" in txt      # (−10 + 5) / 2
    assert "종목 기준 (종목마다 처음 감지된 알림만): 3종목 중 ✅ 적중 1 · ❌ 손절 1" in txt
    top = [ln for ln in txt.splitlines() if ln.startswith("• ")]
    assert len(top) == 3 and sum(ln.startswith("• CCC") for ln in top) == 1      # 같은 종목이 TOP 에 여러 번 나오지 않음
    assert "같은 종목 알림 2건" in next(ln for ln in top if ln.startswith("• CCC"))


def test_missing_quotes_table_does_not_break(tmp_path):
    def broken(sql, args=()):
        if "FROM quotes" in sql:
            raise RuntimeError("no such table: quotes")
        return []
    assert alertperf.quote_spread(broken, "AAA", iso(T0)) is None
