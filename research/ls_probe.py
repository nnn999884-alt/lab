"""LS증권 OpenAPI 해외주식 실시간 웹소켓 연결 시험 (주문 없음, 조회만).

확인하려는 것
  1) 토큰이 나오나  2) 웹소켓(해외 체결 GSC) 주소·구독 형식이 맞나  3) 체결이 실시간인가 지연인가(체결 시각 vs 지금)
  4) 구독 종목을 늘리면 몇 개에서 막히나  5) 프리·정규·애프터·데이마켓 중 어느 세션에서 데이터가 오나

준비: .env 에 LS_APP_KEY, LS_APP_SECRET (또는 LS_APPKEY, LS_APPSECRET) 을 넣는다 (LS증권 OpenAPI 포털 openapi.ls-sec.co.kr 에서 앱 등록 → 앱키·시크릿 발급).
실행: .venv\\Scripts\\python research\\ls_probe.py [--seconds 60] [--max-subs 80] [--ws wss://...]
키 값은 화면에 출력하지 않는다. 토큰도 출력하지 않는다. 결과 요약은 state/ls_probe.json 에 저장.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import ssl
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
ET = ZoneInfo("America/New_York")
REST = "https://openapi.ls-sec.co.kr:8080"
WS_CANDIDATES = [
    "wss://openapi.ls-sec.co.kr:9443/websocket",
    "wss://openapi.ls-sec.co.kr:9443/websocket/overseas-stock",
    "wss://openapi.ls-sec.co.kr:29443/websocket",            # 모의투자 서버 (자료에 따라 이 포트가 보임)
]
EXCH = {"NASDAQ": "82", "NYSE": "81", "AMEX": "83", "NYSE_ARCA": "81"}   # LS 해외 거래소 코드 (자료 기준, 안 맞으면 응답 메시지로 드러남)


def load_env() -> dict:
    out = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def token(env: dict) -> str | None:
    import requests
    ak = env.get("LS_APP_KEY") or env.get("LS_APPKEY")
    sk = env.get("LS_APP_SECRET") or env.get("LS_APPSECRET")
    if not ak or not sk:
        print("❌ .env 에 LS_APP_KEY / LS_APP_SECRET 이 없습니다.")
        return None
    r = requests.post(f"{REST}/oauth2/token", headers={"content-type": "application/x-www-form-urlencoded"},
                      data={"grant_type": "client_credentials", "appkey": ak, "appsecretkey": sk, "scope": "oob"}, timeout=15)
    try:
        j = r.json()
    except Exception:
        j = {}
    print(f"1) 토큰 요청: HTTP {r.status_code} · token_type={j.get('token_type')} · expires_in={j.get('expires_in')} · "
          f"오류={j.get('error') or j.get('error_description') or j.get('msg1') or '-'}")
    return j.get("access_token")


def sample_symbols(n: int) -> list[tuple[str, str]]:
    base = [("NASDAQ", "AAPL"), ("NASDAQ", "TSLA"), ("NASDAQ", "NVDA")]
    try:
        con = sqlite3.connect(f"file:{(ROOT / 'state' / 'market.db').as_posix()}?mode=ro", uri=True, timeout=10)
        rows = con.execute("SELECT market, symbol FROM universe WHERE snapshot_date=(SELECT MAX(snapshot_date) FROM universe) "
                           "AND last BETWEEN 0.5 AND 20 ORDER BY symbol LIMIT ?", (n,)).fetchall()
        return base + [(m, s) for m, s in rows if (m, s) not in base]
    except Exception:
        return base


def frame_summary(raw: str) -> dict:
    try:
        j = json.loads(raw)
    except Exception:
        return {"raw": raw[:200]}
    h, b = j.get("header") or {}, j.get("body")
    return {"tr_cd": h.get("tr_cd"), "rsp_cd": (b or {}).get("rsp_cd") if isinstance(b, dict) else None,
            "rsp_msg": (b or {}).get("rsp_msg") if isinstance(b, dict) else None, "body": b, "header": h}


def run(ws_url: str, tok: str, seconds: int, max_subs: int, report: dict) -> bool:
    import websocket
    ws = websocket.create_connection(ws_url, timeout=10, sslopt={"cert_reqs": ssl.CERT_REQUIRED})
    print(f"2) 웹소켓 연결 성공: {ws_url}")
    syms = sample_symbols(max_subs)
    errors, frames, first_frames = [], 0, []
    subscribed = 0

    def sub(m: str, s: str, key_style: int) -> None:
        code = EXCH.get(m, "82")
        key = f"{code}{s}" if key_style == 0 else f"{code}{s:<16}"
        ws.send(json.dumps({"header": {"token": tok, "tr_type": "3"}, "body": {"tr_cd": "GSC", "tr_key": key}}))

    # 구독 형식 확인: AAPL 을 두 가지 키 형식으로
    sub("NASDAQ", "AAPL", 0)
    sub("NASDAQ", "AAPL", 1)
    t0 = time.time()
    ws.settimeout(2)
    next_batch = time.time() + 5
    idx = 3
    while time.time() - t0 < seconds:
        if time.time() >= next_batch and idx < len(syms):       # 5초마다 10개씩 늘림
            for m, s in syms[idx:idx + 10]:
                sub(m, s, 0)
                subscribed += 1
            idx += 10
            next_batch = time.time() + 5
        try:
            raw = ws.recv()
        except websocket.WebSocketTimeoutException:
            continue
        except Exception as e:
            print(f"   연결 끊김: {e}")
            report["disconnect"] = str(e)
            break
        if not raw:
            continue
        frames += 1
        fs = frame_summary(raw if isinstance(raw, str) else raw.decode("utf-8", "replace"))
        if fs.get("rsp_cd") not in (None, "00000") or (fs.get("rsp_msg") and fs.get("tr_cd") is None):
            errors.append({"after_subs": subscribed, "rsp_cd": fs.get("rsp_cd"), "rsp_msg": fs.get("rsp_msg")})
        if len(first_frames) < 12:
            first_frames.append({"local_et": datetime.now(ET).strftime("%H:%M:%S"), **{k: fs.get(k) for k in ("tr_cd", "rsp_cd", "rsp_msg", "body")}})
    ws.close()
    print(f"3) 받은 프레임 {frames}개 · 구독 시도 종목 {3 + subscribed}개 · 오류 응답 {len(errors)}개")
    for e in errors[:8]:
        print("   오류:", e)
    print("4) 앞쪽 프레임 (체결 시각 필드와 '지금(미 동부)'을 비교해 지연을 보세요):")
    for f in first_frames:
        print("  ", json.dumps(f, ensure_ascii=False)[:420])
    report.update({"ws": ws_url, "frames": frames, "subs_tried": 3 + subscribed, "errors": errors[:20], "first_frames": first_frames})
    return frames > 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--max-subs", type=int, default=80)
    ap.add_argument("--ws", default=None)
    a = ap.parse_args()
    env = load_env()
    tok = token(env)
    if not tok:
        return 1
    report: dict = {"at": datetime.now(timezone.utc).isoformat(), "et_now": datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S")}
    ok = False
    for url in ([a.ws] if a.ws else WS_CANDIDATES):
        try:
            ok = run(url, tok, a.seconds, a.max_subs, report)
        except Exception as e:
            print(f"   {url} 실패: {type(e).__name__}: {str(e)[:160]}")
            report.setdefault("ws_failures", []).append({"url": url, "err": f"{type(e).__name__}: {str(e)[:160]}"})
            continue
        if ok:
            break
    out = ROOT / "state" / "ls_probe.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"\n결과 저장: {out}")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
