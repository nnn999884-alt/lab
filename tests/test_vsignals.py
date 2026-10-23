import json
from datetime import datetime, timedelta, timezone

from pennybot.collector import Store
from pennybot.collector.runner import Collector
from pennybot.config import CollectorConfig
from pennybot.precursor import daily_list_text


class Sent:
    def __init__(self):
        self.msgs = []

    def send(self, text, key=None, every=600, buttons=None):
        self.msgs.append((text, buttons))


def make(tmp_path):
    db = str(tmp_path / "m.db")
    st = Store(db)
    sent = Sent()
    col = Collector(None, st, None, CollectorConfig(db_path=db), notifier=sent, link=lambda: "https://x.example")
    col.universe = {"PENX": {"name": "펜엑스", "last": 1.0}}
    return col, st, sent


def test_virtual_buy_then_take_profit(tmp_path):
    col, st, sent = make(tmp_path)
    now = datetime(2026, 10, 23, 1, 0, tzinfo=timezone.utc)
    lv = {"metrics": {"price": 1.0, "chg": 5.0, "vol_x": 6.0, "ret60": 4.0}}
    col._vbuy(now, "2026-10-22", "PENX", 9, 0.95, lv, "DAY")
    col._vbuy(now, "2026-10-22", "PENX", 9, 0.95, lv, "DAY")             # 보유 중 → 중복 안 함
    assert st.query("SELECT COUNT(*) FROM vsignals")[0][0] == 1
    assert sent.msgs[0][0].startswith("🧪 가상 매수 (PENX · 펜엑스)") and "익절 $1.30" in sent.msgs[0][0]
    col.universe["PENX"]["last"] = 0.8                                    # −20%: 아직 보유 (손절 −35%)
    col.check_vsignals(now + timedelta(minutes=1), "DAY")
    assert st.query("SELECT status, low FROM vsignals")[0] == ("OPEN", 0.8)
    col.universe["PENX"]["last"] = 1.31                                   # +31% → 익절
    col.check_vsignals(now + timedelta(hours=5), "PRE")
    status, reason, hi = st.query("SELECT status, exit_reason, high FROM vsignals")[0]
    assert status == "CLOSED" and reason == "익절 +30%" and hi == 1.31
    text = sent.msgs[-1][0]
    assert text.startswith("🔵 가상 매도 (PENX · 펜엑스) · 익절 +30%") and "최저 −20.0%" in text


def test_no_chase_after_big_move(tmp_path):
    col, st, sent = make(tmp_path)
    lv = {"metrics": {"price": 1.3, "chg": 25.0, "vol_x": 6.0, "ret60": 10.0}}
    col._vbuy(datetime(2026, 10, 23, tzinfo=timezone.utc), "2026-10-22", "PENX", 9, 1.04, lv, "DAY")
    assert st.query("SELECT COUNT(*) FROM vsignals")[0][0] == 0 and not sent.msgs


def test_daily_list_text(tmp_path):
    col, st, sent = make(tmp_path)
    st.put_precursor_daily([{"d0": "2026-10-22", "symbol": "PENX", "score": 9, "close": 0.5, "created": "x",
                             "signals": json.dumps(["crashed", "spiked", "vol_up", "vol_up_strong", "accum"]),
                             "metrics": json.dumps({"dd60": -80, "vol_ratio": 5.0, "last_spike_d": "2026-10-01", "volume": 1e6})}])
    st.put_stock_info([{"symbol": "PENX", "name": "펜엑스", "shares": 1e7, "list_date": None, "updated": "x"}])
    st.put_stock_tags([{"d0": "2026-10-22", "symbol": "PENX", "tags": json.dumps(["ATM 진행"]), "info": "{}"}])
    text = daily_list_text(str(tmp_path / "m.db"), 5, "https://x.example")
    assert text.startswith("📋 오늘 급등 전 감시 목록 (2026-10-22 종가 기준)")
    assert "1. PENX" in text and "9점" in text and "회전율 10.0%" in text and "최근 급등 10-01" in text
    assert "🏷 거래량 폭발 · ATM 진행 · 매집 의심" in text
