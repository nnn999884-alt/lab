"""트리거 순간 호가 로거 (collector/triggerlog.py) — 연구용 기록 전용. P1 동결 파일은 건드리지 않음."""
import hashlib
import json
import queue
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from pennybot.collector import triggerlog as TL
from pennybot.collector.store import Store

T0 = 1_800_000_000          # 60 의 배수 (분 경계)
KST = timezone(timedelta(hours=9))


def feed_plan(det, plan, session="REGULAR", sym="AAA", start=T0, skip=()):
    """plan = [(그 분에 체결된 수량, 가격)]. 분마다 :10 과 :50 에 스냅샷 (수량은 :50 까지 쌓임). skip = 아예 스냅샷이 없는 분."""
    cum, out = 0, []
    for k, (vol, px) in enumerate(plan):
        if k in skip:
            cum += vol
            continue
        for sec in (10, 50):
            if sec == 50:
                cum += vol
            out += det.feed({sym: {"px": px, "vol": cum, "pre_vol": cum, "post_vol": cum}}, start + 60 * k + sec, session)
    return out


def det_with(pc=1.0, adv=100_000.0):
    d = TL.TriggerDetector()
    d.reset("2026-11-16", {"AAA": (pc, adv)})
    return d


BASE = [(1000, 1.0)] * 9      # 첫 분은 시작 누적을 몰라 무효 → 유효 봉 8개, 폭발 봉 기준 직전 60분(최근 5분 제외)에 3봉


def test_detector_fires_once_on_first_volume_burst_with_bar_quality():
    out = feed_plan(det_with(), BASE + [(100_000, 1.05), (500, 1.05)])
    assert len(out) == 1
    t = out[0]
    assert t["symbol"] == "AAA" and t["bar_source"] == "tv_cum"
    assert t["bar_end_ts"] == T0 + 60 * 10                # 폭발 봉(9번째 분)의 끝
    assert t["detected_ts"] == T0 + 60 * 10 + 10          # 다음 분 첫 스냅샷에서 인식 (미완성 봉 값은 쓰지 않음)
    assert t["bar_lag_s"] == 10                           # 마지막 스냅샷이 분 끝보다 10초 전 → 봉 거래량이 최대 10초치 부족할 수 있음
    assert t["dv_bar"] == pytest.approx(105_000) and t["dv5"] >= 105_000
    d = det_with()
    out = feed_plan(d, BASE + [(100_000, 1.05), (500, 1.05), (100_000, 1.05), (100_000, 1.05)])
    assert len(out) == 1                                   # 종목·날 첫 번째만


@pytest.mark.parametrize("plan,why", [
    (BASE + [(100_000, 1.12), (500, 1.12)], "이미 +10% 이상"),
    (BASE + [(6_000, 1.0), (500, 1.0)], "5분 거래대금 $2만 미만"),
    (BASE + [(2_000, 1.0), (500, 1.0)], "봉 거래대금 $5천 미만"),
])
def test_detector_requires_all_conditions(plan, why):
    assert feed_plan(det_with(), plan) == [], why


def test_detector_gates_by_reference_data_and_session():
    assert feed_plan(det_with(adv=10_000.0), BASE + [(100_000, 1.05), (500, 1.05)]) == []      # 평소 거래대금 부족
    assert feed_plan(det_with(pc=20.0, adv=1e6), BASE + [(5_000, 15.0), (500, 15.0)]) == []    # 전날 종가 범위 밖
    d = det_with()
    assert feed_plan(d, BASE + [(100_000, 1.05), (500, 1.05)], session="DAY") == [] and d.skipped_day > 0   # 데이마켓은 누적 거래량 없음
    pre = feed_plan(det_with(), BASE + [(100_000, 1.05), (500, 1.05)], session="PRE")
    assert len(pre) == 1 and pre[0]["session"] == "PRE"


def test_detector_needs_baseline_history():
    """직전 60분 이력이 3봉 미만이면(세션 시작·재시작 직후) 트리거를 내지 않는다 — 기준 0 으로 아무 거래나 폭발로 보는 가짜 트리거 방지."""
    assert feed_plan(det_with(), [(1000, 1.0)] * 4 + [(100_000, 1.05), (500, 1.05)]) == []         # 이력 부족 + 감지기가 막 시작 → 기준을 믿을 수 없음
    assert feed_plan(det_with(), [(1000, 1.0)] * 8 + [(100_000, 1.05), (500, 1.05)]) == []         # 유효 봉 7개 → 기준 봉이 2개뿐
    out = feed_plan(det_with(), BASE + [(100_000, 1.05), (500, 1.05)])
    assert len(out) == 1 and out[0]["warm_s"] < 3600                    # 이력은 있으나 감지기가 막 시작 → 워밍업 표시 (로거가 호가를 요청하지 않음)
    quiet = [(0, 1.0)] * 65                                                                          # 65분 동안 조용히 지켜봄(이력 없음을 확인) → 진짜 '첫 활동'
    assert len(feed_plan(det_with(), quiet + [(100_000, 1.05), (500, 1.05)])) == 1
    assert feed_plan(det_with(), [(0, 1.0)] * 40 + [(100_000, 1.05), (500, 1.05)]) == []             # 40분만 봤으면 아직 못 믿음
    d = det_with()                                                                                   # 스냅샷이 끊기면 다시 못 믿음
    out = feed_plan(d, [(0, 1.0)] * 65)
    d.feed({"AAA": {"px": 1.0, "vol": 0}}, T0 + 60 * 65 + 400, "REGULAR")
    assert d.warm_start == T0 + 60 * 65 + 400


def test_detector_session_change_does_not_mix_cumulative_columns():
    """PRE 의 누적(pre_vol)과 REGULAR 의 누적(vol)은 다른 열 — 경계 분에서 서로 빼면 거래량이 틀어진다."""
    d = det_with()
    out = []
    for k in range(10):
        ses = "PRE" if k < 5 else "REGULAR"
        snap = {"AAA": {"px": 1.0, "pre_vol": 50_000_000 + 1000 * k, "vol": 1000 * k, "post_vol": 0}}   # pre_vol 이 vol 보다 훨씬 큼
        for sec in (10, 50):
            out += d.feed(snap, T0 + 60 * k + sec, ses)
    assert out == []                                       # 경계에서 (vol − pre_vol) 같은 가짜 거래량으로 트리거가 나면 안 됨



def test_detector_does_not_invent_volume_across_gaps():
    out = feed_plan(det_with(), BASE + [(100_000, 1.05), (500, 1.05), (500, 1.05)], skip={9})     # 폭발 분에 스냅샷이 없음
    assert out == []                                       # 빈 분을 건너뛴 거래량을 그 다음 분에 몰아 넣지 않음


def test_load_ref_uses_only_prior_days(tmp_path):
    st = Store(str(tmp_path / "m.db"))
    with st.db:
        for i in range(12):
            d = (date(2026, 11, 3) + timedelta(days=i)).isoformat()
            st.db.execute("INSERT INTO daily_bars (symbol, d, open, high, low, close, volume) VALUES ('AAA', ?, 1,1,1, ?, 100000)", (d, 1.0 + i / 100))
    ref = TL.load_ref(st, "2026-11-12")                    # 2026-11-12 이전 9일뿐 → 부족
    assert ref == {}
    with st.db:                                            # 당일(11/16) 일봉이 있어도 신호 시점엔 알 수 없는 값 → 무시
        st.db.execute("INSERT INTO daily_bars (symbol, d, open, high, low, close, volume) VALUES ('AAA', '2026-11-16', 1,1,1, 9.99, 999999999)")
    ref = TL.load_ref(st, "2026-11-16")
    assert set(ref) == {"AAA"} and ref["AAA"][0] == pytest.approx(1.11)       # 11/14 종가, 당일 9.99 는 안 씀
    assert ref["AAA"][1] < 200_000                                             # 20일 평균 거래대금에도 당일 거래량이 안 섞임


class FakeClient:
    def __init__(self, clock, ts_offset=-3.0, ts_mode="iso", exc=None):
        self.clock, self.ts_offset, self.ts_mode, self.exc, self.calls = clock, ts_offset, ts_mode, exc, []

    def request(self, method, path, params=None, retries=4, **kw):
        self.calls.append((method, path, params, retries))
        if self.exc:
            raise self.exc
        now = self.clock.now
        ts = None if self.ts_mode == "none" else datetime.fromtimestamp(now + self.ts_offset, KST).isoformat(timespec="milliseconds")
        return {"timestamp": ts, "currency": "USD", "asks": [{"price": "1.06", "volume": "300"}, {"price": "1.07", "volume": "100"}],
                "bids": [{"price": "1.04", "volume": "500"}]}


class Clock:
    def __init__(self, now=2_000_000_000.0): self.now = now
    def __call__(self):
        self.now += 0.1
        return self.now


def trig(detected, sym="AAA", day="2026-11-16"):
    return dict(symbol=sym, session="REGULAR", trade_date=day, bar_end_ts=detected - 5, detected_ts=detected, bar_source="tv_cum", bar_lag_s=10.0, warm_s=7200.0,
                pc=1.0, trig_px=1.05, dv_bar=105_000.0, dv5=110_000.0, base_dv=1000.0, adv20=100_000.0)


def make(tmp_path, **kw):
    st = Store(str(tmp_path / "m.db"))
    clock = Clock()
    cli = FakeClient(clock, **{k: kw.pop(k) for k in ("ts_offset", "ts_mode", "exc") if k in kw})
    lg = TL.TriggerQuoteLogger(cli, str(tmp_path / "m.db"), clock=clock, store_factory=lambda: st, **kw)
    with st.db:
        for s in TL.SCHEMA.split(";"):
            if s.strip(): st.db.execute(s)
    return lg, st, cli, clock


def rows(st):
    return st.db.execute("SELECT symbol, status, fresh, req_start_ts, resp_ts, asof_ts, asof_raw, asof_age_s, bid, bid_size, ask, ask_size, levels, error, origin FROM trigger_quotes ORDER BY id").fetchall()


def test_fresh_quote_stores_request_response_and_asof_times_separately(tmp_path):
    lg, st, cli, clock = make(tmp_path)
    lg._handle(st, trig(clock.now + 1))
    (sym, status, fresh, req, resp, asof, raw, age, bid, bs, ask, as_, lv, err, origin), = rows(st)
    assert (status, fresh, origin) == ("ok", 1, "live")
    assert req < resp and asof < req                            # 세 시각이 서로 다른 값
    assert resp - asof == pytest.approx(age) and 2.5 < age < 4
    assert (bid, bs, ask, as_, lv) == (1.04, 500, 1.06, 300, 1) and raw.endswith("+09:00")
    assert cli.calls == [("GET", "/api/v1/orderbook", {"symbol": "AAA"}, 0)]      # 재시도 없음(retries=0)


def test_ls_client_uses_orderbook_instead_of_unsupported_request(tmp_path):
    lg, st, cli, clock = make(tmp_path)

    def orderbook(symbol):
        assert symbol == "AAA"
        now = clock.now
        return {"timestamp": datetime.fromtimestamp(now - 2, KST).isoformat(),
                "bids": [{"price": "1.04", "volume": "500"}],
                "asks": [{"price": "1.06", "volume": "300"}]}

    cli.orderbook = orderbook
    lg._handle(st, trig(clock.now + 1))
    assert rows(st)[0][1:3] == ("ok", 1)
    assert cli.calls == []


@pytest.mark.parametrize("kw,status", [
    ({"ts_mode": "none"}, "unconfirmed_no_asof"),          # 기준 시각 없음 → 수신 시각만으로 신선하다고 하지 않음
    ({"ts_offset": -60.0}, "unconfirmed_stale_asof"),      # 기준 시각이 낡음
    ({"ts_offset": +30.0}, "unconfirmed_clock"),           # 기준 시각이 미래 (시계 어긋남)
])
def test_unconfirmed_quotes_are_never_fresh(tmp_path, kw, status):
    lg, st, cli, clock = make(tmp_path, **kw)
    lg._handle(st, trig(clock.now + 1))
    (r,) = rows(st)
    assert r[1] == status and r[2] == 0                          # fresh = 0
    assert r[8] == 1.04                                          # 값은 진단용으로 남기되 fresh 가 아님
    assert lg.counts == {status: 1}


def test_warmup_trigger_is_recorded_but_no_quote_requested(tmp_path):
    lg, st, cli, clock = make(tmp_path)
    t = trig(clock.now + 1); t["warm_s"] = 600.0
    lg._handle(st, t)
    (r,) = rows(st)
    assert r[1] == "unconfirmed_warmup" and r[2] == 0 and r[8] is None and cli.calls == []     # 요청 안 보냄
    assert st.db.execute("SELECT warm_s FROM trigger_quotes").fetchone()[0] == 600.0


def test_failure_and_delay_are_recorded_as_unconfirmed_without_filling(tmp_path):
    lg, st, cli, clock = make(tmp_path, exc=RuntimeError("boom"))
    lg._handle(st, trig(clock.now + 1))
    r = rows(st)[0]
    assert r[1] == "unconfirmed_error" and r[2] == 0 and r[8] is None and "boom" in r[13]     # 호가 칸은 비어 있음 (과거·미래 값으로 안 채움)
    lg2, st2, cli2, clock2 = make(tmp_path / "x") if (tmp_path / "x").mkdir() is None else None
    lg2._handle(st2, trig(clock2.now - 30))                       # 감지 뒤 30초 → 10초 안에 못 보냄
    r = rows(st2)[0]
    assert r[1] == "unconfirmed_delayed" and r[8] is None and cli2.calls == []         # 요청 자체를 안 보냄


def test_rate_limit_and_429_pause(tmp_path):
    lg, st, cli, clock = make(tmp_path, max_per_min=2)
    lg._handle(st, trig(clock.now + 1, "AAA"))
    lg._handle(st, trig(clock.now + 1, "BBB"))                    # 30초 간격 필요 → 10초 안에 불가 → 미확인(한도)
    assert [r[1] for r in rows(st)] == ["ok", "unconfirmed_ratelimited"] and len(cli.calls) == 1
    (tmp_path / "y").mkdir()
    lg2, st2, cli2, clock2 = make(tmp_path / "y", exc=RuntimeError("HTTP 429 rate limit"))
    lg2._handle(st2, trig(clock2.now + 1, "AAA"))
    lg2._handle(st2, trig(clock2.now + 1, "BBB"))                 # 429 뒤 60초 쉼 → 요청 안 보냄
    assert [r[1] for r in rows(st2)] == ["unconfirmed_ratelimited", "unconfirmed_ratelimited"] and len(cli2.calls) == 1


def test_one_row_per_symbol_day_and_overflow_is_unconfirmed(tmp_path):
    lg, st, cli, clock = make(tmp_path, max_per_min=600)
    lg._handle(st, trig(clock.now + 1)); lg._handle(st, trig(clock.now + 1))
    assert len(rows(st)) == 1                                      # 같은 종목·날은 한 줄
    (tmp_path / "z").mkdir()
    lg2, st2, _, clock2 = make(tmp_path / "z", queue_max=1)
    snap = lambda s, c: {s: {"px": 1.05, "vol": c}}
    lg2.det.reset("2026-11-16", {"AAA": (1.0, 1e5), "BBB": (1.0, 1e5)})
    for k, (vol, px) in enumerate(BASE + [(100_000, 1.05), (500, 1.05)]):
        for sec in (10, 50):
            both = {"AAA": {"px": px, "vol": 0 + sum(v for v, _ in (BASE + [(100_000, 1.05), (500, 1.05)])[:k]) + (vol if sec == 50 else 0)}}
            both["BBB"] = dict(both["AAA"])
            lg2.feed(both, T0 + 60 * k + sec, "REGULAR", "2026-11-16", st2)
    assert lg2.detected == 2 and lg2.q.qsize() == 1 and len(lg2._ovf) == 1
    lg2._drain_overflow(st2)
    assert [r[1] for r in rows(st2)] == ["unconfirmed_overflow"]


def test_collector_hook_never_breaks_the_collector(tmp_path):
    from pennybot.collector.runner import Collector
    from pennybot.config import CollectorConfig
    db = str(tmp_path / "m.db"); st = Store(db)
    col = Collector(None, st, None, CollectorConfig(db_path=db))       # 캘린더·클라이언트가 없는 고장 난 상태
    col._trig_feed({"AAA": {"px": 1.0, "vol": 1}}, datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc), "REGULAR", st)   # 예외 안 남
    assert col._trig is not None
    col._trig.stop(); col._trig._thread.join(3)
    off = Collector(None, st, None, CollectorConfig(db_path=db, trigger_log=False))
    off._trig_feed({}, datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc), "REGULAR", st)
    assert off._trig is None                                           # 끄면 아무것도 안 만듦


def test_status_in_collector_status_file(tmp_path):
    from pennybot.collector.runner import Collector
    from pennybot.config import CollectorConfig
    db = str(tmp_path / "m.db"); st = Store(db)
    col = Collector(None, st, None, CollectorConfig(db_path=db))
    col.write_status(datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc), "REGULAR")
    assert json.loads((tmp_path / "collector_status.json").read_text(encoding="utf-8"))["trig"] is None
    lg, st2, cli, clock = make(tmp_path / "s") if (tmp_path / "s").mkdir() is None else None
    lg._handle(st2, trig(clock.now + 1))
    stt = lg.status()
    assert stt["counts"] == {"ok": 1} and stt["detected"] == 0 and stt["queue"] == 0


def test_p1_frozen_files_unchanged():
    """P1 동결 규칙·평가 기준 파일의 해시가 그대로여야 한다 (수집기 변경이 P1 평가 코드를 건드리지 않았다는 확인)."""
    rs = Path(__file__).resolve().parent.parent / "research"
    fr = rs / "frozen_rules.json"
    if not fr.exists():
        pytest.skip("research/frozen_rules.json 없음")
    for fn, h in json.loads(fr.read_text(encoding="utf-8"))["code_sha256"].items():
        assert hashlib.sha256((rs / fn).read_bytes()).hexdigest() == h, f"{fn} 동결 후 변경됨"


def test_existing_quotes_table_is_untouched(tmp_path):
    st = Store(str(tmp_path / "m.db"))
    cols_before = [r[1] for r in st.db.execute("PRAGMA table_info(quotes)")]
    with st.db:
        for s in TL.SCHEMA.split(";"):
            if s.strip(): st.db.execute(s)
    assert [r[1] for r in st.db.execute("PRAGMA table_info(quotes)")] == cols_before
    assert "trigger_quotes" in {r[0] for r in st.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
