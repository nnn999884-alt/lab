"""재생 일꾼: 저장소의 1분봉을 허브로 흘린다. 실시간 일꾼과 같은 통로라 감지기는 구분하지 못한다(= 백테스트)."""
from __future__ import annotations

from datetime import datetime

from ..core.store import Store
from ..hub.client import HubClient


def run(cfg, symbols: list[str] | None = None) -> int:
    client = HubClient(cfg.hub_port)
    client.connected.wait(10)
    q, args = "SELECT * FROM bars_1m", []
    if symbols:
        q += f" WHERE symbol IN ({','.join('?' * len(symbols))})"
        args = list(symbols)
    n = 0
    for r in Store(cfg.db_path).db.execute(q + " ORDER BY ts", args):
        ts = datetime.fromisoformat(r["ts"].replace("Z", "+00:00")).timestamp()
        client.publish("bar", {"s": r["symbol"], "ts": ts, "o": r["open"], "h": r["high"],
                               "l": r["low"], "c": r["close"], "v": r["volume"]})
        n += 1
    return n
