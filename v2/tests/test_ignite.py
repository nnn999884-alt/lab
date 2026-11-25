import random
import time
from datetime import datetime, timezone

import pytest

from pennybot2.core.sessions import ET
from pennybot2.workers.ignite import DEFAULT_VARIANTS, IgniteDetector, Variant
from pennybot2.workers.notify import should_send

# 정규장 한가운데(수요일 11:00 ET) 에서 시작 — 개장 직후 제외 규칙에 안 걸림
T0 = datetime(2026, 11, 25, 11, 0, tzinfo=ET).timestamp()
OPEN = datetime(2026, 11, 25, 9, 30, tzinfo=ET).timestamp()


def quiet(det, sym="ABC", start=T0, minutes=30, price=1.0, per_min=6, dv_per_min=3000.0, seed=1):
    """조용한 장: 분당 per_min 건, 분당 약 dv_per_min 달러, 가격은 거의 안 움직임. 마지막 시각을 돌려준다."""
    rnd, t, p = random.Random(seed), start, price
    for m in range(minutes):
        for k in range(per_min):
            t = start + m * 60 + k * (60 / per_min)
            p = price * (1 + rnd.uniform(-0.001, 0.001))
            det.on_tick(sym, t, p, dv_per_min / per_min / p)
    return t + 10


def burst(det, sym, t0, seconds=60, n=20, dv=80_000.0, start_price=1.0, end_price=1.03, step=None):
    """t0 부터 seconds 동안 n 건에 총 dv 달러, 가격이 start→end 로 오름. (시각, 신호) 목록을 돌려준다."""
    sigs = []
    for i in range(n):
        t = t0 + i * seconds / n
        p = start_price + (end_price - start_price) * (i + 1) / n
        for s in det.on_tick(sym, t, p, dv / n / p):
            sigs.append((t, s))
    return sigs


def kinds(sigs):
    return {s["kind"] for _, s in sigs}


def test_catches_burst_early_with_attrs():
    det = IgniteDetector()
    t = quiet(det)
    sigs = burst(det, "ABC", t)
    assert "ignite:fast" in kinds(sigs) and "ignite:early" in kinds(sigs)
    fast = next(s for _, s in sigs if s["kind"] == "ignite:fast")
    a = fast["attrs"]
    assert a["from_low_pct"] < 8 and a["ratio"] >= 5 and a["trades"] >= 8 and a["session"] == "REGULAR"
    assert fast["notify"] is True and "아직 이른 구간" in fast["text"]
    first_fast = min(tt for tt, s in sigs if s["kind"] == "ignite:fast")
    assert first_fast - t < 60                       # 버스트 시작 60초 안에 잡음 (+10% 를 기다리지 않음)
    # 더 빠른 규칙(early)은 같거나 더 일찍
    assert min(tt for tt, s in sigs if s["kind"] == "ignite:early") <= first_fast


def test_does_not_fire_when_already_run_up():
    """30분 저점 대비 이미 +15% 오른 뒤의 터짐은 '늦은 알림'(추격)이라 제외."""
    det = IgniteDetector()
    t = quiet(det, price=1.0)
    for i in range(30):                              # 완만하게 먼저 +15%: 거래대금은 평소 수준
        p = 1.0 + 0.15 * (i + 1) / 30
        det.on_tick("ABC", t + i * 2, p, 400 / p)
    t2 = t + 70
    sigs = burst(det, "ABC", t2, start_price=1.15, end_price=1.18)
    assert not (kinds(sigs) & {"ignite:fast", "ignite:early", "ignite:strict"})


def test_needs_liquidity_and_trade_count():
    det = IgniteDetector()
    t = quiet(det)
    assert not kinds(burst(det, "ABC", t, dv=8_000.0))                           # 거래대금이 작음
    det2 = IgniteDetector()
    t = quiet(det2, sym="XYZ")
    assert not kinds(burst(det2, "XYZ", t, n=3, dv=90_000.0))                    # 큰 거래 3건뿐


def test_baseline_is_per_symbol_so_busy_names_do_not_fire():
    det = IgniteDetector()
    t = quiet(det, dv_per_min=60_000.0, per_min=30)         # 원래 분당 6만 달러 도는 종목
    assert not kinds(burst(det, "ABC", t, dv=80_000.0))     # 같은 8만 달러는 평소 수준 → 배수 부족
    det2 = IgniteDetector()
    t = quiet(det2, sym="Q", dv_per_min=1_000.0)
    assert "ignite:fast" in kinds(burst(det2, "Q", t, dv=80_000.0))


def test_cooldown_then_fires_again():
    det = IgniteDetector()
    t = quiet(det)
    n1 = sum(s["kind"] == "ignite:fast" for _, s in burst(det, "ABC", t))
    n2 = sum(s["kind"] == "ignite:fast" for _, s in burst(det, "ABC", t + 70, start_price=1.03, end_price=1.06))
    assert n1 == 1 and n2 == 0                        # 쿨다운 30분 안에는 다시 안 울림
    later = t + 2400
    t = quiet(det, start=later - 1800, price=1.06, seed=2)           # 다시 조용해진 뒤
    assert sum(s["kind"] == "ignite:fast" for _, s in burst(det, "ABC", t + 5, start_price=1.06, end_price=1.09)) == 1


def test_stir_is_volume_before_price():
    det = IgniteDetector()
    t = quiet(det)
    sigs = burst(det, "ABC", t, end_price=1.003)       # 거래는 터졌는데 가격은 거의 안 움직임
    assert "stir" in kinds(sigs) and not (kinds(sigs) & {"ignite:fast", "ignite:early", "ignite:strict"})
    assert all(s["notify"] is False for _, s in sigs)


def test_open_burst_suppressed_but_early_variant_sees_it():
    det = IgniteDetector()
    t = quiet(det, start=OPEN - 1800, minutes=30)                # 장 시작 직전까지 조용
    sigs = burst(det, "ABC", OPEN + 5, seconds=40, n=20, dv=80_000.0, end_price=1.03)
    assert "ignite:early" in kinds(sigs)                         # skip_open_s=0 규칙은 잡음
    assert "ignite:fast" not in kinds(sigs)                      # 기본 규칙은 개장 직후 60초 제외
    early = next(s for _, s in sigs if s["kind"] == "ignite:early")
    assert early["attrs"]["sec_from_open"] < 60


def test_warm_start_from_bars_avoids_false_ratio():
    det = IgniteDetector()
    base_min = int(T0 // 60) - 30
    det.warm("ABC", [(base_min + i, 0.99, 1.0, 50_000.0) for i in range(30)])        # 분당 약 $5만 도는 종목
    assert not kinds(burst(det, "ABC", T0, dv=80_000.0))
    det2 = IgniteDetector()                                                          # 기준선 정보가 없으면(하한 사용) 같은 움직임에 반응
    assert "ignite:fast" in kinds(burst(det2, "ABC", T0, dv=80_000.0))


def test_out_of_order_and_bad_ticks_are_ignored():
    det = IgniteDetector()
    det.on_tick("A", T0 + 200, 1.0, 100)
    assert det.on_tick("A", T0, 1.0, 100) == [] and det.on_tick("A", T0 + 201, 0, 100) == [] and det.on_tick("A", T0 + 202, 1.0, 0) == []


def test_price_range_filter():
    det = IgniteDetector(price_range=(0.5, 5.0))
    t = quiet(det, price=20.0, dv_per_min=3000.0)
    assert not kinds(burst(det, "ABC", t, start_price=20.0, end_price=20.6))


def test_random_noise_produces_no_ignite_signals():
    """종목 300개 × 2시간의 평범한 거래(가끔 큰 체결은 있음, 가격은 안 움직임)에서 오탐 0."""
    rnd = random.Random(5)
    det = IgniteDetector(DEFAULT_VARIANTS)
    n_sig = 0
    for s in range(300):
        sym, p = f"N{s}", rnd.uniform(0.6, 4.0)
        scale = rnd.uniform(500, 20_000)
        for m in range(120):
            for k in range(rnd.randint(2, 12)):
                t = T0 + m * 60 + k * 4 + rnd.random()
                p *= 1 + rnd.gauss(0, 0.0008)
                dv = scale / 6 * (10 if rnd.random() < 0.02 else 1) * rnd.uniform(0.3, 1.7)
                for sig in det.on_tick(sym, t, p, dv / p):
                    n_sig += sig["kind"].startswith("ignite")
    assert n_sig == 0


def test_throughput_per_tick():
    det = IgniteDetector()
    rnd = random.Random(2)
    ticks = [(f"S{i % 500}", T0 + j * 0.01, 1 + rnd.random() * 0.01, 50 + rnd.random() * 50) for i, j in zip(range(200_000), range(200_000))]
    t = time.perf_counter()
    for sym, ts, p, v in ticks:
        det.on_tick(sym, ts, p, v)
    dt = time.perf_counter() - t
    assert dt < 4.0, f"20만 틱에 {dt:.1f}초 — 장 시작 쏠림을 감당 못 함"


def test_notify_flag_filter():
    assert should_send({"text": "x"}) and should_send({"text": "x", "notify": True})
    assert should_send({"text": "x", "notify": False}) is False


# ---------------- 백테스트(1분봉) + 채점 연결 ----------------
def build_bars_db(path, n_noise=60, spike_minute=60):
    """정규장 데이터. 심은 종목 PLANT: 60분 조용하다가 spike_minute 분부터 거래 폭발 + 가격 +3%→+8%→+12%. 나머지는 평범한 장."""
    from pennybot2.core.store import Store
    s = Store(path)
    start = datetime(2026, 11, 25, 14, 30, tzinfo=timezone.utc)          # 09:30 ET
    rnd = random.Random(11)
    rows = []

    def add(sym, m, o, h, l, c, v):
        t = datetime.fromtimestamp(start.timestamp() + m * 60, timezone.utc).isoformat()
        rows.append((sym, t, "2026-11-25", "REGULAR", o, h, l, c, v))

    for sym in [f"N{i}" for i in range(n_noise)] + ["PLANT"]:
        p = 1.0 if sym == "PLANT" else 1.0 + rnd.random()
        for m in range(240):
            o = p
            if sym == "PLANT" and spike_minute <= m < spike_minute + 4:
                c = {0: 1.03, 1: 1.08, 2: 1.12, 3: 1.14}[m - spike_minute] * 1.0
                v = {0: 60_000, 1: 80_000, 2: 50_000, 3: 30_000}[m - spike_minute]
            else:
                c = p if sym == "PLANT" else p * (1 + rnd.gauss(0, 0.002))
                v = 3000 * rnd.uniform(0.5, 1.5)
            h, l = max(o, c) * 1.001, min(o, c) * 0.999
            add(sym, m, o, h, l, c, v)
            p = c
    s.put_bars(rows)
    return s


def test_backtest_catches_planted_ignition_before_the_late_detector(tmp_path):
    from pennybot2.workers.ignite import backtest
    src = tmp_path / "bars.db"
    build_bars_db(src).close()
    out = tmp_path / "sig.db"
    counts = backtest(src, out)
    import sqlite3
    db = sqlite3.connect(out)
    rows = db.execute("SELECT ts, symbol, kind FROM signals ORDER BY ts").fetchall()
    plant = {k: t for t, s, k in rows if s == "PLANT"}
    assert "ignite:fast" in plant and "surge" in plant
    assert plant["ignite:fast"] < plant["surge"]                           # 조기 감지가 늦은 감지보다 먼저
    assert not [r for r in rows if r[1].startswith("N") and r[2].startswith("ignite")]     # 평범한 종목은 오탐 없음
    assert counts["ignite:fast"] >= 1
    # 신호 속성이 저장돼 있다
    import json
    attrs = json.loads(db.execute("SELECT attrs FROM signals WHERE symbol='PLANT' AND kind='ignite:fast'").fetchone()[0])
    assert attrs["pseudo"] is True and attrs["from_low_pct"] < 8


def test_backtest_does_not_fire_during_warmup(tmp_path):
    from pennybot2.workers.ignite import backtest
    src = tmp_path / "bars.db"
    build_bars_db(src, n_noise=2, spike_minute=10).close()                  # 시작 10분 만에 폭발 → 기준선이 없는 구간
    import sqlite3
    backtest(src, tmp_path / "sig.db")
    assert sqlite3.connect(tmp_path / "sig.db").execute("SELECT count(*) FROM signals WHERE symbol='PLANT'").fetchone()[0] == 0


def test_grade_with_alerts_db_compares_early_vs_late(tmp_path, capsys):
    from pennybot2.grading import run as grun
    from pennybot2.workers.ignite import backtest
    src = tmp_path / "bars.db"
    build_bars_db(src).close()
    out = tmp_path / "sig.db"
    backtest(src, out)
    assert grun.main([str(src), f"--alerts-db={out}", "--controls=200"]) == 0
    text = capsys.readouterr().out
    section = text.split("== 알림 종류별 ==")[1].split("== 세션별 ==")[0]
    assert "ignite:fast" in section and "surge" in section and "대조군" in section


def test_pseudo_ticks_shape():
    up = pseudo_ticks_ = __import__("pennybot2.workers.ignite", fromlist=["pseudo_ticks"]).pseudo_ticks(1000.0, 1.0, 1.2, 0.9, 1.1, 400)
    assert [round(p, 2) for _, p, _ in up] == [1.0, 0.9, 1.2, 1.1] and sum(v for *_, v in up) == 400     # 양봉: 시→저→고→종
    down = __import__("pennybot2.workers.ignite", fromlist=["pseudo_ticks"]).pseudo_ticks(1000.0, 1.1, 1.2, 0.9, 1.0, 400)
    assert [round(p, 2) for _, p, _ in down] == [1.1, 1.2, 0.9, 1.0]


def test_signal_saves_recent_ticks_for_later_tuning(tmp_path):
    det = IgniteDetector()
    t = quiet(det)
    sigs = burst(det, "ABC", t)
    s = next(s for _, s in sigs if s["kind"] == "ignite:fast")
    ticks = s["attrs"]["ticks"]
    assert 8 <= len(ticks) <= 200 and all(len(x) == 3 and x[0] <= 0 for x in ticks)       # [신호 기준 상대 초(≤0), 가격, 수량]
    assert abs(sum(p * v for _, p, v in ticks) - s["attrs"]["dv"]) / s["attrs"]["dv"] < 0.5     # 창 안 거래대금과 같은 규모


def test_hub_saves_signal_attrs(tmp_path):
    import json
    from pennybot2.core.store import Store
    from pennybot2.hub.client import HubClient
    from pennybot2.hub.server import Hub
    hub = Hub(tmp_path / "m.db", port=0, flush_sec=0.2).start_in_thread()
    pub = HubClient(hub.port)
    pub.connected.wait(3)
    pub.publish("signal", {"s": "ABC", "ts": T0, "kind": "ignite:fast", "text": "t", "value": 3.0, "price": 1.03,
                           "attrs": {"variant": "fast", "ratio": 9.1, "ticks": [[-1.0, 1.0, 100]]}, "notify": True})
    s = Store(tmp_path / "m.db")
    end = time.time() + 4
    while time.time() < end and not s.db.execute("SELECT count(*) FROM signals").fetchone()[0]:
        time.sleep(0.05)
    a = json.loads(s.db.execute("SELECT attrs FROM signals").fetchone()[0])
    assert a["variant"] == "fast" and a["ratio"] == 9.1 and a["ticks"] == [[-1.0, 1.0, 100]]
