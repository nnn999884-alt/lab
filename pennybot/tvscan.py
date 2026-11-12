"""트레이딩뷰 스크리너 (로그인 쿠키, 실시간) — 전 종목의 지금 1분봉·5분봉·세션 거래량을 요청 한 번(약 0.6초)에.

토스 현재가엔 거래량이 없고 토스 1분봉은 종목마다 따로 불러야 해서(한도 초당 ~9번) 전 종목 거래대금을 몰랐다.
로그인 안 하면 15분 지연(update_mode=delayed_streaming_900), 유료 계정 쿠키(.env TV_SESSIONID · TV_SESSIONID_SIGN)면 streaming.
공식 API 가 아니라 약관상 계정 제한 위험이 있어 조회 간격을 넉넉히(기본 10초) 둔다. 데이마켓(Blue Ocean)은 안 나올 수 있음.
쿠키 값은 화면·로그에 절대 출력하지 않는다.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

URL = "https://scanner.tradingview.com/america/scan"
HEADERS = {"Origin": "https://www.tradingview.com", "Referer": "https://www.tradingview.com/",
           "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Content-Type": "application/json"}
COLS = ["name", "close", "change", "open|1", "high|1", "low|1", "close|1", "volume|1", "change|1",
        "close|5", "volume|5", "change|5", "premarket_change", "premarket_volume", "volume", "postmarket_volume",
        "market_cap_basic", "relative_volume_10d_calc", "update_mode"]
KEYS = ["name", "px", "chg", "o1", "h1", "l1", "c1", "v1", "chg1", "c5", "v5", "chg5", "pre_chg", "pre_vol", "vol",
        "post_vol", "mcap", "rvol", "mode"]


def from_env() -> "TVScan | None":
    sid, sign = os.environ.get("TV_SESSIONID", ""), os.environ.get("TV_SESSIONID_SIGN", "")
    return TVScan(sid, sign) if sid and sign else None


class TVScan:
    def __init__(self, sessionid: str, sessionid_sign: str, http=None, max_px: float = 20.0):
        import requests
        self._ck = {"sessionid": sessionid, "sessionid_sign": sessionid_sign}
        self._http = http or requests.Session()
        self.max_px = max_px
        self.realtime: bool | None = None                # 마지막 응답이 실시간(streaming)이었나

    def snapshot(self) -> dict[str, dict]:
        """→ {종목: {px, chg, o1..v1(지금 1분봉), c5·v5(지금 5분봉), pre_vol·vol·post_vol(세션 누적), mcap, rvol, mode, d1, d5}}.
        d1 · d5 = 지금 1분봉 · 5분봉 거래대금 ($). 오늘 거래가 있는 종목만 온다 (active_symbols_only)."""
        body = {"filter": [{"left": "type", "operation": "in_range", "right": ["stock", "dr"]},
                           {"left": "exchange", "operation": "in_range", "right": ["NASDAQ", "NYSE", "AMEX"]},
                           {"left": "close", "operation": "in_range", "right": [0.0001, self.max_px]}],
                "options": {"lang": "en", "active_symbols_only": True}, "markets": ["america"],
                "columns": COLS, "range": [0, 10000]}
        r = self._http.post(URL, json=body, headers=HEADERS, cookies=self._ck, timeout=15)
        r.raise_for_status()
        out = {}
        modes = set()
        for x in (r.json() or {}).get("data") or []:
            sym = str(x.get("s", "")).split(":")[-1]
            d = dict(zip(KEYS, x.get("d") or []))
            if not sym or not d.get("px"):
                continue
            d["d1"] = (d.get("c1") or 0) * (d.get("v1") or 0)
            d["d5"] = (d.get("c5") or 0) * (d.get("v5") or 0)
            modes.add(d.get("mode"))
            out[sym] = d
        self.realtime = "streaming" in modes if modes else None
        return out


def hot_list(snap: dict, universe, need_1m: float, low: set | None = None, top: int = 70) -> list[str]:
    """지금 돈이 들어오는 순서: 최근 5분봉 거래대금 큰 순 — 단, 오늘 +5%↑ 이거나 1분봉이 ±2%↑ 움직이고,
    1분봉 거래대금이 세션 기준(need_1m) 이상인 유니버스 종목. 뒷순위(low)는 오늘 +10%↑ 일 때만."""
    low = low or set()
    rows = []
    for s, d in snap.items():
        if s not in universe:
            continue
        chg, chg1 = d.get("chg") or 0, d.get("chg1") or 0
        if d["d1"] < need_1m or not (chg >= 5 or abs(chg1) >= 2):
            continue
        if s in low and chg < 10:
            continue
        rows.append((d["d5"], s))
    rows.sort(reverse=True)
    return [s for _, s in rows[:top]]
