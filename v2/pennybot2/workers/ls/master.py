"""종목 마스터(g3190)로 구독할 종목 고르기. 가격대·거래정지·ETF류를 걸러 keysymbol 목록을 만든다."""
from __future__ import annotations

import json
import logging
import time

import requests

from .auth import BAD_TOKEN_CODES, REST, LsAuth

log = logging.getLogger(__name__)
SKIP_WORDS = (" ETF", " ETN", " WARRANT", " WARRANTS", " RIGHT", " RIGHTS", " UNIT", " UNITS", " FUND", " TRUST")


def pick(rows: list[dict], min_price: float, max_price: float) -> list[str]:
    """마스터 행 → 구독 키(keysymbol) 목록."""
    return [str(r["keysymbol"]).strip() for r in pick_rows(rows, min_price, max_price)]


def pick_rows(rows: list[dict], min_price: float, max_price: float) -> list[dict]:
    """마스터 행 중 쓸 만한 것. 기준가(clos)가 범위 밖이면 제외 (기준가 없으면 포함)."""
    out = []
    for r in rows:
        key = str(r.get("keysymbol") or "").strip()
        if not key or str(r.get("suspend") or "0").strip() not in ("", "0", "N"):
            continue
        name = " " + str(r.get("engname") or "").upper()
        if any(w in name for w in SKIP_WORDS):
            continue
        try:
            clos = float(r.get("clos") or 0)
        except ValueError:
            clos = 0
        if clos and not (min_price * 0.5 <= clos <= max_price * 2):      # 어제 기준가가 범위에서 많이 먼 종목은 오늘도 대개 범위 밖
            continue
        out.append(r)
    return out


def fetch(auth: LsAuth, max_pages: int = 60) -> list[dict]:
    rows, cv, cont, ckey = [], "", "N", ""
    for _ in range(max_pages):
        for attempt in (0, 1):
            h = {"content-type": "application/json; charset=UTF-8", "authorization": f"Bearer {auth.token(force=attempt == 1)}",
                 "tr_cd": "g3190", "tr_cont": cont, "tr_cont_key": ckey}
            body = {"g3190InBlock": {"delaygb": "R", "natcode": "US", "exgubun": "4", "readcnt": 500, "cts_value": cv}}
            r = requests.post(f"{REST}/overseas-stock/market-data", headers=h, data=json.dumps(body), timeout=20)
            j = r.json() if r.content else {}
            if attempt == 0 and (r.status_code == 401 or j.get("rsp_cd") in BAD_TOKEN_CODES):
                continue
            break
        page = j.get("g3190OutBlock1") or []
        rows += page
        cv = (j.get("g3190OutBlock") or {}).get("cts_value") or ""
        if not page or r.headers.get("tr_cont") != "Y":
            break
        cont, ckey = "Y", r.headers.get("tr_cont_key") or ""
        time.sleep(0.11)                                   # 이 TR 은 초당 10건
    log.info("LS 종목 마스터: %d종목", len(rows))
    return rows
