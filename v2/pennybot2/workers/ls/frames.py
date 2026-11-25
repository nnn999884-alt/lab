"""LS 웹소켓 프레임 해석 (네트워크 없음). GSC(체결) → tick, 1분 단위로 모아 bar.
GSC 필드: symbol price trdq(이번 체결수량) totq(누적수량) trdtm(미동부 HHMMSS) ovsdate(미국 날짜 YYYYMMDD)"""
from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def _f(x, d=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


MAX_SKEW = 300.0        # 체결 시각과 수신 시각이 이만큼(초) 넘게 어긋나면 의심


def reconcile_ts(tick: dict, now: float, max_skew: float = MAX_SKEW) -> bool:
    """체결 시각(ovsdate+trdtm 을 미동부로 가정)이 수신 시각과 크게 어긋나면 수신 시각으로 바꾼다 → 바꿨으면 True.
    LS 의 날짜 필드가 데이마켓(20~24시 ET)에서 '거래일'이면 하루 앞선 시각이 되는데, 그걸 실제로 확인하지 못했다(PLAN 보완점 4).
    어긋난 시각 그대로 쓰면 세션 판정·기준선이 전부 틀어지므로, 확인되기 전까지의 안전장치."""
    if abs(now - tick["ts"]) > max_skew:
        tick["ts"] = now
        return True
    return False


def sub_msg(token: str, tr_cd: str, key: str, on: bool = True) -> str:
    """구독(3)/해제(4) 메시지. key 는 마스터의 keysymbol (거래소 코드 2자 + 종목, 18자 오른쪽 공백)."""
    return json.dumps({"header": {"token": token, "tr_type": "3" if on else "4"},
                       "body": {"tr_cd": tr_cd, "tr_key": key.ljust(18)}})


def parse(raw: str):
    """프레임 → ("tick", dict) | ("error", (코드, 메시지)) | None."""
    try:
        j = json.loads(raw)
    except (TypeError, ValueError):
        return None
    h, b = j.get("header") or {}, j.get("body")
    if not b:
        code = h.get("rsp_cd")
        return ("error", (code, h.get("rsp_msg"))) if code not in (None, "", "00000") else None
    if h.get("tr_cd") != "GSC":
        return None
    sym, price = (b.get("symbol") or "").strip().upper(), _f(b.get("price"))
    if not sym or price <= 0:
        return None
    try:
        ts = datetime.strptime(f"{b.get('ovsdate')} {b.get('trdtm')}", "%Y%m%d %H%M%S").replace(tzinfo=ET).timestamp()
    except ValueError:
        return None
    return "tick", {"s": sym, "ts": ts, "p": price, "v": _f(b.get("trdq"))}


class BarBuilder:
    """tick 을 1분봉으로. 분이 바뀌는 순간 끝난 봉을 돌려준다 (늦게 도착한 옛 틱은 무시)."""

    def __init__(self) -> None:
        self._cur: dict[str, list] = {}      # symbol -> [minute, o, h, l, c, v]

    def add(self, t: dict) -> dict | None:
        m = int(t["ts"] // 60)
        cur = self._cur.get(t["s"])
        done = None
        if cur is None or m > cur[0]:
            if cur is not None:
                done = self._bar(t["s"], cur)
            self._cur[t["s"]] = [m, t["p"], t["p"], t["p"], t["p"], t["v"]]
        elif m == cur[0]:
            cur[2], cur[3], cur[4], cur[5] = max(cur[2], t["p"]), min(cur[3], t["p"]), t["p"], cur[5] + t["v"]
        return done

    @staticmethod
    def _bar(sym: str, c: list) -> dict:
        return {"s": sym, "ts": c[0] * 60, "o": c[1], "h": c[2], "l": c[3], "c": c[4], "v": c[5]}
