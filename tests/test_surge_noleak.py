"""급등 연구 특징에 미래 데이터가 섞이면 안 된다 (11/15: 시간 창에 상한이 없어 5분 거래대금에 하루 뒤 거래량이 들어간 적 있음).
신호 봉 k 까지만 잘라서 계산한 특징 == 하루 전체로 계산한 특징 이어야 한다."""
import sys

import numpy as np

sys.path.insert(0, "D:/pennybot-live/research")
import surge_study as SS


def test_features_do_not_use_future_bars():
    rng = np.random.default_rng(3)
    n = 400
    ts = np.cumsum(rng.integers(40, 70, n)).astype(float) + 1.8e9
    c = 1.0 * np.cumprod(1 + rng.normal(0, 0.01, n)); c[300:] *= 1.5          # 신호 뒤에 크게 오르고 거래도 폭발
    o = np.concatenate([[c[0]], c[:-1]]); h = np.maximum(o, c) * 1.003; l = np.minimum(o, c) * 0.997
    v = rng.integers(100, 1000, n).astype(float); v[300:] *= 200
    k = 250
    full = SS.feats(ts, o, h, l, c, v, k, "REGULAR")
    cut = SS.feats(ts[:k + 1], o[:k + 1], h[:k + 1], l[:k + 1], c[:k + 1], v[:k + 1], k, "REGULAR")
    for key in full:
        a, b = full[key], cut[key]
        assert (a == b) or (isinstance(a, float) and np.isnan(a) and np.isnan(b)), f"{key}: 전체 {a} vs 신호까지 {b} — 미래 누수"
