import json
from datetime import datetime, timedelta, timezone

from pennybot.collector import Store
from pennybot.collector.runner import Collector
from pennybot.config import CollectorConfig
from pennybot.precursor import daily_list_text


class Sent:
    def __init__(self):
        self.msgs = []

    def send(self, text, key=None, every=600, buttons=None):
        self.msgs.append((text, buttons))


def make(tmp_path):
    db = str(tmp_path / "m.db")
    st = Store(db)
    sent = Sent()
    col = Collector(None, st, None, CollectorConfig(db_path=db), notifier=sent, link=lambda: "https://x.example")
    col.universe = {"PENX": {"name": "펜엑스", "last": 1.0}}
    return col, st, sent


T0 = datetime(2026, 10, 23, 8, 0, tzinfo=timezone.utc)


def bars_of(px, vol=100000.0):
    return [((T0 + timedelta(minutes=k)).isoformat(), p, p, p, p, vol) for k, p in enumerate(px)]


def test_limit_plan():
    from pennybot.precursor import limit_plan
    px = [0.9] * 3 + [1.0, 1.10, 0.95, 0.85, 0.79, 0.9]                  # 전날 0.9 → 1.0 (+11%, 1분 $10만) 신호
    p = limit_plan(bars_of(px), 0.9, None, 20, 60)
    assert p["trig_k"] == 3 and round(p["limit"], 2) == 0.80 and p["fill_k"] == 7 and round(p["fill_px"], 2) == 0.79
    assert limit_plan(bars_of(px, vol=1000.0), 0.9, None) is None          # 1분 $1천 → 신호 없음
    assert limit_plan(bars_of(px, vol=1000.0), 0.9, 1e5)["trig_k"] == 3   # 1분 회전율 1% → 신호
    assert limit_plan(bars_of(px[:7]), 0.9, None)["fill_k"] is None         # 아직 안 닿음


def test_virtual_limit_order_fill_and_take_profit(tmp_path):
    col, st, sent = make(tmp_path)
    px = [0.9] * 3 + [1.0, 1.10]
    col._vlimit(T0, "2026-10-22", "PENX", 9, 0.9, bars_of(px), None, "PRE")
    assert st.query("SELECT status, round(entry, 2) FROM vsignals")[0] == ("ORDER", 0.8)
    assert sent.msgs[-1][0].startswith("🧪 가상 지정가 주문 (PENX · 펜엑스)") and "$0.8000 에 매수 대기" in sent.msgs[-1][0]
    col._vlimit(T0, "2026-10-22", "PENX", 9, 0.9, bars_of(px + [0.95]), None, "PRE")   # 안 닿음 → 그대로
    assert st.query("SELECT status FROM vsignals")[0][0] == "ORDER" and len(sent.msgs) == 1
    px += [0.95, 0.85, 0.78]
    col._vlimit(T0, "2026-10-22", "PENX", 9, 0.9, bars_of(px), None, "PRE")             # 0.78 → 지정가 0.80 체결 (시가 0.78)
    assert st.query("SELECT status, round(entry, 2) FROM vsignals")[0] == ("OPEN", 0.78)
    assert sent.msgs[-1][0].startswith("🧪 가상 매수 체결 (PENX · 펜엑스)") and "익절 $0.8970" in sent.msgs[-1][0]
    col.universe["PENX"]["last"] = 0.6                                    # −23%: 아직 보유 (손절 −35%)
    col.check_vsignals(T0 + timedelta(minutes=10), "PRE")
    assert st.query("SELECT status FROM vsignals")[0][0] == "OPEN"
    col.universe["PENX"]["last"] = 0.90                                   # +15.4% → 익절
    col.check_vsignals(T0 + timedelta(hours=2), "PRE")
    status, reason = st.query("SELECT status, exit_reason FROM vsignals")[0]
    assert status == "CLOSED" and reason == "익절 +15%"
    text = sent.msgs[-1][0]
    assert text.startswith("🔵 가상 매도 (PENX · 펜엑스) · 익절 +15%") and "최고 +15.4% (매수 1시간 53분 뒤)" in text
    col._vlimit(T0, "2026-10-22", "PENX", 9, 0.9, bars_of(px), None, "PRE")             # 하루 한 번
    assert st.query("SELECT COUNT(*) FROM vsignals")[0][0] == 1


def test_virtual_order_expires_and_late_signal_ignored(tmp_path):
    col, st, sent = make(tmp_path)
    px = [0.9] * 3 + [1.0, 1.10]
    col._vlimit(T0, "2026-10-22", "PENX", 9, 0.9, bars_of(px), None, "PRE")
    col.check_vsignals(T0 + timedelta(minutes=64), "PRE")                # 신호(08:03) + 60분 지남
    assert st.query("SELECT status FROM vsignals")[0][0] == "EXPIRED"
    col2, st2, _ = make(tmp_path / "b")
    col2._vlimit(T0, "2026-10-22", "PENX", 9, 0.9, bars_of(px + [1.1] * 10), None, "PRE")   # 신호가 10분 전 → 무시
    assert st2.query("SELECT COUNT(*) FROM vsignals")[0][0] == 0


def test_virtual_position_closes_after_regular(tmp_path):
    col, st, sent = make(tmp_path)
    px = [0.9] * 3 + [1.0, 1.10, 0.95, 0.78]
    col._vlimit(T0, "2026-10-22", "PENX", 9, 0.9, bars_of(px), None, "REGULAR")
    col.universe["PENX"]["last"] = 0.82
    col.check_vsignals(T0 + timedelta(hours=6), "AFTER")
    assert st.query("SELECT status, exit_reason FROM vsignals")[0] == ("CLOSED", "정규장 마감 정리")


def test_daily_list_text(tmp_path):
    col, st, sent = make(tmp_path)
    st.put_precursor_daily([{"d0": "2026-10-22", "symbol": "PENX", "score": 9, "close": 0.5, "created": "x",
                             "signals": json.dumps(["crashed", "spiked", "vol_up", "vol_up_strong", "accum"]),
                             "metrics": json.dumps({"dd60": -80, "vol_ratio": 5.0, "last_spike_d": "2026-10-01", "volume": 1e6})}])
    st.put_stock_info([{"symbol": "PENX", "name": "펜엑스", "shares": 1e7, "list_date": None, "updated": "x"}])
    st.put_stock_tags([{"d0": "2026-10-22", "symbol": "PENX", "tags": json.dumps(["ATM 진행"]), "info": "{}"}])
    text = daily_list_text(str(tmp_path / "m.db"), 5, "https://x.example")
    assert text.startswith("📋 오늘 급등 전 감시 목록 (2026-10-22 종가 기준)")
    assert "1. PENX" in text and "9점" in text and "회전율 10.0%" in text and "최근 급등 10-01" in text
    assert "🏷 거래량 폭발 · ATM 진행 · 매집 의심" in text


def test_tier():
    from pennybot.precursor import tier
    assert tier({"vol_ratio": 6, "dd60": -60, "volume": 1e6}, 5e6, 1.5) == 3      # 회전율 20% · $2 미만
    assert tier({"vol_ratio": 6, "dd60": -60, "volume": 1e6}, 5e7, 1.5) == 0      # 회전율 2% · 발행 많음
    assert tier({"vol_ratio": 12, "dd60": -70}, 4e6) == 2
    assert tier({"vol_ratio": 12, "dd60": -10}, 4e6) == 1
    assert tier({"vol_ratio": 6, "dd60": -60}, 4e6) == 1
    assert tier({"vol_ratio": 6, "dd60": -10}, 4e6) == 0
    assert tier({"vol_ratio": 50, "dd60": -80}, 2e7) == 0      # 발행주식 많음
    assert tier({"vol_ratio": 50, "dd60": -80}, None) == 0     # 발행주식 모름


def test_daily_list_puts_picks_first(tmp_path):
    st = Store(str(tmp_path / "m.db"))
    met = lambda vr, dd: json.dumps({"dd60": dd, "vol_ratio": vr, "volume": 1e5})
    st.put_precursor_daily([
        {"d0": "2026-10-22", "symbol": "HIGH", "score": 10, "close": 1, "created": "x", "signals": "[]", "metrics": met(2, -60)},
        {"d0": "2026-10-22", "symbol": "PICK", "score": 4, "close": 1, "created": "x", "signals": "[]", "metrics": met(15, -70)}])
    st.put_stock_info([{"symbol": s, "name": None, "shares": 3e6, "list_date": None, "updated": "x"} for s in ("HIGH", "PICK")])
    text = daily_list_text(str(tmp_path / "m.db"), 5)
    assert "🎯 급등 후보 1종목 (★★★ 0 · ★★ 1)" in text
    assert text.index("1. PICK") < text.index("2. HIGH") and "PICK  · ★★" in text


def test_live_money_signal_and_ask_depth():
    from datetime import datetime, timedelta, timezone
    from pennybot.precursor import alert_text, ask_depth, live
    t0 = datetime(2026, 10, 23, 8, 0, tzinfo=timezone.utc)
    bars = [((t0 + timedelta(minutes=k)).isoformat(), 1.0, 1.0, 1.0, 1.0, 100.0) for k in range(60)]
    bars += [((t0 + timedelta(minutes=60 + k)).isoformat(), 1.06, 1.08, 1.05, 1.07, 20000.0) for k in range(3)]
    now = t0 + timedelta(minutes=62)
    assert "money" in live(bars, 1.0, now)["signals"]                       # 3분 $6.4만 · +7%
    small = [b[:5] + (2000.0,) for b in bars]
    assert "money" not in live(small, 1.0, now)["signals"]                  # $6.4천
    assert "money" in live(small, 1.0, now, shares=1e6)["signals"]          # 3분 회전율 0.6%
    assert "money" not in live(bars, 1.06, now)["signals"]                  # +1% → 안 켜짐
    ob = {"asks": [{"price": "1.08", "volume": "10000"}, {"price": "1.30", "volume": "99999"}]}
    assert ask_depth(ob, 1.07) == 10800.0                                   # +10% 밖 호가는 뺀다
    lv = live(bars, 1.0, now)
    lv["metrics"]["ask10"] = 10800.0
    text = alert_text("PENX", None, {"score": 7, "signals": [], "metrics": {"dd60": -80, "spikes60": 1, "vol_ratio": 5}},
                      lv, "프리마켓")
    assert "호가: 매도 +10% 안" in text and "얇음 ✅" in text


def test_premarket_burst_buys_at_once(tmp_path):
    """동부 06~09시에 처음 터지면 지정가 대신 바로 매수, 익절 +30%."""
    col, st, sent = make(tmp_path)
    t7 = datetime(2026, 10, 23, 11, 0, tzinfo=timezone.utc)              # 07:00 ET (서머타임)
    bars = [((t7 + timedelta(minutes=k)).isoformat(), p, p, p, p, 100000.0) for k, p in enumerate([0.9] * 3 + [1.0])]
    col._vlimit(t7, "2026-10-22", "PENX", 9, 0.9, bars, None, "PRE")
    status, entry, tp = st.query("SELECT status, round(entry, 3), tp FROM vsignals")[0]
    assert (status, entry, tp) == ("OPEN", 1.005, 30.0)
    assert sent.msgs[-1][0].startswith("🧪 가상 매수 체결") and "익절 $1.3065 (+30%)" in sent.msgs[-1][0]
    col.universe["PENX"]["last"] = 1.2                                    # +19%: +15% 넘었지만 이 매매는 +30% 익절
    col.check_vsignals(t7 + timedelta(minutes=20), "PRE")
    assert st.query("SELECT status FROM vsignals")[0][0] == "OPEN"
    col.universe["PENX"]["last"] = 1.31
    col.check_vsignals(t7 + timedelta(minutes=40), "PRE")
    assert st.query("SELECT status, exit_reason FROM vsignals")[0] == ("CLOSED", "익절 +30%")
