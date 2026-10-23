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


def test_strategy_plans():
    from pennybot.vstrat import STRATEGIES, plan, vwap_series
    px = [0.9] * 3 + [1.0, 1.10, 0.95, 0.85, 0.9]                        # 04:03 ET 신호 (+11%, 1분 $10만)
    bars = bars_of(px)
    s1 = plan(bars, 0.9, None, STRATEGIES["S1"])                          # VWAP −10%
    assert s1["trig_k"] == 3 and s1["fill_k"] == 6 and abs(s1["fill_px"] - 0.85) < 1e-9
    assert abs(vwap_series(bars)[5] * 0.9 - 0.8625) < 1e-3
    s4 = plan(bars, 0.9, None, STRATEGIES["S4"])                          # 신호가 1.0 −15% = 0.85
    assert s4["fill_k"] == 6 and abs(s4["fill_px"] - 0.85) < 1e-9
    s3 = plan(bars, 0.9, None, STRATEGIES["S3"])                          # 고점 1.10 −30% = 0.77 → 아직
    assert s3["fill_k"] is None and abs(s3["limit"] - 0.77) < 1e-9
    assert plan(bars, 0.9, None, STRATEGIES["S2"]) is None                 # 04시 → 06~09시 아님
    t7 = datetime(2026, 10, 23, 11, 0, tzinfo=timezone.utc)               # 07:00 ET
    b7 = [((t7 + timedelta(minutes=k)).isoformat(), x, x, x, x, 1e5) for k, x in enumerate(px)]
    s2 = plan(b7, 0.9, None, STRATEGIES["S2"])
    assert s2["fill_k"] == 3 and abs(s2["fill_px"] - 1.005) < 1e-9
    t_reg = datetime(2026, 10, 23, 14, 0, tzinfo=timezone.utc)            # 10:00 ET 정규장 신호 → 아무도 안 삼
    reg = [((t_reg + timedelta(minutes=k)).isoformat(), x, x, x, x, 1e5) for k, x in enumerate(px)]
    assert all(plan(reg, 0.9, None, st) is None for st in STRATEGIES.values())
    assert plan(bars_of(px, vol=1000.0), 0.9, None, STRATEGIES["S1"]) is None          # 1분 $1천 → 신호 없음
    assert plan(bars_of(px, vol=1000.0), 0.9, 1e5, STRATEGIES["S1"])["trig_k"] == 3    # 1분 회전율 1% → 신호


def only(col, *keys):
    col.cfg.vsignal_strategies = list(keys)
    return col


def test_virtual_limit_order_fill_and_take_profit(tmp_path):
    col, st, sent = make(tmp_path)
    only(col, "S1")
    px = [0.9] * 3 + [1.0, 1.10]
    col._vtrade(T0, "2026-10-22", "PENX", 9, 0.9, bars_of(px), None, "PRE")
    assert st.query("SELECT status, round(entry, 3), strat FROM vsignals")[0] == ("ORDER", 0.864, "S1")   # VWAP 0.96 × 0.9
    assert sent.msgs[-1][0].startswith("🧪 [전략1 VWAP 눌림] 가상 지정가 주문 (PENX · 펜엑스)") and "VWAP −10%" in sent.msgs[-1][0]
    col._vtrade(T0, "2026-10-22", "PENX", 9, 0.9, bars_of(px + [0.95]), None, "PRE")   # 안 닿음 → 지정가만 VWAP 따라
    assert st.query("SELECT status, round(entry, 3) FROM vsignals")[0] == ("ORDER", 0.863) and len(sent.msgs) == 1
    px += [0.95, 0.85]
    col._vtrade(T0, "2026-10-22", "PENX", 9, 0.9, bars_of(px), None, "PRE")             # 0.85 → 체결
    assert st.query("SELECT status, round(entry, 2) FROM vsignals")[0] == ("OPEN", 0.85)
    assert sent.msgs[-1][0].startswith("🧪 [전략1 VWAP 눌림] 가상 매수 체결") and "익절 $0.9350 (+10%)" in sent.msgs[-1][0]
    col.universe["PENX"]["last"] = 0.6                                    # −29%: 아직 보유 (손절 −35%)
    col.check_vsignals(T0 + timedelta(minutes=10), "PRE")
    assert st.query("SELECT status FROM vsignals")[0][0] == "OPEN"
    col.universe["PENX"]["last"] = 0.94                                   # +10.6% → 익절
    col.check_vsignals(T0 + timedelta(hours=2), "PRE")
    status, reason = st.query("SELECT status, exit_reason FROM vsignals")[0]
    assert status == "CLOSED" and reason == "익절 +10%"
    text = sent.msgs[-1][0]
    assert text.startswith("🔵 [전략1 VWAP 눌림] 가상 매도 (PENX · 펜엑스) · 익절 +10%") and "최고 +10.6% (매수 1시간 54분 뒤)" in text
    col._vtrade(T0, "2026-10-22", "PENX", 9, 0.9, bars_of(px), None, "PRE")             # 하루 한 번
    assert st.query("SELECT COUNT(*) FROM vsignals")[0][0] == 1


def test_strategies_trade_independently(tmp_path):
    col, st, sent = make(tmp_path)                                        # 넷 다 켜짐 (기본)
    px = [0.9] * 3 + [1.0, 1.10, 0.95, 0.85]
    col._vtrade(T0, "2026-10-22", "PENX", 9, 0.9, bars_of(px[:5]), None, "PRE")
    assert sorted(st.query("SELECT strat, status FROM vsignals")) == [("S1", "ORDER"), ("S3", "ORDER"), ("S4", "ORDER")]
    col._vtrade(T0, "2026-10-22", "PENX", 9, 0.9, bars_of(px), None, "PRE")             # 0.85 → ①·④ 체결, ③ 대기
    assert sorted(st.query("SELECT strat, status FROM vsignals")) == [("S1", "OPEN"), ("S3", "ORDER"), ("S4", "OPEN")]
    col.universe["PENX"]["last"] = 0.94                                   # +10.6%: ① 익절(+10), ④ 아직(+15)
    col.check_vsignals(T0 + timedelta(minutes=20), "PRE")
    assert sorted(st.query("SELECT strat, status FROM vsignals")) == [("S1", "CLOSED"), ("S3", "ORDER"), ("S4", "OPEN")]


def test_premarket_chase_buys_at_once(tmp_path):
    col, st, sent = make(tmp_path)
    only(col, "S2")
    t7 = datetime(2026, 10, 23, 11, 0, tzinfo=timezone.utc)
    bars = [((t7 + timedelta(minutes=k)).isoformat(), p, p, p, p, 1e5) for k, p in enumerate([0.9] * 3 + [1.0])]
    col._vtrade(t7, "2026-10-22", "PENX", 9, 0.9, bars, None, "PRE")
    assert st.query("SELECT status, round(entry, 3), tp, sl, strat FROM vsignals")[0] == ("OPEN", 1.005, 20.0, 35.0, "S2")
    assert sent.msgs[-1][0].startswith("🧪 [전략2 프리 추격] 가상 매수 체결") and "(+20%)" in sent.msgs[-1][0]


def test_virtual_order_expires_and_late_signal_ignored(tmp_path):
    col, st, sent = make(tmp_path)
    only(col, "S1", "S4")
    px = [0.9] * 3 + [1.0, 1.10]
    col._vtrade(T0, "2026-10-22", "PENX", 9, 0.9, bars_of(px), None, "PRE")
    col.check_vsignals(T0 + timedelta(minutes=34), "PRE")                # ④ 는 30분, ① 은 60분
    assert sorted(st.query("SELECT strat, status FROM vsignals")) == [("S1", "ORDER"), ("S4", "EXPIRED")]
    col.check_vsignals(T0 + timedelta(minutes=64), "PRE")
    assert st.query("SELECT status FROM vsignals WHERE strat='S1'")[0][0] == "EXPIRED"
    col2, st2, _ = make(tmp_path / "b")
    col2._vtrade(T0, "2026-10-22", "PENX", 9, 0.9, bars_of(px + [1.1] * 10), None, "PRE")   # 신호가 10분 전 → 무시
    assert st2.query("SELECT COUNT(*) FROM vsignals")[0][0] == 0


def test_virtual_position_closes_after_regular(tmp_path):
    col, st, sent = make(tmp_path)
    only(col, "S1")
    px = [0.9] * 3 + [1.0, 1.10, 0.95, 0.80]
    col._vtrade(T0, "2026-10-22", "PENX", 9, 0.9, bars_of(px), None, "PRE")
    assert st.query("SELECT status FROM vsignals")[0][0] == "OPEN"
    col.universe["PENX"]["last"] = 0.82
    col.check_vsignals(T0 + timedelta(hours=10), "AFTER")
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


def test_day_bars_backfills_once_then_merges(tmp_path):
    col, st, sent = make(tmp_path)
    old = [((T0 + timedelta(minutes=k)).isoformat(), 1.0) for k in range(5)]

    class C:
        calls = 0

        def candles(self, sym, interval, count, before=None):
            C.calls += 1
            return {"candles": [{"timestamp": t, "openPrice": p, "highPrice": p, "lowPrice": p, "closePrice": p, "volume": 10}
                                for t, p in reversed(old)], "nextBefore": None}
    col.c = C()
    start = T0.isoformat()
    recent = bars_of([1.1, 1.2])[:2]
    recent = [((T0 + timedelta(minutes=5 + k)).isoformat(),) + b[1:] for k, b in enumerate(recent)]
    full = col._day_bars("PENX", recent, start)
    assert len(full) == 7 and full[0][0] == start and C.calls == 1
    nxt = [((T0 + timedelta(minutes=7)).isoformat(), 1.3, 1.3, 1.3, 1.3, 5.0)]
    assert len(col._day_bars("PENX", nxt, start)) == 8 and C.calls == 1   # 두 번째부터는 이어 붙이기만


def test_hold_strategies():
    from pennybot.vstrat import STRATEGIES, plan
    # 04:03 신호 1.0 → 1.30 고점 → 눌림 1.10 (−15%), 거래대금 유지
    px = [0.9] * 3 + [1.0, 1.2, 1.3, 1.2, 1.12, 1.10, 1.08, 1.09, 1.10, 1.11]
    bars = bars_of(px)
    s5 = plan(bars, 0.9, None, STRATEGIES["S5"])                          # 5분 뒤(k=8) 1.10 ≤ 1.30×0.9 → 바로
    assert s5["fill_k"] == 8 and abs(s5["fill_px"] - 1.10 * 1.005) < 1e-9
    assert plan(bars[:8], 0.9, None, STRATEGIES["S5"])["waiting"]          # 아직 5분 안 됨
    thin = bars[:4] + [b[:5] + (100.0,) for b in bars[4:]]                # 거래대금 말라버림 → 안 삼
    assert plan(thin, 0.9, None, STRATEGIES["S5"]) is None
    s6 = plan(bars, 0.9, None, STRATEGIES["S6"])                          # 4분 뒤(k=7) 1.12 지정가 → 다음 분 1.10 체결
    assert s6["fill_k"] == 8 and abs(s6["fill_px"] - 1.10) < 1e-9
    up = bars_of(px[:8] + [1.35])                                         # 그 다음 분 고점 1.30 돌파 → 시장가 (시가 1.35)
    s6b = plan(up, 0.9, None, STRATEGIES["S6"])
    assert s6b["fill_k"] == 8 and abs(s6b["fill_px"] - 1.35 * 1.005) < 1e-9
    flat = bars_of(px[:8] + [1.15, 1.16, 1.15])                           # 3분 동안 안 닿음 → 취소
    assert plan(flat, 0.9, None, STRATEGIES["S6"]) is None


def test_archive_saves_watched_symbols(tmp_path, monkeypatch):
    import pennybot.anatomy as an
    col, st, sent = make(tmp_path)
    st.put_precursor_daily([{"d0": "2026-10-22", "symbol": "PENX", "score": 9, "close": 1, "created": "x", "signals": "[]", "metrics": "{}"}])
    st.put_precursor_live([{"d0": "2026-10-22", "symbol": "PENX", "ts": "x", "signals": "[]", "metrics": "{}", "alerted": None},
                           {"d0": "2026-10-22", "symbol": "ABCD", "ts": "x", "signals": "[]", "metrics": "{}", "alerted": None}])
    got = []

    def fake(client, rdb, sym, d0, d):
        got.append((sym, d0, d))
        rdb.put("bars_1m_done", [{"symbol": sym, "d": f"full:{d}"}])
        return [("t", 1, 1, 1, 1, 1)]
    monkeypatch.setattr(an, "fetch_day", fake)
    assert col.archive_1m("2026-10-22", "2026-10-23") == 2
    assert sorted(got) == [("ABCD", "2026-10-22", "2026-10-23"), ("PENX", "2026-10-22", "2026-10-23")]
    assert col.archive_1m("2026-10-22", "2026-10-23") == 0                # 이미 받은 날은 건너뜀
