import json
import threading
from datetime import datetime

from pennybot2.workers.ls import frames
from pennybot2.workers.ls.auth import KST, next_0700_kst
from pennybot2.workers.ls.master import pick


def gsc(sym="ABC", price="1.25", qty="100", t="093001", d="20261125"):
    return json.dumps({"header": {"tr_cd": "GSC", "tr_key": "82" + sym},
                       "body": {"symbol": sym, "price": price, "trdq": qty, "trdtm": t, "ovsdate": d}})


def test_parse_tick_et_time():
    kind, t = frames.parse(gsc())
    assert kind == "tick" and t["s"] == "ABC" and t["p"] == 1.25 and t["v"] == 100
    assert datetime.fromtimestamp(t["ts"], frames.ET).strftime("%H:%M:%S") == "09:30:01"


def test_parse_ignores_junk_and_reports_errors():
    assert frames.parse("not json") is None
    assert frames.parse(gsc(price="0")) is None
    assert frames.parse(gsc(t="zz")) is None
    ack = json.dumps({"header": {"rsp_cd": "00000", "rsp_msg": "ok"}})
    assert frames.parse(ack) is None
    err = json.dumps({"header": {"rsp_cd": "10003", "rsp_msg": "limit"}})
    assert frames.parse(err) == ("error", ("10003", "limit"))


def test_bar_builder_emits_on_minute_roll():
    bb, out = frames.BarBuilder(), []
    for t in ("093001", "093030", "093059", "093102"):
        out.append(bb.add(frames.parse(gsc(price={"093001": "1.0", "093030": "1.5", "093059": "1.2", "093102": "1.3"}[t], t=t))[1]))
    assert out[:3] == [None, None, None]
    bar = out[3]
    assert (bar["o"], bar["h"], bar["l"], bar["c"], bar["v"]) == (1.0, 1.5, 1.0, 1.2, 300)
    assert bb.add(frames.parse(gsc(price="9", t="093010"))[1]) is None      # 늦게 온 옛 틱은 무시


def test_sub_msg_pads_key():
    m = json.loads(frames.sub_msg("TOK", "GSC", "82ABC"))
    assert m["body"]["tr_key"] == "82ABC".ljust(18) and m["header"]["tr_type"] == "3"
    assert json.loads(frames.sub_msg("TOK", "GSC", "82ABC", on=False))["header"]["tr_type"] == "4"


def test_master_pick_filters():
    rows = [{"keysymbol": "82AAA", "clos": "1.2", "engname": "Aaa Inc"},
            {"keysymbol": "82BBB", "clos": "250", "engname": "Big Inc"},            # 가격대 밖
            {"keysymbol": "82CCC", "clos": "1.0", "engname": "Ccc ETF"},            # ETF
            {"keysymbol": "82DDD", "clos": "1.0", "engname": "Ddd", "suspend": "1"},  # 거래정지
            {"keysymbol": "82EEE", "clos": "", "engname": "Eee"},                   # 기준가 없음 → 포함
            {"clos": "1", "engname": "NoKey"}]
    assert pick(rows, 0.5, 5.0) == ["82AAA", "82EEE"]


def test_token_expiry_is_next_0700_kst():
    t = datetime(2026, 11, 25, 6, 0, tzinfo=KST).timestamp()
    assert datetime.fromtimestamp(next_0700_kst(t), KST).strftime("%m-%d %H:%M") == "11-25 07:00"
    t = datetime(2026, 11, 25, 8, 0, tzinfo=KST).timestamp()
    assert datetime.fromtimestamp(next_0700_kst(t), KST).strftime("%m-%d %H:%M") == "11-26 07:00"




def test_ls_feed_end_to_end_with_fake_server(tmp_path):
    """가짜 LS 웹소켓 서버: 구독 메시지를 받으면 체결 프레임 4개를 보낸다 → 허브 구독자가 tick 4개, bar 1개를 받는다."""
    import asyncio
    import time

    import websockets

    from pennybot2.core.config import Config
    from pennybot2.hub.client import HubClient
    from pennybot2.hub.server import Hub
    from pennybot2.workers import ls_feed

    subs = []

    async def handler(ws):
        async for m in ws:
            subs.append(json.loads(m))
            if len(subs) == 1:
                for price, t in (("1.0", "093001"), ("1.5", "093030"), ("1.2", "093059"), ("1.3", "093102")):
                    await ws.send(gsc(price=price, t=t))
                await ws.send(json.dumps({"header": {"rsp_cd": "00000"}}))     # 구독 확인 응답은 무시돼야 함

    started = threading.Event()
    box = {}

    def serve():
        async def main():
            async with websockets.serve(handler, "127.0.0.1", 0) as srv:
                box["port"] = srv.sockets[0].getsockname()[1]
                started.set()
                await asyncio.Future()
        try:
            asyncio.run(main())
        except Exception:
            pass

    threading.Thread(target=serve, daemon=True).start()
    assert started.wait(5)

    hub = Hub(tmp_path / "m.db", port=0, flush_sec=0.2).start_in_thread()
    got = []
    sub = HubClient(hub.port, ["tick", "bar"], lambda t, d: got.append((t, d)))
    sub.connected.wait(3)

    class FakeAuth:
        def token(self, force=False):
            return "TOK"

        def valid_until(self):
            return time.time() + 3600

    cfg = Config(hub_port=hub.port, state_dir=tmp_path)
    threading.Thread(target=ls_feed.run, daemon=True, kwargs=dict(
        cfg=cfg, ws_url=f"ws://127.0.0.1:{box['port']}", auth=FakeAuth(), keys_fn=lambda: ["82ABC"])).start()

    end = time.time() + 6
    while time.time() < end and sum(1 for t, _ in got if t == "tick") < 4:
        time.sleep(0.05)
    kinds = [t for t, _ in got]
    assert kinds.count("tick") == 4
    time.sleep(0.2)
    assert [d for t, d in got if t == "bar"][0]["c"] == 1.2       # 09:30 봉 마감
    assert subs[0]["body"] == {"tr_cd": "GSC", "tr_key": "82ABC".ljust(18)} and subs[0]["header"]["token"] == "TOK"
