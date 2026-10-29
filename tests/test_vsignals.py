import json
from datetime import datetime, timedelta, timezone

from pennybot.collector import Store
from pennybot.collector.runner import Collector
from pennybot.config import CollectorConfig
from pennybot.vstrat import STRATEGIES
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


def test_virtual_limit_order_fill_and_take_profit(tmp_path, monkeypatch):
    monkeypatch.setitem(STRATEGIES["S1"], "tp", 10)
    col, st, sent = make(tmp_path)
    only(col, "S1")
    px = [0.9] * 3 + [1.0, 1.10]
    col._vtrade(T0, "2026-10-22", "PENX", 9, 0.9, bars_of(px), None, "PRE")
    assert st.query("SELECT status, round(entry, 3), strat FROM vsignals")[0] == ("ORDER", 0.864, "S1")   # VWAP 0.96 × 0.9
    assert sent.msgs[-1][0].startswith("🧪 [전략1 VWAP 눌림] 가상 지정가 주문 · PENX(펜엑스)") and "VWAP −10%" in sent.msgs[-1][0]
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
    assert text.startswith("🔵 [전략1 VWAP 눌림] 가상 매도 · PENX(펜엑스) · 익절 +10%") and "최고 +10.6% (매수 1시간 54분 뒤)" in text
    col._vtrade(T0, "2026-10-22", "PENX", 9, 0.9, bars_of(px), None, "PRE")             # 하루 한 번
    assert st.query("SELECT COUNT(*) FROM vsignals")[0][0] == 1


def test_strategies_trade_independently(tmp_path, monkeypatch):
    monkeypatch.setitem(STRATEGIES["S1"], "tp", 10)
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


def test_virtual_position_closes_at_after_hours_end(tmp_path):
    col, st, sent = make(tmp_path)
    only(col, "S1")
    px = [0.9] * 3 + [1.0, 1.10, 0.95, 0.80]
    col._vtrade(T0, "2026-10-22", "PENX", 9, 0.9, bars_of(px), None, "PRE")
    assert st.query("SELECT status FROM vsignals")[0][0] == "OPEN"
    col.universe["PENX"]["last"] = 0.82
    col.check_vsignals(T0 + timedelta(hours=10), "AFTER")                # 애프터장 중엔 그대로
    assert st.query("SELECT status FROM vsignals")[0][0] == "OPEN"
    del col.universe["PENX"]                                              # $0.20 아래로 빠져 유니버스에서 사라져도
    st.put_prices([{"symbol": "PENX", "ts": "2026-10-23T23:59:00+00:00", "trade_date": "2026-10-23", "session": "AFTER",
                    "last": 0.81, "price_ts": None}])
    col.check_vsignals(T0 + timedelta(hours=16), "CLOSED", True)         # 애프터장 마감 → 마지막 가격으로 정리
    assert st.query("SELECT status, exit_reason, exit_price FROM vsignals")[0] == ("CLOSED", "애프터장 마감 정리", 0.81)


def test_alert_grade():
    from pennybot.precursor import alert_grade
    t7 = "2026-10-23T11:10:00+00:00"
    assert alert_grade(t7, 3, 1.5, 80) == ("A", ["프리 06~09시", "★★★", "전날 거래량 50배+"])
    assert alert_grade("2026-10-23T08:10:00+00:00", 2, 3.0, 10) == ("C", [])
    assert alert_grade("2026-10-23T08:10:00+00:00", 2, 1.0, 80) == ("B", ["전날 거래량 50배+"])
    assert alert_grade("2026-10-23T08:10:00+00:00", 2, 8.0, 10)[0] == "C"


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
    assert "호가 +10%안" in text and "얇음 ✅" in text


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


def test_realtime_trigger_uses_volume_not_price(tmp_path):
    col, st, sent = make(tmp_path)
    col._rt_seeded = {"PENX"}
    col._rt_shares = {"PENX": 1e7}
    t = 1_800_000_000.0                                                   # 분 경계와 무관한 고정 시각
    for k in range(30):                                                   # 평소: 분당 1,000주
        col._rt_min.setdefault("PENX", {})[int(t // 60) - 31 + k] = 1000.0
    col._on_trade("PENX", 1.0, 20000, t)                                  # 60초 $2만 · 회전율 0.2% → 아직
    assert col._rt_q.empty()
    col._on_trade("PENX", 0.98, 40000, t + 20)                            # 60초 $5.9만, 6만주 (평소 60배) → 신호 (가격은 내려도)
    job = col._rt_q.get_nowait()
    assert job[0] == "alert" and job[1] == "PENX" and job[2] >= 5e4
    col._on_trade("PENX", 1.0, 90000, t + 30)                             # 10분 안 다시 → 안 보냄
    assert col._rt_q.empty()
    # 원래 거래가 많은 종목: 금액은 커도 평소의 5배가 안 되면 안 보냄
    col._rt_seeded.add("BIGV")
    for k in range(30):
        col._rt_min.setdefault("BIGV", {})[int(t // 60) - 31 + k] = 100000.0
    col._on_trade("BIGV", 50.0, 2000, t)                                  # $10만이지만 평소 분당 10만주
    assert col._rt_q.empty()
    col._on_trade("NEWX", 1.0, 100000, t)                                 # 평소 거래량을 아직 안 채운 종목 → 안 보냄
    assert col._rt_q.empty()


def test_rank_scan_subscribes_newcomers(tmp_path):
    col, st, sent = make(tmp_path)
    col.universe = {"PENX": {"name": "펜엑스", "last": 1.0}, "NEWX": {"name": "뉴", "last": 1.0}}
    st.put_precursor_daily([{"d0": "2026-10-22", "symbol": "PENX", "score": 7, "close": 1.0, "created": "x", "signals": "[]", "metrics": "{}"}])

    class C:
        def rankings(self, type_, duration="realtime", **kw):
            return {"rankings": [{"symbol": "AAPL"}, {"symbol": "PENX"}, {"symbol": "NEWX"}]}

        def candles(self, sym, interval, count, before=None):
            return {"candles": []}
    col.c = C()
    col._rt_syms = {"PENX"}
    assert col.fast_scan(T0, None, "PRE") == ["NEWX"]                     # AAPL 은 유니버스 밖, PENX 는 이미 구독
    assert "NEWX" in col._rt_syms and ("seed", "NEWX") in list(col._rt_q.queue)
    assert col.fast_scan(T0, None, "PRE") == []


def test_realtime_alert_sends(tmp_path):
    col, st, sent = make(tmp_path)
    st.put_precursor_daily([{"d0": "2026-10-22", "symbol": "PENX", "score": 7, "close": 1.0, "created": "x", "signals": "[]",
                             "metrics": json.dumps({"dd60": -80, "vol_ratio": 60})}])
    st.put_daily_bars([{"symbol": "PENX", "d": "2026-10-22", "open": 1, "high": 1, "low": 1, "close": 1.0, "volume": 1}])

    class C:
        def candles(self, sym, interval, count, before=None):
            return {"candles": [{"timestamp": (T0 + timedelta(minutes=k)).isoformat(), "openPrice": 1, "highPrice": 1.1,
                                 "lowPrice": 1, "closePrice": 1.08, "volume": 1000} for k in range(3)]}

        def orderbook(self, sym):
            return {"asks": []}

    class Cal:
        def classify(self, now):
            return (None, "PRE")

        def bounds(self, d, s):
            return None
    col.c, col.cal = C(), Cal()
    col.clock = lambda: T0 + timedelta(minutes=3)                        # 04:03 ET
    col._rt_shares = {"PENX": 1e6}
    col._rt_alert(st, "PENX", 80000.0, 70000.0, 1.08)
    assert len(sent.msgs) == 1 and "⚡ 실시간 1분 $80K · 회전율 7.0%" in sent.msgs[0][0]
    col._rt_alert(st, "PENX", 90000.0, 80000.0, 1.10)                    # 하루 한 번
    assert len(sent.msgs) == 1


def test_presurge_rule():
    from pennybot.precursor import presurge
    t7 = "2026-10-23T11:10:00+00:00"                                      # 07:10 ET
    assert presurge(t7, 0, 1.5, 3, 12, "PRE") == (True, ["$5 미만"])
    assert presurge(t7, 0, 3.0, 80, 12, "PRE")[0]                         # 전날 거래량 80배
    assert not presurge(t7, 0, 6.0, 10, 12, "PRE")[0]                     # $6 · 10배 · ★ 없음 → 아무 사유도 없음
    assert not presurge(t7, 3, 1.5, 80, 3, "PRE")[0]                      # +3% (아직 +5% 아님)
    assert presurge("2026-10-23T14:10:00+00:00", 3, 1.5, 80, 12, "DAY")[0]       # 정규장만 아니면 시간대 상관없음
    assert presurge("2026-10-23T14:10:00+00:00", 3, 1.5, 80, 12, "REGULAR")[0]  # 정규장은 여전히 막힘
    assert presurge(t7, 3, 1.5, 80, 12, "AFTER")[0]                   # 애프터마켓도 막힘


def test_after_movers_catches_slow_cumulative_move(tmp_path):
    """애프터마켓 중 정규장 종가 대비 천천히(1분 급등 기준엔 안 걸리게) 누적으로 많이 움직인 종목을 잡는다."""
    from datetime import date
    col, st, sent = make(tmp_path)
    d0 = date(2026, 10, 23)
    st.put_prices([
        {"symbol": "BRAI", "ts": "2026-10-23T20:00:00+00:00", "trade_date": "2026-10-23", "session": "REGULAR",
         "last": 2.15, "price_ts": None},
        {"symbol": "BRAI", "ts": "2026-10-23T21:35:00+00:00", "trade_date": "2026-10-23", "session": "AFTER",
         "last": 2.73, "price_ts": None},
        {"symbol": "FLAT", "ts": "2026-10-23T20:00:00+00:00", "trade_date": "2026-10-23", "session": "REGULAR",
         "last": 3.00, "price_ts": None},
        {"symbol": "FLAT", "ts": "2026-10-23T21:35:00+00:00", "trade_date": "2026-10-23", "session": "AFTER",
         "last": 3.10, "price_ts": None},          # +3.3%, 기준(10%) 밖
    ])
    now = datetime(2026, 10, 23, 21, 36, tzinfo=timezone.utc)
    col.poll_after_movers(now, d0)
    assert sent.msgs[-1][0].startswith("🌆 애프터마켓 누적 변동 · BRAI")
    assert "+27" in sent.msgs[-1][0]
    assert not any("FLAT" in m[0] for m in sent.msgs)
    assert len(sent.msgs) == 1                     # FLAT 은 안 보냄
    col.poll_after_movers(now, d0)                 # 같은 날 두 번째는 다시 안 보냄
    assert len(sent.msgs) == 1


def test_burst_sends_signal_or_presurge(tmp_path):
    col, st, sent = make(tmp_path)

    class C:
        def orderbook(self, sym):
            return {"asks": []}
    col.c = C()
    lv = {"signals": ["burst"], "metrics": {"price": 1.2, "chg": 20.0, "dollar_3m": 9e4, "dollar_today": 3e5,
                                            "vol_x": 8, "grade": "A", "grade_why": []}}
    t4 = datetime(2026, 10, 23, 8, 0, tzinfo=timezone.utc)                # 정규장 중 → 짧은 신호만 (급등 전조는 정규장엔 안 감)
    col._alert(st, t4, "REGULAR", "PENX", 7, "[]", json.dumps({"dd60": -80, "vol_ratio": 60}), lv, 0, ["⚡ 실시간"])
    assert sent.msgs[-1][0].startswith(("📶 거래 터짐", "🚨 급등 전조"))
    t7 = datetime(2026, 10, 23, 11, 0, tzinfo=timezone.utc)               # 프리마켓(시간대 상관없음) + $1 + 60배 → 급등 전조
    col._alert(st, t7, "PRE", "PENX", 7, "[]", json.dumps({"dd60": -80, "vol_ratio": 60, "spikes60": 2}), lv, 0, ["⚡ 실시간"])
    assert sent.msgs[-1][0].startswith("🚨 급등 전조 · PENX(펜엑스)") and "+30% 선도달 약 50%" in sent.msgs[-1][0]


def test_report_counts_rejected_symbols_once():
    from pennybot.trading.report import _short
    assert _short("토스 거래대금 상위, 이미 +30.2% (늦음)") == "이미 많이 오름"
    assert _short("호재 공시, 고점 대비 밀림") == "고점 대비 밀림"
    assert _short("가격범위 밖 $9.5400") == "가격범위 밖"
    assert _short("희석 위험 100점 (ATM 진행 중, 증자 등록)") == "희석 위험"


def test_news_halt_alert_and_resume(tmp_path):
    col, st, sent = make(tmp_path)
    st.put_precursor_daily([{"d0": "2026-10-22", "symbol": "PENX", "score": 7, "close": 1.0, "created": "x", "signals": "[]", "metrics": "{}"}])

    class Short:
        rows = [{"symbol": "PENX", "halt_ts": "2026-10-23T08:40:00-04:00", "reason": "T1", "reason_ko": "뉴스·재개 대기", "resume_ts": None},
                {"symbol": "PENX", "halt_ts": "2026-10-23T09:00:00-04:00", "reason": "LUDP", "reason_ko": "변동성 정지(LULD)", "resume_ts": None}]

        def halts(self):
            return self.rows
    col.short = Short()
    col.clock = lambda: T0
    col.poll_halts()
    assert len(sent.msgs) == 1 and sent.msgs[0][0].startswith("🛑 뉴스 대기 거래정지 (PENX · 펜엑스)") and "08:40 ET" in sent.msgs[0][0]
    assert "PENX" in col._fast_hot[1]                                    # 실시간 구독에 들어감
    col.poll_halts()                                                      # 같은 정지 → 다시 안 보냄
    assert len(sent.msgs) == 1
    Short.rows = [dict(Short.rows[0], resume_ts="2026-10-23T09:50:00-04:00")]
    col.poll_halts()
    assert sent.msgs[-1][0].startswith("▶ PENX 펜엑스 거래 재개 예정 09:50 ET")


def test_pre_open_watch_once(tmp_path):
    col, st, sent = make(tmp_path)
    col.universe = {"PENX": {"name": "펜엑스", "last": 1.3}, "FLAT": {"name": "평평", "last": 1.0}, "BRST": {"name": "터짐", "last": 1.02}}
    st.put_precursor_daily([{"d0": "2026-10-22", "symbol": s, "score": 7, "close": 1.0, "created": "x", "signals": "[]", "metrics": "{}"}
                            for s in col.universe])
    st.put_daily_bars([{"symbol": s, "d": "2026-10-22", "open": 1, "high": 1, "low": 1, "close": 1.0, "volume": 1} for s in col.universe])
    st.put_precursor_live([{"d0": "2026-10-22", "symbol": "BRST", "ts": "x", "signals": "[]", "metrics": "{}",
                            "alerted": "2026-10-23T03:00:00+00:00"}])

    class Cal:
        def bounds(self, d, s):
            return (datetime(2026, 10, 23, 0, 0, tzinfo=timezone.utc),)
    col.cal = Cal()
    t = datetime(2026, 10, 23, 7, 50, tzinfo=timezone.utc)               # 03:50 ET → 아직
    assert col.pre_open_watch(t, "d") == []
    t = datetime(2026, 10, 23, 7, 56, tzinfo=timezone.utc)               # 03:56 ET
    assert col.pre_open_watch(t, "d") == ["PENX", "BRST"]                 # +30% 와 데이마켓 거래 터짐, FLAT 은 빠짐
    assert sent.msgs[-1][0].startswith("🌅 프리마켓 개장(04:00 ET) 주목")
    assert col.pre_open_watch(t, "d") == []                               # 하루 한 번


def test_poll_steady_flow_flags_persistent_but_not_spiky_symbol(tmp_path):
    """한 번에 크게 안 터지고 거래대금 랭킹에 꾸준히 올라와 있는 종목('사부작사부작')을 잡는다 —
    랭킹에 거의 안 든 종목은 걸리지 않는다."""
    col, st, sent = make(tmp_path)
    col.universe = {"STDY": {"name": "꾸준이"}, "FLAT": {"name": "평평이"}}
    base = datetime(2026, 10, 23, 14, 0, tzinfo=timezone.utc)
    rows = []
    for i in range(12):                              # 2시간, 10분 간격
        ts = (base + timedelta(minutes=i * 10)).isoformat()
        if i % 2 == 0:                                # STDY 는 절반 이상 상위권
            rows.append({"ts": ts, "trade_date": "2026-10-23", "session": "REGULAR", "type": "MARKET_TRADING_AMOUNT",
                        "duration": "1d", "rank": 5, "symbol": "STDY", "last": 1.0, "base": 1.0,
                        "change_rate": None, "volume": 1000, "amount": 1000, "ranked_at": ts})
        rows.append({"ts": ts, "trade_date": "2026-10-23", "session": "REGULAR", "type": "MARKET_TRADING_AMOUNT",
                    "duration": "1d", "rank": 1, "symbol": "OTHER", "last": 1.0, "base": 1.0,
                    "change_rate": None, "volume": 1000, "amount": 1000, "ranked_at": ts})
    st.put_rankings(rows)
    now = base + timedelta(minutes=110)
    col.poll_steady_flow(now, "2026-10-23", "REGULAR")
    ev = {r[0]: r[1] for r in st.query("SELECT symbol, kind FROM events")}
    assert ev.get("STDY") == "steady" and "FLAT" not in ev   # FLAT 은 랭킹에 한 번도 안 들어서 안 걸림
    assert any(m[0].startswith("🫧 꾸준히 거래 붙음 · STDY(꾸준이)") for m in sent.msgs)
    n = len(sent.msgs)
    col.poll_steady_flow(now, "2026-10-23", "REGULAR")                             # 쿨다운 안 지났으면 다시 안 보냄
    assert len(sent.msgs) == n


def test_pre_open_watch_ranks_by_dollar_volume_not_pct(tmp_path):
    """등락률이 아니라 거래대금 순 — 호가만 튀고 거래는 적은 종목이 위로 올라오면 안 된다."""
    col, st, sent = make(tmp_path)
    col.universe = {"PENX": {"name": "펜엑스", "last": 1.3}, "BRST": {"name": "터짐", "last": 1.1}}
    st.put_precursor_daily([{"d0": "2026-10-22", "symbol": s, "score": 7, "close": 1.0, "created": "x", "signals": "[]", "metrics": "{}"}
                            for s in col.universe])
    st.put_daily_bars([{"symbol": s, "d": "2026-10-22", "open": 1, "high": 1, "low": 1, "close": 1.0, "volume": 1} for s in col.universe])
    # PENX: +30%지만 거래대금 적음. BRST: 데이마켓 거래 터짐 + 거래대금 훨씬 큼 (등락률은 낮음)
    st.put_precursor_live([
        {"d0": "2026-10-22", "symbol": "PENX", "ts": "x", "signals": "[]", "metrics": json.dumps({"dollar_today": 5_000}),
         "alerted": "2026-10-22T00:00:00+00:00"},
        {"d0": "2026-10-22", "symbol": "BRST", "ts": "x", "signals": "[]", "metrics": json.dumps({"dollar_today": 500_000}),
         "alerted": "2026-10-23T03:00:00+00:00"},
    ])

    class Cal:
        def bounds(self, d, s):
            return (datetime(2026, 10, 23, 0, 0, tzinfo=timezone.utc),)
    col.cal = Cal()
    t = datetime(2026, 10, 23, 7, 56, tzinfo=timezone.utc)               # 03:56 ET
    assert col.pre_open_watch(t, "d") == ["BRST", "PENX"]                 # 등락률로는 PENX 가 위지만 거래대금은 BRST 가 위
    assert "거래대금 순" in sent.msgs[-1][0]


def test_stale_positions_closed_on_restart(tmp_path):
    col, st, sent = make(tmp_path)
    only(col, "S1")
    px = [0.9] * 3 + [1.0, 1.10, 0.95, 0.80]
    col._vtrade(T0, "2026-10-22", "PENX", 9, 0.9, bars_of(px), None, "PRE")
    col.universe["PENX"]["last"] = 0.82
    col.check_vsignals(T0 + timedelta(hours=5), "CLOSED", True, 20)      # 5시간 → 그대로
    assert st.query("SELECT status FROM vsignals")[0][0] == "OPEN"
    col.check_vsignals(T0 + timedelta(hours=25), "CLOSED", True, 20)     # 25시간 → 정리
    assert st.query("SELECT status, exit_reason FROM vsignals")[0] == ("CLOSED", "애프터장 마감 정리")


def test_realtime_fill_and_exits(tmp_path, monkeypatch):
    monkeypatch.setitem(STRATEGIES["S1"], "tp", 10)
    col, st, sent = make(tmp_path)
    only(col, "S1")

    class Cal:
        def classify(self, now):
            return (None, "PRE")
    col.cal = Cal()
    col.clock = lambda: T0 + timedelta(minutes=6)
    px = [0.9] * 3 + [1.0, 1.10]
    col._vtrade(T0, "2026-10-22", "PENX", 9, 0.9, bars_of(px), None, "PRE")   # 지정가 0.864 (VWAP −10%)
    col._vopen_refresh(st)                                                # 주문 종목을 구독 → 평소 거래량 채우기 작업
    assert col._rt_q.get_nowait() == ("seed", "PENX")
    col._on_trade("PENX", 0.90, 100)                                      # 지정가 위 → 아직
    assert col._rt_q.empty()
    col._on_trade("PENX", 0.86, 100)                                      # 체결가가 지정가 아래 → 바로 체결
    job = col._rt_q.get_nowait()
    assert job[0] == "vfill"
    col._rt_vfill(st, job[1], job[2])
    assert st.query("SELECT status, round(entry, 3) FROM vsignals")[0] == ("OPEN", 0.864)
    assert sent.msgs[-1][0].startswith("🧪 [전략1 VWAP 눌림] 가상 매수 체결")
    col._on_trade("PENX", 0.95, 100)                                      # +10% 익절은 0.9504 → 아직
    assert col._rt_q.empty()
    col._on_trade("PENX", 0.951, 100)                                     # 익절가 도달 → 바로 정리
    job = col._rt_q.get_nowait()
    col._rt_vclose(st, job[1], job[2])
    assert st.query("SELECT status, exit_reason FROM vsignals")[0] == ("CLOSED", "익절 +10%")


def test_near_target_pullback_exit(tmp_path):
    col, st, sent = make(tmp_path)
    v = {"id": 1, "entry": 1.0, "tp": 20.0, "sl": 35.0, "high": 1.0, "opened": T0.isoformat()}
    now = T0 + timedelta(minutes=10)
    assert col._vexit(v, 1.185, 1.195, now) is None                      # +19.5% 고점, 지금 +18.5% (1%p 밀림)
    assert col._vexit(v, 1.175, 1.195, now) == "목표 근처 +19.5% 에서 밀려 정리"   # 2%p 밀림
    assert col._vexit(v, 1.12, 1.15, now) is None                        # +15% 고점은 목표 근처(+18%) 아님


def test_settings_toggle_strategy_and_alerts(tmp_path):
    from pennybot import settings
    col, st, sent = make(tmp_path)
    settings.save(str(tmp_path), {"strategies": {"S1": False}, "alerts": {"vtrade": False}})
    only(col, "S1", "S4")
    px = [0.9] * 3 + [1.0, 1.10]
    col._vtrade(T0, "2026-10-22", "PENX", 9, 0.9, bars_of(px), None, "PRE")
    assert [r[0] for r in st.query("SELECT strat FROM vsignals")] == ["S4"]   # ① 꺼짐
    assert sent.msgs == []                                                # 가상 매매 알림 꺼짐
    assert settings.load(str(tmp_path))["vaccount"] == {"capital": 10000, "per_trade": 1000}
    assert settings.save(str(tmp_path), {"vaccount": {"capital": "5000"}, "bogus": 1})["vaccount"]["capital"] == 5000
