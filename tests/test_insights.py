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
    assert overhead.label(p).startswith("위 +30% 매물 가벼움")
    near = overhead.profile(bars, 1.75, shares=1e7)           # $1.75 → +30% 안에 $2 매물대
    assert near["near_pct"] > 25 and "무거움" in overhead.label(near)
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
    assert txt.startswith("🗞 장 시작 전 브리핑") and "HLT" in txt and "▲ FIL 대형 계약" in txt
    assert sent.msgs[-1][0] == txt
    assert col.send_briefing(t + timedelta(minutes=1), "2026-10-22") is None      # 하루 한 번
    b = json.loads((tmp_path / "briefing.json").read_text(encoding="utf-8"))
    assert b["halts"][0]["symbol"] == "HLT"
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
    assert len(alerts()) == 2 and alerts()[-1].startswith("🔁 재가속 (PENX · 펜엑스) — 오늘 1번째")
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
