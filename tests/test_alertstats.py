from pennybot.alertperf import breakdown, grade_of, session_of


def test_session_of_uses_eastern_time():
    assert session_of("2026-10-23T11:10:00+00:00") == "PRE"       # 07:10 ET
    assert session_of("2026-10-23T14:00:00+00:00") == "REG"       # 10:00 ET
    assert session_of("2026-10-23T21:00:00+00:00") == "AFTER"     # 17:00 ET
    assert session_of("2026-10-24T01:00:00+00:00") == "DAY"       # 21:00 ET
    assert session_of("2026-10-23T07:00:00+00:00") == "DAY"       # 03:00 ET


def test_grade_of_reads_event_text():
    assert grade_of("📶 X 거래 터짐 (+5.0% · 3분 $90K · PRE · 회전 1.20% · A등급)") == "A"
    assert grade_of("🚨 X 급등 전조 (프리 06~09시 · +8.0%)") == "-"


def test_breakdown_groups_and_averages():
    rs = [
        {"ts": "2026-10-23T11:10:00+00:00", "kind": "burst", "text": "A등급", "price": 1.0, "hit30": True, "result": "win",
         "h3_last": 10.0, "h3_high": 30.0, "h3_low": -2.0},
        {"ts": "2026-10-23T11:20:00+00:00", "kind": "burst", "text": "A등급", "price": 1.0, "hit30": False, "result": "loss",
         "h3_last": -4.0, "h3_high": 6.0, "h3_low": -12.0},
        {"ts": "2026-10-23T11:20:00+00:00", "kind": "old", "text": "", "price": 1.0, "hit30": False, "result": None},
        {"ts": "2026-10-23T11:20:00+00:00", "kind": "burst", "text": "", "price": None},
    ]
    bd = breakdown(rs)
    x = bd[("burst", "PRE", "A")]
    assert (x["n"], x["win"], x["loss"], x["hit30"]) == (2, 1, 1, 1)
    assert x["last"] / x["h3n"] == 3.0 and len(bd) == 1
