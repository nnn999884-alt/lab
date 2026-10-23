from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from pennybot.trigger import signals, watch

ET = ZoneInfo("America/New_York")


def bar(dt, c, v):
    return (dt.replace(tzinfo=ET).astimezone(timezone.utc).isoformat(), c, c * 1.001, c * 0.999, c, v)


def test_signals_a_b_c():
    d0, d1, c0 = "2026-09-14", "2026-09-15", 0.50
    t = datetime(2026, 9, 14, 20, 0)
    bars = [bar(t + timedelta(minutes=10 * k), 0.50, 200) for k in range(30)]          # 데이마켓 조용 (5시간)
    t2 = t + timedelta(minutes=300)
    bars += [bar(t2 + timedelta(minutes=k), 0.50 + 0.002 * k, 3000) for k in range(20)]  # 거래량 깨어남 + 조금씩 상승
    bars += [bar(t2 + timedelta(minutes=25), 0.56, 20000)]                              # +12% (거래대금 충분)
    bars += [bar(datetime(2026, 9, 15, 9, 31), 0.60, 50000)]                            # 정규장 (신호 대상 아님)
    s = signals(bars, d0, d1, c0)
    assert s["A"] and s["B"] and s["C"]
    assert s["C"][0] < s["A"][0]                       # 전조 신호가 +10% 보다 먼저
    assert abs(s["A"][1] - 0.56) < 1e-9


def test_no_signal_in_regular_session_only():
    d0, d1 = "2026-09-14", "2026-09-15"
    bars = [bar(datetime(2026, 9, 15, 9, 31), 0.80, 100000)]
    assert signals(bars, d0, d1, 0.5) == {"A": None, "B": None, "C": None}


def test_watch_condition():
    assert watch({"price": 0.6, "ret_30": -40, "range_avg": 15})
    assert not watch({"price": 1.2, "ret_30": -40, "range_avg": 15})
