from pennybot.precursor import is_moving


def m(d3, vx, sess=None, v3=1000):
    return {"dollar_3m": d3, "vol_x": vx, "vol_3m": v3, **({"session": sess} if sess else {})}


def test_premarket_runner_with_low_relative_volume_is_moving():
    # 11/04 프리 새벽 실제 값: CRIS +96% 달리는 중 — 3분 $37.8만·회전율 6.7%인데 '평소 대비' 는 1.1배라 예전엔 빠졌다
    assert is_moving(m(378_000, 1.10, "PRE"), 6.7)
    assert is_moving(m(182_000, 0.88, "PRE"), 1.47)       # HPAI
    assert is_moving(m(41_000, 2.79, "PRE"), 2.39)        # YFOR (3분 $4만)


def test_relative_volume_alone_is_enough_when_dollar_is_met():
    assert is_moving(m(39_000, 5.76, "PRE"), 0.002)       # SOUN: 회전율은 낮지만 거래량이 평소의 5배↑


def test_dollar_floor_differs_by_session():
    thin = m(30_000, 8.0)
    assert is_moving(thin, 0.5, "PRE") and is_moving(thin, 0.5, "AFTER")
    assert not is_moving(thin, 0.5, "REGULAR")             # 정규장은 3분 $10만 이상
    assert is_moving(m(12_000, 8.0), 0.5, "DAY") and not is_moving(m(12_000, 8.0), 0.5, "PRE")   # 데이는 $1만부터


def test_below_floor_or_no_trades_is_not_moving():
    assert not is_moving(m(8_000, 50.0, "PRE"), 5.0)       # 거래대금이 모자라면 배수·회전율이 아무리 커도 아님
    assert not is_moving(m(500_000, 9.0, "PRE", v3=0), 5.0)  # 3분 체결 없음
    assert not is_moving(m(378_000, 1.1, "PRE"), 0.05)     # 회전율도 배수도 둘 다 모자람


def test_session_is_read_from_live_metrics_when_not_given():
    assert is_moving(m(30_000, 8.0, "AFTER"), 0.5)
    assert not is_moving(m(30_000, 8.0), 0.5)               # 세션을 모르면 가장 엄격한 정규장 기준
