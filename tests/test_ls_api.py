import json
from datetime import datetime, timezone

from pennybot import dailysync
from pennybot.ls_api import LsClient


def _gsc(sym, price, qty=100, trdtm="105540", ovs="20261116", rate="5.0", diff="0.1", totq="1000", amount="50000"):
    return json.dumps({"header": {"tr_cd": "GSC", "tr_key": ("82" + sym).ljust(18)},
                       "body": {"symbol": sym, "price": str(price), "trdq": str(qty), "totq": totq, "amount": amount, "open": "1.0",
                                "high": "2.0", "low": "0.9", "rate": rate, "diff": diff, "trdtm": trdtm, "ovsdate": ovs}})


def _client(tmp_path):
    cl = LsClient("key", "secret", tmp_path, tmp_path / "m.db")
    return cl, cl.make_stream(daily=False)


def test_gsc_builds_price_bar_and_callback_only_for_watched(tmp_path):
    cl, st = _client(tmp_path)
    got = []
    st.on_trade = lambda s, p, q: got.append((s, p, q))
    st._cb_now = {"AAA"}
    st._handle_ls(_gsc("AAA", 1.5, 100))
    st._handle_ls(_gsc("AAA", 1.6, 50))
    st._handle_ls(_gsc("BBB", 2.0, 10))                       # 캐시만 (콜백 대상 아님)
    assert got == [("AAA", 1.5, 100.0), ("AAA", 1.6, 50.0)]
    assert [p["lastPrice"] for p in cl.prices(["AAA", "BBB", "ZZZ"])] == [1.6, 2.0]
    c = cl.candles("AAA", "1m", 5)["candles"]
    assert len(c) == 1 and c[0]["openPrice"] == 1.5 and c[0]["highPrice"] == 1.6 and c[0]["closePrice"] == 1.6 and c[0]["volume"] == 150.0
    assert c[0]["timestamp"].endswith("Z")                    # 토스와 같은 형식 (봉 종료 시각)
    assert st.sess_frames["REGULAR"] == 3


def test_rest_refresh_replaces_stale_websocket_price_for_selected_symbol(tmp_path, monkeypatch):
    cl, st = _client(tmp_path)
    st._handle_ls(_gsc("AAA", 1.5))
    monkeypatch.setattr(cl, "quote_now", lambda sym, ttl=0: {"price": 1.7} if sym == "AAA" else None)
    rows = cl.prices(["AAA", "BBB"], rest_symbols={"AAA"})
    assert rows[0]["lastPrice"] == 1.7
    assert rows[1:] == []


def test_rest_refresh_keeps_websocket_price_when_rest_unavailable(tmp_path, monkeypatch):
    cl, st = _client(tmp_path)
    st._handle_ls(_gsc("AAA", 1.5))
    monkeypatch.setattr(cl, "quote_now", lambda sym, ttl=0: None)
    assert cl.prices(["AAA"], rest_symbols={"AAA"})[0]["lastPrice"] == 1.5


def test_rankings_shape_matches_toss(tmp_path):
    cl, st = _client(tmp_path)
    st._handle_ls(_gsc("AAA", 1.5, rate="12.0", diff="0.16", totq="5000", amount="7500"))
    st._handle_ls(_gsc("BBB", 2.0, rate="-3.0", diff="-0.06", totq="9000", amount="18000"))
    g = cl.rankings("TOP_GAINERS", "1d")["rankings"]
    assert [r["symbol"] for r in g] == ["AAA", "BBB"] and g[0]["rank"] == 1
    assert abs(g[0]["price"]["changeRate"] - 0.12) < 1e-9 and g[0]["price"]["lastPrice"] == 1.5
    v = cl.rankings("MARKET_TRADING_VOLUME", "realtime")["rankings"]
    assert [r["symbol"] for r in v] == ["BBB", "AAA"] and v[0]["tradingVolume"] == 9000


def test_orderbook_from_gsh_and_non_blocking(tmp_path):
    cl, st = _client(tmp_path)
    assert cl.orderbook("AAA")["bids"] == []                  # 아직 없음 → 기다리지 않고 구독만 요청
    assert "AAA" in st.book_desired()
    st._handle_ls(json.dumps({"header": {"tr_cd": "GSH"}, "body": {"symbol": "AAA", "bidho1": "1.50", "bidrem1": "100",
                                                                  "offerho1": "1.52", "offerrem1": "200"}}))
    ob = cl.orderbook("AAA")
    assert ob["bids"] == [{"price": 1.5, "volume": 100.0}] and ob["asks"] == [{"price": 1.52, "volume": 200.0}]
    st.connected = True                                      # book() 은 연결 중일 때만
    assert st.book("AAA").has_quote


def test_orderbook_timestamp_preserves_cached_quote_age(tmp_path):
    import time
    from datetime import datetime

    cl, st = _client(tmp_path)
    st._handle_ls(json.dumps({"header": {"tr_cd": "GSH"}, "body": {"symbol": "AAA", "bidho1": "1.50",
                                                                  "bidrem1": "100", "offerho1": "1.52", "offerrem1": "200"}}))
    captured, bids, asks = st.depth["AAA"]
    st.depth["AAA"] = (captured - 20, bids, asks)
    quote = cl.orderbook("AAA")
    age = time.time() - datetime.fromisoformat(quote["timestamp"].replace("Z", "+00:00")).timestamp()
    assert 19 <= age <= 23


def test_day_rollover_flushes_daily_bars(tmp_path):
    import sqlite3
    db = tmp_path / "m.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE daily_bars (symbol TEXT, d TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, PRIMARY KEY (symbol, d))")
    con.commit()
    con.close()
    cl, st = _client(tmp_path)
    st._handle_ls(_gsc("AAA", 1.5, ovs="20261116"))
    assert cl.flush_rows("20261116", {k: dict(v) for k, v in st.last.items()}) == 1
    row = sqlite3.connect(db).execute("SELECT symbol, d, close FROM daily_bars").fetchall()
    assert row == [("AAA", "2026-11-16", 1.5)]


def test_yahoo_history_client_shapes_and_paging(monkeypatch):
    t = int(datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc).timestamp())
    res = {"chart": {"result": [{"timestamp": [t, t + 60, t + 120], "indicators": {"quote": [{
        "open": [1, 2, 3], "high": [1, 2, 3], "low": [1, 2, 3], "close": [1, 2, 3], "volume": [10, 20, 30]}]}}]}}

    class R:
        status_code = 200
        def json(self): return res
        def raise_for_status(self): pass

    cl = dailysync.YahooHistoryClient()
    monkeypatch.setattr(cl.s, "get", lambda *a, **k: R())
    page = cl.candles("AAA", "1m", 2)
    assert [c["closePrice"] for c in page["candles"]] == [3, 2] and page["nextBefore"] == page["candles"][-1]["timestamp"]
    assert page["candles"][0]["timestamp"] == "2026-11-16T15:03:00Z"        # 봉 종료 시각 = 시작 + 60초
    rest = cl.candles("AAA", "1m", 2, before=page["nextBefore"])
    assert [c["closePrice"] for c in rest["candles"]] == [1] and rest["nextBefore"] is None
    d = cl.candles("AAA", "1d", 5)["candles"]
    assert d[0]["timestamp"] == "2026-11-16T00:00:00Z"


def test_last_completed_day_rules():
    assert dailysync.last_completed_day(datetime(2026, 11, 16, 22, 30, tzinfo=timezone.utc)) == "2026-11-16"    # 월요일 17:30 ET
    assert dailysync.last_completed_day(datetime(2026, 11, 16, 14, 0, tzinfo=timezone.utc)) == "2026-11-13"     # 월요일 09:00 ET → 직전 금요일
    assert dailysync.last_completed_day(datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)) == "2026-11-13"     # 일요일


def test_depth_saved_only_around_surge(tmp_path):
    """평소엔 호가를 DB 에 쓰지 않고, 급등 신호(spike_start/burst) 순간에 직전 스냅샷을 저장하고 이후 10분도 저장한다."""
    import sqlite3
    from datetime import date, timedelta
    from pennybot.collector.runner import Collector
    from pennybot.collector.store import Store
    r = Collector.__new__(Collector)
    r.store = Store(tmp_path / "d.db")
    r._depth_ring, r._depth_cap, r._ev_last = {}, {}, {}
    t0 = datetime(2026, 11, 17, 11, 0, tzinfo=timezone.utc)
    bids = lambda q: [{"price": 1.5, "volume": q}, {"price": 1.49, "volume": 0}]
    asks = [{"price": 1.52, "volume": 200}]
    for k in range(3):
        r._depth_note("AAA", t0 + timedelta(minutes=k), date(2026, 11, 17), "PRE", bids(100 + k), asks)
    con = sqlite3.connect(tmp_path / "d.db")
    assert con.execute("SELECT COUNT(*) FROM depth_pre").fetchone()[0] == 0          # 평소엔 저장 없음
    r._event(t0 + timedelta(minutes=3), "AAA", "spike_start", "x")
    assert con.execute("SELECT COUNT(*) FROM depth_pre").fetchone()[0] == 3          # 직전 3개
    r._depth_note("AAA", t0 + timedelta(minutes=4), date(2026, 11, 17), "PRE", bids(500), asks)
    assert con.execute("SELECT COUNT(*) FROM depth_pre").fetchone()[0] == 4          # 신호 뒤 10분 안
    r._depth_note("AAA", t0 + timedelta(minutes=20), date(2026, 11, 17), "PRE", bids(500), asks)
    assert con.execute("SELECT COUNT(*) FROM depth_pre").fetchone()[0] == 4          # 10분 지나면 중단
    d = con.execute("SELECT depth FROM depth_pre ORDER BY ts LIMIT 1").fetchone()[0]
    assert d == '{"b":[[1.5,100.0]],"a":[[1.52,200.0]]}'                             # 잔량 0 칸은 빠짐


def test_invalid_token_response_forces_new_token():
    """11/18: 서버가 기록된 만료 시각 전에 토큰을 거부(IGW00121)해도 같은 토큰으로 674번 재접속하던 문제."""
    import json as _json
    from pennybot.ls_api import LsStream
    st = LsStream.__new__(LsStream)
    st._force_token = False
    st._bad_token = False
    st._handle_ls(_json.dumps({"header": {"rsp_cd": "IGW00121", "rsp_msg": "유효하지 않은 token 입니다."}}))
    assert st._force_token and st._bad_token


def test_alert_limit_strategy_reads_a1_order(tmp_path):
    """전략A: 수집기가 알림 때 남긴 vsignals A1 ORDER 행을 매매 봇 alert_limit 이 지정가 계획으로 바꾼다 (유효 시간·가격대·하루 한 번)."""
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from pennybot.sessions import SessionCalendar
    from pennybot.trading.alertlimit import AlertLimit
    now = datetime(2026, 11, 18, 15, 0, tzinfo=timezone.utc)
    rows = [("AAA", 1.00, (now - timedelta(seconds=60)).isoformat(), "알림가 1.05", 1.05),       # 유효
            ("OLD", 1.00, (now - timedelta(seconds=400)).isoformat(), "너무 오래됨", 1.0)]
    mv = SimpleNamespace(q=lambda sql, a=(): [r for r in rows if r[2] >= a[0]], _asof=lambda: "9999")
    cal = SessionCalendar(None)
    ctx = SimpleNamespace(now=now, trade_date=now.date(), mv=mv, held=set(), cal=cal)
    s = AlertLimit({"entry_ttl_seconds": 180, "hold_hours": 2, "take_profit_pct": 30.0, "stop_loss_pct": 30.0,
                    "min_price": 0.5, "max_price": 5.0})
    out = s.scan(ctx)
    assert [p.symbol for p in out] == ["AAA"]
    p = out[0]
    assert p.price == 1.0 and abs(p.target - 1.3) < 1e-9 and abs(p.stop - 0.7) < 1e-9
    ctx.held = {"AAA"}
    assert s.scan(ctx) == []                                   # 이미 보유 중
    ctx.held = set()
    s2 = AlertLimit({"entry_ttl_seconds": 180, "hold_hours": 2, "take_profit_pct": 30.0, "stop_loss_pct": 30.0,
                     "min_price": 2.0, "max_price": 5.0})
    assert s2.scan(ctx) == []                                  # 가격대 밖


def test_a1_tick_fills_and_expires(tmp_path):
    """전략A: 현재가가 지정가에 닿으면 체결(OPEN), minutes 분이 지나면 취소(EXPIRED) — 메인 틱이 막혀도 빠른 훑기 스레드에서 처리."""
    from datetime import datetime, timedelta, timezone
    from pennybot.collector.runner import Collector
    from pennybot.collector.store import Store
    store = Store(str(tmp_path / "m.db"))
    c = Collector.__new__(Collector)
    c.universe = {"AAA": {"last": 1.00}, "BBB": {"last": 1.20}}
    c.notifier = None
    now = datetime(2026, 11, 18, 15, 0, tzinfo=timezone.utc)

    def put(sym, opened, entry):
        with store.db:
            store.db.execute("INSERT INTO vsignals (symbol, d0, opened, session, entry, score, reason, status, high, low, last, tp, sl, strat) "
                             "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (sym, "2026-11-18", opened.isoformat(), "PRE", entry, 0, "t", "ORDER", entry, entry, entry, 30, 30, "A1"))
    put("AAA", now - timedelta(seconds=60), 1.00)           # 현재가가 이미 지정가 → 체결
    put("BBB", now - timedelta(seconds=240), 1.00)          # 4분 지남 → 취소 (현재가 1.20 > 지정가)
    c._a1_tick(now, "PRE", store)
    st = {r[0]: r[1] for r in store.query("SELECT symbol, status FROM vsignals WHERE strat='A1'")}
    assert st == {"AAA": "OPEN", "BBB": "EXPIRED"}


def test_token_is_treated_expired_at_next_0700_kst(tmp_path):
    """11/18: 서버가 07:00 KST 에 토큰을 무효로 만드는데 파일의 만료(발급+30시간)만 믿어 같은 토큰으로 재접속하던 문제."""
    import json as _json
    import time as _time
    from datetime import datetime, timedelta, timezone
    from pennybot.ls_api import LsAuth
    kst = timezone(timedelta(hours=9))
    p = tmp_path / "tok.json"
    a = LsAuth("KEY123456", "sec", p)
    issued = datetime(2026, 11, 17, 19, 57, tzinfo=kst).timestamp()      # 11/17 19:57 발급 (서버 만료는 30시간 뒤로 적어 줌)
    p.write_text(_json.dumps({"key": "KEY123", "token": "T", "expires_at": issued + 108000, "issued_at": issued}), encoding="utf-8")
    until = a.valid_until()
    assert datetime.fromtimestamp(until, kst) == datetime(2026, 11, 18, 7, 0, tzinfo=kst)     # 다음 07:00 KST
    # 07:00 이후에 발급한 토큰은 그다음 날 07:00 까지
    issued2 = datetime(2026, 11, 18, 7, 5, tzinfo=kst).timestamp()
    p.write_text(_json.dumps({"key": "KEY123", "token": "T2", "expires_at": issued2 + 108000, "issued_at": issued2}), encoding="utf-8")
    assert datetime.fromtimestamp(a.valid_until(), kst) == datetime(2026, 11, 19, 7, 0, tzinfo=kst)


def test_tv_gap_scan_only_in_ls_gap_and_needs_a_minute_of_history():
    """LS 시세가 없는 미국 17~20시: 트레이딩뷰 누적 거래량이 1분 사이 크게 늘면 거래 터짐으로 처리 큐에 보낸다 (그 밖의 시간엔 안 함)."""
    import queue
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from pennybot.collector.runner import Collector
    from pennybot.config import CollectorConfig
    c = Collector.__new__(Collector)
    c.cfg = CollectorConfig()
    c.universe = {"AAA": {}, "BBB": {}}
    c._rt_q = queue.Queue()
    c._rt_shares = {"AAA": 1_000_000, "BBB": 1_000_000_000}
    et_17h = datetime(2026, 11, 18, 22, 10, tzinfo=timezone.utc)          # 17:10 ET
    snap0 = {"AAA": {"vol": 100_000, "post_vol": 0, "px": 2.0}, "BBB": {"vol": 5_000_000, "post_vol": 0, "px": 2.0}, "CCC": {"vol": 1, "post_vol": 0, "px": 1.0}}
    assert c._tv_gap_scan(snap0, et_17h, "AFTER", t=1000.0) == []           # 첫 스냅샷: 비교 대상 없음
    snap1 = {"AAA": {"vol": 100_000, "post_vol": 20_000, "px": 2.1}, "BBB": {"vol": 5_000_000, "post_vol": 12_000, "px": 2.0},
             "CCC": {"vol": 900_000, "post_vol": 0, "px": 1.0}}
    # AAA: +20,000주 × $2.1 = $42K, 회전율 2% → 터짐 / BBB: $24K 지만 회전율 0.0012% 이고 $5만 미만 → 아님 / CCC: 유니버스 밖
    assert c._tv_gap_scan(snap1, et_17h, "AFTER", t=1060.0) == ["AAA"]
    job = c._rt_q.get_nowait()
    assert job[0] == "burst" and job[1] == "AAA" and abs(job[2] - 42_000) < 1 and job[3] == 20_000
    # LS 시세가 있는 시간(예: 프리마켓 09:00 UTC = 04:00 ET… 아님 정규 이후가 아닌 세션)에는 아무 것도 안 함
    assert c._tv_gap_scan(snap1, datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc), "PRE", t=1120.0) == []



def test_nyse_holidays_and_early_close():
    """11/19: 장 캘린더가 평일이면 다 연다고 봤다 → NYSE 휴장일·조기폐장 규칙."""
    from datetime import date, datetime
    from pennybot.sessions import ET, SessionCalendar, nyse_early_close, nyse_holidays, REGULAR, CLOSED
    h = nyse_holidays(2026)
    assert h == {date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3), date(2026, 5, 25), date(2026, 6, 19),
                 date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25)}
    assert date(2027, 12, 31) in nyse_holidays(2027) or date(2027, 12, 24) in nyse_holidays(2027)    # 2027-12-25 가 토요일 → 금요일 휴장
    assert nyse_early_close(date(2026, 11, 27)) and nyse_early_close(date(2026, 12, 24)) and not nyse_early_close(date(2026, 11, 25))
    cal = SessionCalendar(None)
    assert cal.classify(datetime(2026, 11, 26, 11, 0, tzinfo=ET))[1] == CLOSED                # 추수감사절 정규장 시간 = 휴장
    d, ses = cal.classify(datetime(2026, 11, 27, 12, 0, tzinfo=ET))
    assert ses == REGULAR                                                                       # 조기폐장일 오전은 정규장
    assert cal.classify(datetime(2026, 11, 27, 14, 0, tzinfo=ET))[1] == "AFTER"                # 13시 이후 애프터


def test_alert_limit_day_order_stays_until_day_session_ends():
    """데이마켓 알림 지정가는 3분이 아니라 세션이 끝날 때(프리장 시작)까지 유효하다. 다른 세션은 3분 그대로."""
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from pennybot.sessions import SessionCalendar
    from pennybot.trading.alertlimit import AlertLimit
    cal = SessionCalendar(None)
    now = datetime(2026, 11, 18, 3, 0, tzinfo=timezone.utc)          # 미 동부 11/17 22:00 = 데이마켓 한가운데
    assert cal.classify(now)[1] == "DAY"
    opened = (now - timedelta(hours=1, minutes=50)).isoformat()      # 1시간 50분 전 주문 (3분이면 이미 만료)
    rows = [("AAA", 1.00, opened, "알림가 1.05", 1.05)]
    mv = SimpleNamespace(q=lambda sql, a=(): [r for r in rows if r[2] >= a[0]], _asof=lambda: "9999")
    base = {"entry_ttl_seconds": 180, "hold_hours": 5, "take_profit_pct": 30.0, "stop_loss_pct": 30.0,
            "min_price": 0.5, "max_price": 5.0}
    ctx = SimpleNamespace(now=now, trade_date=now.date(), mv=mv, held=set(), cal=cal, session="DAY")
    assert AlertLimit(dict(base)).scan(ctx) == []                                   # 설정 없으면 3분
    out = AlertLimit(dict(base, hold_until_session_end=["DAY"])).scan(ctx)
    assert [p.symbol for p in out] == ["AAA"]
    assert out[0].deadline > cal.next_change(now)                                   # 보유 기한은 세션이 끝난 뒤부터 센다


def test_a1_virtual_order_day_expires_only_when_session_changes():
    from datetime import datetime, timedelta, timezone
    from pennybot.collector.runner import Collector
    from pennybot.vstrat import STRATEGIES
    st = STRATEGIES["A1"]
    now = datetime(2026, 11, 18, 3, 0, tzinfo=timezone.utc)
    v = {"session": "DAY", "opened": (now - timedelta(hours=2)).isoformat()}
    assert Collector._a1_order_expired(v, now, "DAY", st) is False                   # 데이마켓 안에선 계속 유지
    assert Collector._a1_order_expired(v, now, "PRE", st) is True                    # 프리장 시작되면 취소
    p = {"session": "PRE", "opened": (now - timedelta(minutes=4)).isoformat()}
    assert Collector._a1_order_expired(p, now, "PRE", st) is True                    # 다른 세션은 3분
