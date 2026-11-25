"""LS 시세 일꾼: 웹소켓 체결(GSC)을 받아 tick 을 허브로 보내고, 1분이 끝나면 bar 도 보낸다.
종목은 마스터(g3190)에서 가격대로 골라 한 연결에 전부 구독(초당 90건씩 나눠서 — LS 는 초당 200건 넘으면 5초 차단)."""
from __future__ import annotations

import logging
import time

from ..hub.client import HubClient
from . import heartbeat
from .ls import frames
from .ls.auth import LsAuth, next_0700_kst
from .ls.master import fetch, pick

log = logging.getLogger(__name__)
WS_URL = "wss://openapi.ls-sec.co.kr:9443/websocket"
SUBS_PER_SEC = 90


def run(cfg, ws_url: str = WS_URL, auth=None, keys_fn=None) -> None:
    """auth·keys_fn·ws_url 은 테스트에서 가짜로 바꿔 끼우는 자리."""
    import websocket
    if auth is None:
        if not (cfg.ls_app_key and cfg.ls_app_secret):
            raise SystemExit("LS_APP_KEY / LS_APP_SECRET 이 .env 에 없습니다.")
        auth = LsAuth(cfg.ls_app_key, cfg.ls_app_secret, cfg.state_dir / "ls_token.json")
    keys_fn = keys_fn or (lambda: pick(fetch(auth), cfg.min_price, cfg.max_price))
    hub = HubClient(cfg.hub_port)
    stats = {"frames": 0, "ticks": 0, "bars": 0}
    heartbeat.start(hub, "ls", lambda: dict(stats))
    bars = frames.BarBuilder()
    force = False
    while True:
        try:
            token = auth.token(force=force)
            force = False
            keys = keys_fn()
            log.info("구독 종목 %d개", len(keys))
            ws = websocket.create_connection(ws_url, timeout=10)
            ws.settimeout(1.0)
            until = min(auth.valid_until(), next_0700_kst(time.time())) - 20
            pending, last_sub, last_ping, last_log = list(keys), 0.0, time.time(), time.time()
            try:
                while True:
                    now = time.time()
                    if now > until:
                        force = True
                        raise ConnectionError("토큰 만료 임박 → 새 토큰으로 다시 연결")
                    if pending and now - last_sub >= 1.0:
                        for k in pending[:SUBS_PER_SEC]:
                            ws.send(frames.sub_msg(token, "GSC", k))
                        pending, last_sub = pending[SUBS_PER_SEC:], now
                    if now - last_ping >= 25:
                        ws.ping()
                        last_ping = now
                    if now - last_log >= 60:
                        log.info("수신 %s", stats)
                        last_log = now
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not raw:
                        raise ConnectionError("서버가 연결을 닫음")
                    stats["frames"] += 1
                    res = frames.parse(raw if isinstance(raw, str) else raw.decode("utf-8", "replace"))
                    if not res:
                        continue
                    kind, d = res
                    if kind == "error":
                        log.warning("LS 웹소켓 응답: %s %s", *d)
                        if d[0] in ("IGW00121", "IGW00105", "IGW00106"):
                            force = True
                            raise ConnectionError("토큰 무효")
                        if d[0] == "10003":
                            time.sleep(6)
                            raise ConnectionError("전송 한도 초과 → 재연결")
                        continue
                    hub.publish("tick", d)
                    stats["ticks"] += 1
                    done = bars.add(d)
                    if done:
                        hub.publish("bar", done)
                        stats["bars"] += 1
            finally:
                try:
                    ws.close()
                except Exception:
                    pass
        except Exception as e:
            log.warning("LS 연결 끊김 (%s) → 5초 뒤 다시", e)
            time.sleep(5)
