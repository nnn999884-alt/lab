from datetime import date, timedelta

from pennybot.bigruns import compare, daily_features, find_cases, pick_controls, run_anatomy


def series(sym_closes):
    d0 = date(2026, 1, 1)
    return [((d0 + timedelta(days=i)).isoformat(), c, c * 1.02, c * 0.98, c, 1000.0) for i, c in enumerate(sym_closes)]


def test_find_case_starts_at_first_big_day():
    closes = [2.0] * 40 + [1.0] * 30 + [1.1, 1.6, 3.0, 7.0, 5.0] + [5.0] * 30
    b = {"BIG": series(closes), "FLAT": series([1.0] * 105)}
    cases = find_cases(b, mult=6, win=20)
    assert len(cases) == 1
    c = cases[0]
    # 1.1(+10%) 다음 1.6(+45%)이 처음 +30% 튄 날 → D0 = 1.1 인 날
    assert b["BIG"][c["i0"]][4] == 1.1 and c["d1"] == b["BIG"][c["i0"] + 1][0]
    a = run_anatomy(b["BIG"], c["i0"], c["peak_i"])
    assert round(a["peak"]) == round((7.0 * 1.02 / 1.1 - 1) * 100) and a["days_to_peak"] == 3
    f = daily_features(b["BIG"], c["i0"])
    assert f["dd_60"] < -40 and f["spikes_60"] == 0 and f["price"] == 1.1
    ctrls = pick_controls(b, cases, k=2)
    assert ctrls and all(x["symbol"] == "FLAT" for x in ctrls)


def test_compare_lift():
    big = [{"f": {"price": 0.5, "below1_days": 30, "dd_60": -70, "up_from_low_60": 5, "box_20": 80, "atr_ratio": 1,
                  "vol_d0": 4, "vol_5_60": 1, "ret_d0": 0, "close_loc": .5, "spikes_60": 2, "max_spike_60": 150,
                  "dollar_20": 1e5}}] * 4
    ctl = [{"f": {**big[0]["f"], "dd_60": -10, "vol_d0": 1}}] * 8 + [big[0]] * 2
    rows = {r["condition"]: r for r in compare(big, ctl)}
    r = rows["D0 거래량 평소의 3배 이상"]
    assert r["big"] == 1.0 and r["control"] == 0.2 and r["lift"] == 5.0
    assert rows["$1 미만"]["lift"] == 1.0
