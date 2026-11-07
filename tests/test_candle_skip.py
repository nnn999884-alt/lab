from types import SimpleNamespace

from pennybot.collector.runner import Collector


def test_candles_skipped_until_new_trade():
    c = Collector.__new__(Collector)
    calls = []
    c.c = SimpleNamespace(candles=lambda s, tf, n: calls.append(s) or {"candles": [{"x": 1}]})
    c._trade_ts = {"PEN": "2026-11-06T10:00:00+09:00"}
    assert c._candles_if_traded("PEN") == [{"x": 1}] and len(calls) == 1
    assert c._candles_if_traded("PEN") is Collector._SKIP and len(calls) == 1      # 체결 그대로 → 안 받음
    c._trade_ts["PEN"] = "2026-11-06T10:00:20+09:00"                             # 새 체결
    assert c._candles_if_traded("PEN") == [{"x": 1}] and len(calls) == 2
    c._rt_tick_t = {"PEN": 123}                                                  # 웹소켓 체결도 새 체결
    assert c._candles_if_traded("PEN") == [{"x": 1}] and len(calls) == 3
