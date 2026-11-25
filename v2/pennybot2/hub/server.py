"""허브: 모든 일꾼이 데이터를 보내는 한 곳. 받은 줄을 구독자에게 그대로 넘기고(재인코딩 없음),
저장은 1초마다 모아서 다른 스레드가 한다. 느린 구독자는 그 구독자 몫만 버린다 — 허브는 기다리지 않는다.

메시지는 JSON 한 줄: {"op":"pub","topic":"tick","d":{...}} · {"op":"sub","topics":[...]} · {"op":"snap"}
토픽: tick {s,ts,p,v} · bar {s,ts,o,h,l,c,v} · signal {s,ts,kind,text,value}   (ts = epoch 초)
      candidates {asof, items:[{s,score,why,minus}]} (마지막 것을 기억해 새 구독자에게 줌)
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from ..core.sessions import classify
from ..core.store import Store

log = logging.getLogger(__name__)
RETAIN = {"candidates"}   # 마지막 메시지를 기억했다가 새로 구독하는 쪽에 바로 준다
MAX_BACKLOG = 1 << 20      # 구독자 쓰기 대기 1MB 넘으면 그 구독자 메시지는 버림


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


class Hub:
    def __init__(self, db_path, host: str = "127.0.0.1", port: int = 8765, flush_sec: float = 1.0):
        self.db_path, self.host, self.port, self.flush_sec = db_path, host, port, flush_sec
        self.last: dict[str, list] = {}               # symbol -> [ts, price]
        self.clients: dict[asyncio.StreamWriter, set] = {}
        self.dropped = 0
        self._retained: dict[str, bytes] = {}
        self._bars: list[dict] = []
        self._signals: list[dict] = []
        self._pool = ThreadPoolExecutor(1)
        self._store: Store | None = None
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    # ---- 수신·전달 (지연에 민감한 경로: 디스크 없음) ----
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.clients[writer] = set()
        try:
            async for line in reader:
                try:
                    msg = json.loads(line)
                    op = msg.get("op")
                    if op == "pub":
                        self._on_pub(msg["topic"], msg["d"], line)
                    elif op == "sub":
                        self.clients[writer] = set(msg.get("topics", ()))
                        for t in self.clients[writer] & self._retained.keys():
                            writer.write(self._retained[t])
                    elif op == "snap":
                        writer.write((json.dumps({"op": "snap", "last": self.last}) + "\n").encode())
                except (ValueError, KeyError, AttributeError):
                    log.warning("잘못된 메시지 무시: %r", line[:100])
        except (ConnectionError, asyncio.LimitOverrunError):
            pass
        finally:
            self.clients.pop(writer, None)
            writer.close()

    def _on_pub(self, topic: str, d: dict, raw: bytes) -> None:
        if topic == "tick":
            self.last[d["s"]] = [d["ts"], d["p"]]
        elif topic == "bar":
            self.last[d["s"]] = [d["ts"], d["c"]]
            self._bars.append(d)
        elif topic == "signal":
            self._signals.append(d)
        if topic in RETAIN:
            self._retained[topic] = raw
        for w, topics in list(self.clients.items()):
            if topic in topics:
                if w.transport.get_write_buffer_size() > MAX_BACKLOG:
                    self.dropped += 1
                    continue
                w.write(raw)

    # ---- 저장 (느려도 되는 경로: 별도 스레드, 모아서) ----
    def _flush(self, bars: list[dict], signals: list[dict], last: dict) -> None:
        if self._store is None:
            self._store = Store(self.db_path)
        rows = []
        for b in bars:
            ts = datetime.fromtimestamp(b["ts"], timezone.utc)
            td, ss = classify(ts)
            rows.append((b["s"], ts.isoformat(), td.isoformat() if td else None, ss,
                         b["o"], b["h"], b["l"], b["c"], b["v"]))
        if rows:
            self._store.put_bars(rows)
        for s in signals:
            self._store.add_event(_iso(s["ts"]), s["s"], s["kind"], s["text"], s.get("value"))
        self._store.db.executemany("INSERT OR REPLACE INTO price_now VALUES (?,?,?)",
                                   [(sym, _iso(v[0]), v[1]) for sym, v in last.items()])
        self._store.db.commit()

    async def _flusher(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(self.flush_sec)
            await self.flush_now(loop)

    async def flush_now(self, loop=None) -> None:
        bars, sigs, self._bars, self._signals = self._bars, self._signals, [], []
        try:
            await (loop or asyncio.get_running_loop()).run_in_executor(
                self._pool, self._flush, bars, sigs, dict(self.last))
        except Exception:
            log.exception("저장 실패 — 다음 주기에 이어서 (허브 전달은 계속)")
            self._bars[:0], self._signals[:0] = bars, sigs

    # ---- 실행 ----
    async def serve(self) -> None:
        server = await asyncio.start_server(self._handle, self.host, self.port, limit=1 << 20)
        self.port = server.sockets[0].getsockname()[1]
        self._loop = asyncio.get_running_loop()
        self._main = asyncio.current_task()
        flusher = asyncio.create_task(self._flusher())
        self._ready.set()
        log.info("허브 시작 %s:%s", self.host, self.port)
        try:
            async with server:
                await server.serve_forever()
        finally:
            flusher.cancel()
            await self.flush_now()

    def serve_forever(self) -> None:
        try:
            asyncio.run(self.serve())
        except asyncio.CancelledError:
            pass

    def start_in_thread(self) -> "Hub":
        """테스트·통합 실행용."""
        t = threading.Thread(target=self.serve_forever, daemon=True)
        t.start()
        if not self._ready.wait(5):
            raise RuntimeError("허브가 시작되지 않음")
        return self

    def stop(self) -> None:
        if self._loop:
            fut = asyncio.run_coroutine_threadsafe(self.flush_now(), self._loop)
            fut.result(5)

    def shutdown(self) -> None:
        """허브를 끈다 (남은 데이터는 저장하고 등록된 접속을 끊는다). 테스트·통합용 — 실제 운영에서 프로세스가 끝나면 OS 가 소켓을 모두 닫는다."""
        if self._loop:
            def _close():
                for w in list(self.clients):
                    w.close()
                self._main.cancel()
            self._loop.call_soon_threadsafe(_close)
