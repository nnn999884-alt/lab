from pennybot.accum import compute


def test_accum_metrics_on_rising_series():
    bars = [(f"d{i:02d}", 1.0, 1.05, 0.95, 1.0 + i * 0.01, 1000.0 + 50 * i) for i in range(25)]
    out = compute(bars)
    assert out["ok"]
    assert out["obv_20_rise"] is True and out["ad_20_rise"] is True
    assert out["cmf_20"] > 0
    assert out["mfi_14"] > 50
    assert out["vwap_gap_pct"] is not None


def test_accum_needs_history():
    assert compute([]) == {"ok": False}
    assert compute([("d", 1, 1, 1, 1, 1)] * 3) == {"ok": False}
