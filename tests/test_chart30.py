from datetime import date, timedelta

from pennybot.chart30 import NAMES, features, search


def mk(closes, vols=None, wick=None):
    d0 = date(2026, 1, 1)
    out = []
    for i, c in enumerate(closes):
        hi = c * 1.02 if not (wick and i in wick) else c * 1.3
        out.append(((d0 + timedelta(days=i)).isoformat(), c, hi, c * 0.98, c, (vols or [1000] * len(closes))[i]))
    return out


def test_features_shape():
    closes = [2.0 - i * 0.02 for i in range(60)]                 # 꾸준히 하락
    vols = [1000] * 59 + [5000]                                    # 마지막 날 거래량 5배
    b = mk(closes, vols, wick={45, 50})
    f = features(b, 59)
    assert set(f) == set(NAMES)
    assert f["ret_30"] < 0 and f["slope_30"] < 0 and f["r2_30"] > 0.95 and f["down_streak"] >= 29
    assert f["new_low_3"] == 1 and f["pos_30"] < 0.2
    assert round(f["vol_d0"], 1) == round(5000 / ((29 * 1000 + 5000) / 30), 1) and f["vol_spikes"] == 1 and f["vol_spike_ago"] == 0
    assert f["wick_days"] == 2


def test_search_keeps_only_conditions_confirmed_out_of_sample():
    # 'up_ratio' 가 높은 날만 급등하도록 만든 가짜 데이터 → 조건으로 잡히고 검증에서도 확인돼야 한다
    rows = []
    for k in range(600):
        d = (date(2026, 1, 1) + timedelta(days=k % 100)).isoformat()
        hot = k % 10 == 0
        f = {n: (k % 7) * 1.0 for n in NAMES}
        f["up_ratio"] = 0.9 if hot else 0.3 + (k % 5) * 0.01
        rows.append({"symbol": f"S{k}", "d": d, "f": f, "y100": hot, "y500": hot and k % 20 == 0})
    res = search(rows, 70, 30, (date(2026, 1, 1) + timedelta(days=70)).isoformat(), min_per_day=0.1)
    best = res["singles"][0]
    assert best["conditions"][0].startswith(NAMES["up_ratio"]) and best["lift100_test"] > 5
