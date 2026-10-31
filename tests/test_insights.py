"""물린 물량 · 알림 성과 · 장 시작 전 브리핑 · 실제 계좌 스냅샷."""
import json
from datetime import datetime, timedelta, timezone

from pennybot import alertperf, briefing, overhead
from tests.test_vsignals import make


def test_overhead_profile():
    # 60일 동안 $2 근처에서 많이 거래되고 지금 $1 → 위에 물린 물량이 무겁고 벽은 $2 근처
    bars = [{"high": 2.1, "low": 1.9, "close": 2.0, "volume": 1e6}] * 40 + [{"high": 1.05, "low": 0.95, "close": 1.0, "volume": 2e5}] * 20
    p = overhead.profile(bars, 1.0, shares=1e7)
    assert p["above_pct"] > 80 and p["near_pct"] < 10 and p["wall"]["lo"] > 1.8
    assert p["above_x"] > 3                                   # 발행주식 1천만 × 4배 넘게 위에 물림
    assert overhead.label(p).startswith("매물대 얇음")
    near = overhead.profile(bars, 1.75, shares=1e7)           # $1.75 → +30% 안에 $2 매물대
    assert near["near_pct"] > 25 and "두꺼움" in overhead.label(near)
    assert overhead.profile([], 1.0) is None


def _seed(st, sym, t0, path, prev=1.0, kind="burst", text="📶 x"):
    d = alertperf.trade_day(t0)
    pd_ = (datetime.fromisoformat(d) - timedelta(days=1)).date().isoformat()
    st.db.execute("INSERT OR REPLACE INTO daily_bars (symbol, d, open, high, low, close, volume) VALUES (?,?,1,1,1,?,1)", (sym, pd_, prev))
    for m, hi, lo, c in path:
        st.db.execute("INSERT OR REPLACE INTO bars_1m (symbol, ts, trade_date, session, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?,?,1000)",
                      (sym, (t0 + timedelta(minutes=m)).isoformat(), d, "PRE", c, hi, lo, c))
    st.db.execute("INSERT OR REPLACE INTO events (ts, symbol, kind, text, value) VALUES (?,?,?,?,1)", (t0.isoformat(), sym, kind, text))
    st.db.commit()


T = datetime(2026, 10, 22, 11, 0, tzinfo=timezone.utc)        # 07:00 ET


def test_alert_outcome_and_perf_summary(tmp_path):
    col, st, sent = make(tmp_path)
    _seed(st, "WINR", T, [(0, 1.1, 1.1, 1.1), (3, 1.4, 1.08, 1.35)], kind="precursor", text="🚨 WINR 급등 전조")
    _seed(st, "LOSR", T, [(0, 1.1, 1.1, 1.1), (3, 1.12, 0.95, 0.97)])
    _seed(st, "BOTH", T, [(0, 1.0, 1.0, 1.0), (2, 1.25, 0.85, 1.0)])           # 한 봉에서 둘 다 → 손실로
    mq = col._mq()
    w = alertperf.outcome(mq, None, "WINR", T.isoformat())
    assert w["result"] == "win" and w["hit30"] and abs(w["chg_at"] - 10) < 1e-9
    assert alertperf.outcome(mq, None, "LOSR", T.isoformat())["result"] == "loss"
    assert alertperf.outcome(mq, None, "BOTH", T.isoformat())["result"] == "loss"
    assert alertperf.outcome(mq, None, "NONE", T.isoformat()) is None
    st_ = alertperf.stats(mq, None, today="2026-10-23")
    assert st_["precursor"]["win"] == 1 and st_["burst"]["n"] == 2 and st_["burst"]["loss"] == 2
    txt = col.send_perf(T, "2026-10-22")
    assert txt.startswith("📊 오늘 알림 성과") and "🚨 급등 전조 1건" in txt and "📶 거래 터짐 2건" in txt
    assert sent.msgs[-1][0] == txt
    assert alertperf.kind_of("precursor", "🔥 X 급등 전조 발동") == "old"


def test_briefing_window_and_text(tmp_path):
    col, st, sent = make(tmp_path)
    st.db.execute("INSERT INTO halts (symbol, halt_ts, reason, reason_ko, resume_ts) VALUES ('HLT', ?, 'T1', '뉴스 대기', NULL)",
                  ((T - timedelta(hours=2)).astimezone(alertperf.ET).isoformat(),))
    st.db.execute("INSERT INTO events (ts, symbol, kind, text, value) VALUES (?, 'FIL', 'filing', '📄 FIL FIL 대형 계약 ▲호재', 0)",
                  ((T - timedelta(hours=3)).isoformat(),))
    st.db.commit()
    t = datetime(2026, 10, 22, 7, 47, tzinfo=timezone.utc)    # 03:47 ET
    assert col.send_briefing(t - timedelta(minutes=10), "2026-10-22") is None     # 03:37 → 아직
    txt = col.send_briefing(t, "2026-10-22")
    assert txt.startswith("🗞 장 시작 전 브리핑") and "2026-10-22" in txt
    assert "HLT" not in txt                     # 거래정지·공시는 실시간 알림·대시보드 탭에서 보는다 (브리핑에서 빼냄)
    assert sent.msgs[-1][0] == txt
    assert col.send_briefing(t + timedelta(minutes=1), "2026-10-22") is None      # 하루 한 번
    b = json.loads((tmp_path / "briefing.json").read_text(encoding="utf-8"))
    assert b["date"] == "2026-10-22" and "halts" not in b
    col._settings.value["alerts"]["briefing"] = False                            # 설정에서 끄면 텔레그램 안 보냄
    n = len(sent.msgs)
    col._brief_sent = None
    col._settings.get = lambda: col._settings.value
    col.send_briefing(t, "2026-10-23")
    assert len(sent.msgs) == n
    assert briefing._strip("📄 FIL FIL 대형", "FIL") == "대형"


def test_write_account(tmp_path):
    col, st, sent = make(tmp_path)

    class C:
        def holdings(self):
            return {"items": [{"symbol": "RENT", "quantity": "3", "lastPrice": "2.3", "averagePurchasePrice": "2.94"}]}

        def buying_power(self, cur):
            return {"currency": cur, "cashBuyingPower": "12.5"}
    col.c = C()
    col.write_account(T)
    a = json.loads((tmp_path / "account.json").read_text(encoding="utf-8"))
    assert a["holdings"]["items"][0]["symbol"] == "RENT" and a["buying_power"]["cashBuyingPower"] == "12.5"

    class Bad:
        def holdings(self):
            raise RuntimeError("no account scope")
    col.c = Bad()
    col.write_account(T)                                      # 실패하면 꺼지고 조용히
    assert col._acct_off


def test_reaccel_shape():
    from pennybot.precursor import reaccel
    t = lambda k: f"2026-10-23T08:{k:02d}:00+00:00"
    bars = [(t(0), 1, 1.0, 1.0, 1.0, 1), (t(1), 1, 2.0, 1.8, 1.9, 1), (t(2), 1, 1.9, 1.6, 1.65, 1), (t(3), 1, 1.8, 1.65, 1.78, 1)]
    r = reaccel(bars, t(0))
    assert r and r["high"] == 2.0 and r["low"] == 1.6 and round(r["drop"]) == -20 and round(r["bounce"]) == 11
    assert reaccel(bars[:3], t(0)) is None                     # 아직 반등 10% 안 됨
    assert reaccel(bars, t(3)) is None                         # 고점·눌림 뒤부터 세면 새 파동 없음


def test_realtime_reaccel_after_alert(tmp_path):
    from datetime import timedelta as td
    from tests.test_vsignals import T0
    col, st, sent = make(tmp_path)
    st.put_precursor_daily([{"d0": "2026-10-22", "symbol": "PENX", "score": 7, "close": 1.0, "created": "x", "signals": "[]",
                             "metrics": json.dumps({"dd60": -80, "vol_ratio": 60})}])
    st.put_daily_bars([{"symbol": "PENX", "d": "2026-10-22", "open": 1, "high": 1, "low": 1, "close": 1.0, "volume": 1}])
    path = [(1.0, 1.1, 1.08)] * 3

    class C:
        def candles(self, sym, interval, count, before=None):
            return {"candles": [{"timestamp": (T0 + td(minutes=k)).isoformat(), "openPrice": o, "highPrice": h,
                                 "lowPrice": min(o, c), "closePrice": c, "volume": 50000} for k, (o, h, c) in enumerate(path)]}

        def orderbook(self, sym):
            return {"asks": []}

    class Cal:
        def classify(self, now):
            return (None, "PRE")

        def bounds(self, d, s):
            return None
    col.c, col.cal = C(), Cal()
    col.clock = lambda: T0 + td(minutes=3)
    col._rt_shares = {"PENX": 1e6}
    col._rt_alert(st, "PENX", 80000.0, 70000.0, 1.08)          # 첫 알림 (가상 매매 알림은 세지 않음)
    alerts = lambda: [m[0] for m in sent.msgs if m[0].startswith(("📶", "🚨", "🔁"))]
    assert len(alerts()) == 1
    # 2.0 까지 갔다가 1.6 (−20%) → 1.8 (저점 +12%) 다시 붙음
    path += [(1.1, 2.0, 1.9), (1.9, 1.9, 1.7), (1.7, 1.7, 1.6), (1.6, 1.62, 1.62), (1.62, 1.8, 1.8)]
    col.clock = lambda: T0 + td(minutes=9)
    col._rt_alert(st, "PENX", 90000.0, 80000.0, 1.8)
    assert len(alerts()) == 2 and alerts()[-1].startswith("🔁 재가속 · PENX(펜엑스) · 오늘 1번째")
    assert st.query("SELECT COUNT(*) FROM events WHERE kind='reaccel'")[0][0] == 1
    col.clock = lambda: T0 + td(minutes=12)
    col._rt_alert(st, "PENX", 90000.0, 80000.0, 1.85)          # 20분 안 → 또 안 보냄
    assert len(alerts()) == 2


def test_keep_watch_includes_faded_runners(tmp_path):
    col, st, sent = make(tmp_path)
    rows = {}
    for sym, hi in (("RUNR", 1.8), ("FLAT", 1.05)):
        st.put_daily_bars([{"symbol": sym, "d": "2026-10-21", "open": 1, "high": 1, "low": 1, "close": 1.0, "volume": 1},
                           {"symbol": sym, "d": "2026-10-22", "open": 1, "high": hi, "low": 0.9, "close": 1.0, "volume": 1}])
        rows[sym] = (sym, 1, "[]", "{}", 1.0)
    keep = col._keep_watch("2026-10-22", None, rows)
    assert keep == {"RUNR"}                                    # 어제 +80% 찍고 제자리로 온 종목은 계속 감시


def test_runners_and_approach_before_breakout(tmp_path):
    from datetime import timedelta as td
    from tests.test_vsignals import T0
    col, st, sent = make(tmp_path)
    days = ["2026-10-14", "2026-10-15", "2026-10-16", "2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22"]
    bars = [(1.0, 1.0, 1.0), (1.0, 2.2, 1.6), (1.6, 1.7, 1.2), (1.2, 1.3, 1.0), (1.0, 1.1, 0.9), (0.9, 1.0, 0.95), (0.95, 1.0, 1.0)]
    st.put_daily_bars([{"symbol": "PENX", "d": d, "open": 1, "high": h, "low": lo, "close": c, "volume": 1}
                       for d, (c0, h, c) in zip(days, bars) for lo in [min(c0, c)]])
    r = col._runners("2026-10-22")["PENX"]
    assert r["run_d"] == "2026-10-15" and round(r["run_chg"]) == 120 and r["days"] == 6 and r["level"] == 2.2 and r["low"] == 0.9
    assert col._keep_watch("2026-10-22", None, {"PENX": ("PENX", 1, "[]", "{}", 1.0)}) == {"PENX"}

    class Cal:
        def classify(self, now):
            return (None, "PRE")

        def bounds(self, d, s):
            return None
    col.cal = Cal()
    col.clock = lambda: T0
    col._rb_levels = {"PENX": 2.2}                             # 1단계 선 1.87 · 2단계 선 2.09
    col._rt_seeded.add("PENX")

    def jobs():
        out = []
        while not col._rt_q.empty():
            out.append(col._rt_q.get_nowait())
        return [j for j in out if j[0] == "approach"]
    for k in range(30):                                        # 평소 분당 1천 주
        col._rt_min.setdefault("PENX", {})[int((1000.0 - 60 * (k + 2)) // 60)] = 1000
    col._on_trade("PENX", 1.80, 500, t=1000.0)                 # 선 아래
    col._on_trade("PENX", 1.88, 500, t=1001.0)                 # 1단계 선 넘었지만 거래 안 붙음 → 아직
    assert jobs() == []
    col._on_trade("PENX", 1.80, 500, t=1002.0)
    col._on_trade("PENX", 1.90, 30000, t=1003.0)               # 거래 붙어서 넘음 → 👀 접근
    j, = jobs()
    assert j[3] == 1
    assert col._approach(st, T0, "PRE", "PENX", j[2], 1, j[4], 1.0)
    col._on_trade("PENX", 2.10, 10000, t=1004.0)               # 5% 아래 선 → 🎯 임박
    j2, = jobs()
    assert j2[3] == 2 and col._approach(st, T0 + td(minutes=1), "PRE", "PENX", 2.10, 2, j2[4], 1.0)
    msgs = [m[0] for m in sent.msgs if m[0].startswith(("👀", "🎯"))]
    assert msgs[0].startswith("👀 이전 고점 접근 · PENX(펜엑스) — 6일 전 +120% 급등주") and "$2.20" in msgs[0]
    assert msgs[1].startswith("🎯 이전 고점 임박") and "+4.8%" in msgs[1]
    assert not col._approach(st, T0 + td(minutes=2), "PRE", "PENX", 2.12, 2, 9e4, 1.0)   # 단계마다 하루 한 번
    from pennybot.precursor import runner_tag
    assert runner_tag(r) == "🔙 6일 전 +120% 급등주"


def test_bottom_bounce_catches_second_wave_before_approach(tmp_path):
    """고점 접근(옛 고점 근처)도 아니고 재가속(당일 눌림)도 아닌, 며칠에 걸쳐 바닥 찍고 막 오르기 시작하는 구간."""
    from tests.test_vsignals import T0
    col, st, sent = make(tmp_path)
    days = ["2026-10-14", "2026-10-15", "2026-10-16", "2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22"]
    bars = [(1.0, 1.0, 1.0), (1.0, 2.2, 1.6), (1.6, 1.7, 1.2), (1.2, 1.3, 1.0), (1.0, 1.1, 0.9), (0.9, 0.95, 0.9), (0.9, 0.95, 0.9)]
    st.put_daily_bars([{"symbol": "PENX", "d": d, "open": 1, "high": h, "low": lo, "close": c, "volume": 1}
                       for d, (c0, h, c) in zip(days, bars) for lo in [min(c0, c)]])
    r = col._runners("2026-10-22")["PENX"]
    assert r["level"] == 2.2 and r["low"] == 0.9             # 고점 2.2, 그 뒤 바닥 0.9
    assert not col._bottom_bounce_check(st, T0, "PRE", "PENX", 1.02, 1.0)   # 0.9 대비 +13% — 아직 15% 기준 밖
    assert col._bottom_bounce_check(st, T0, "PRE", "PENX", 1.05, 1.0)       # 0.9 대비 +16.7% → 재반등
    assert sent.msgs[-1][0].startswith("🌊 바닥 찍고 재반등 · PENX(펜엑스) — 6일 전 +120% 급등주")
    assert not col._bottom_bounce_check(st, T0, "PRE", "PENX", 1.06, 1.0)   # 하루 한 번
    assert not col._bottom_bounce_check(st, T0, "PRE", "PENX", 1.90, 1.0)   # 고점(2.2)의 80%(1.76) 넘으면 approach 몫


def test_tier_turnover_and_wake():
    from pennybot.precursor import daily, tier
    big = 5e7                                                     # 발행 5천만 주: 예전 기준이면 무조건 0
    assert tier({"volume": 0.31 * big, "vol_ratio": 1, "dd60": -10}, big, 3.0) == 2          # 회전율 30%+
    assert tier({"volume": 0.05 * big, "vol_ratio": 4, "dd60": -10, "wake": True}, big, 3.0) == 1   # 거래 깨어남
    assert tier({"volume": 0.4 * big, "vol_ratio": 4, "dd60": -10, "wake": True}, big, 3.0) == 3    # 둘 다
    assert tier({"volume": 0.05 * big, "vol_ratio": 4, "dd60": -10}, big, 3.0) == 0
    # 60일 동안 하루 10만 주 → 직전 5일 2만 주로 마름 → 전날 9만 주: 깨어남
    bars = [(f"d{i:03d}", 1, 1.05, 0.95, 1.0, 1e5) for i in range(60)] + [(f"d{60 + i:03d}", 1, 1.05, 0.95, 1.0, 2e4) for i in range(5)]
    bars.append(("d065", 1, 1.2, 0.95, 1.1, 9e4))
    r = daily(bars)
    assert r["metrics"]["wake"] and "wake" in r["signals"]


def test_ranked_does_not_rank_falling_stock_above_rising_one(tmp_path):
    """'거래 급증'(wake)은 방향을 안 본다 — 지금 떨어지는 중이어도 뜬다. 떨어지는 종목이 일봉 점수만 높다고
    (지금) 오르는 종목보다 급등 후보 맨 위로 오면 안 된다."""
    from pennybot.precursor import ranked
    col, st, sent = make(tmp_path)
    d0 = "2026-10-23"
    st.put_precursor_daily([
        {"d0": d0, "symbol": "DOWN", "score": 9, "close": 1.0, "created": "x",
         "signals": "[]", "metrics": "{}"},
        {"d0": d0, "symbol": "UP", "score": 4, "close": 1.0, "created": "x",
         "signals": "[]", "metrics": "{}"},
    ])
    st.put_precursor_live([
        {"d0": d0, "symbol": "DOWN", "ts": "x", "alerted": "x",
         "signals": json.dumps(["wake"]), "metrics": json.dumps({"chg": -12.0})},
        {"d0": d0, "symbol": "UP", "ts": "x", "alerted": "x",
         "signals": json.dumps(["wake"]), "metrics": json.dumps({"chg": 8.0})},
    ])
    r = ranked(str(tmp_path / "m.db"), 10)
    syms = [x["symbol"] for x in r["rows"]]
    assert syms.index("UP") < syms.index("DOWN")


def test_early20_text_format():
    from pennybot.precursor import early20_text
    text = early20_text("PENX", "펜엑스", 23.4, 1.85, "프리마켓")
    assert text.startswith("🌙 정규장 전 +20% 돌파 · PENX(펜엑스)")
    assert "$1.85" in text and "+23.4%" in text and "프리마켓" in text
    assert "5.2배" in text


def test_chart_analysis_factors():
    from pennybot.chartlib import analyze
    bars = [{"d": f"d{i:03d}", "open": 1, "high": 1.05, "low": 0.95, "close": 1.0, "volume": 1e5} for i in range(60)]
    bars += [{"d": f"d{60 + i:03d}", "open": 1, "high": 1.05, "low": 0.95, "close": 1.0, "volume": 2e4} for i in range(5)]
    bars.append({"d": "d065", "open": 0.95, "high": 1.0, "low": 0.9, "close": 0.92, "volume": 4e6})
    a = analyze(bars, shares=1e7)
    on = {f["key"] for f in a["factors"] if f["on"]}
    assert {"turn30", "wake", "sub1"} <= on and "small" not in on            # 발행 1천만 주 = 경계 밖
    assert set(a["strong"]) == {"전날 회전율 30%+", "거래 깨어남"} and a["best_x100"] > 15
    assert analyze(bars[:10]) is None
