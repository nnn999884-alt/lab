"""실시간 감지 호가 기록(trigger_quotes, origin='live') 과 소급 1분봉 트리거(early_study)를 맞춰 분리한다.
live_logged = 소급 트리거와 같은 종목·날, 봉 끝 시각 ±tol 초 안에 실시간 기록이 있음 · retro_only = 소급으로만 알 수 있음 · live_only = 실시간 기록만 있음."""
import pickle, sqlite3, sys
from collections import Counter, defaultdict
from pathlib import Path


def join(events, rows, tol=180.0):
    by = defaultdict(list)
    for r in rows: by[(r["symbol"], r["trade_date"])].append(r)
    used, out = set(), []
    for e in events:
        c = [r for r in by.get((e["sym"], e["td"]), []) if abs(r["bar_end_ts"] - e["ts_trig"]) <= tol]
        if c:
            r = min(c, key=lambda r: abs(r["bar_end_ts"] - e["ts_trig"])); used.add(r["id"])
            out.append(dict(sym=e["sym"], td=e["td"], cls="live_logged", status=r["status"], fresh=r["fresh"], warm_s=r["warm_s"]))
        else:
            out.append(dict(sym=e["sym"], td=e["td"], cls="retro_only", status=None, fresh=0, warm_s=None))
    live_only = [r for r in rows if r["id"] not in used]
    return out, live_only


def read_rows(con, since):
    try:
        cur = con.execute("SELECT id, symbol, trade_date, bar_end_ts, status, fresh, warm_s, session FROM trigger_quotes WHERE origin='live' AND trade_date >= ?", (since,))
    except sqlite3.OperationalError:
        return []
    return [dict(zip(("id", "symbol", "trade_date", "bar_end_ts", "status", "fresh", "warm_s", "session"), r)) for r in cur]


def report(events, rows):
    joined, live_only = join(events, rows)
    c = Counter(j["cls"] for j in joined)
    lines = [f"소급 트리거 {len(events)} → live_logged {c['live_logged']} · retro_only {c['retro_only']} · live_only(실시간 기록만) {len(live_only)}"]
    st = Counter(r["status"] for r in rows)
    lines.append("실시간 기록 상태: " + ", ".join(f"{k} {v}" for k, v in st.most_common()) if rows else "실시간 기록 없음 (로거가 아직 돌지 않았거나 장이 안 열림)")
    ok = sum(r["fresh"] for r in rows)
    lines.append(f"신선한 호가(status=ok): {ok}/{len(rows)}" + (f" ({ok/len(rows)*100:.0f}%)" if rows else ""))
    return "\n".join(lines)


if __name__ == "__main__":
    since = sys.argv[1] if len(sys.argv) > 1 else "2026-11-16"
    ev = [e for e in pickle.load(open("D:/pennybot-live/state/research/early_events_forward.pkl", "rb")) if e["td"] >= since]
    con = sqlite3.connect("file:D:/pennybot-live/state/market.db?mode=ro", uri=True)
    print(report(ev, read_rows(con, since)))
