import json, re, sys, statistics as st
from collections import Counter, defaultdict
m = json.load(open(sys.argv[1], encoding="utf-8"))
S = re.compile(r"[−–]")
sells, summ, calc = [], [], []
for x in m:
    t = S.sub("-", x["text"]); L = t.split("\n"); h = L[0]
    if ("매도" in h) and ("🏁" in h or "🏳" in h):
        g = re.search(r"수익\s+([+\-][\d.]+)%", t); w = re.search(r"사유\s+(.+)", t); k = re.search(r"·\s*(\S+\s*\S*)\s*$", h)
        sells.append({"ts": x["ts"], "virt": "(가상)" in h, "flag": "🏁" if "🏁" in h else "🏳", "sym": L[1].split("·")[0].strip(), "pnl": float(g.group(1)) if g else None,
                      "why": w.group(1).strip() if w else None, "kind": h.split("·")[-1].strip()})
    elif "신호 정리" in h:
        a = re.search(r"신호 (\d+)개 중 (\d+)개 체결", t); b = re.search(r"평균\s+([+\-][\d.]+)%", t); c = re.search(r"합계\s+([+\-][\d.]+)%", t); d = re.search(r"수익 (\d+)건 · 손실 (\d+)건", t)
        summ.append({"ts": x["ts"], "virt": "(가상)" in h, "sig": int(a.group(1)) if a else None, "fill": int(a.group(2)) if a else None,
                     "avg": float(b.group(1)) if b else None, "sum": float(c.group(1)) if c else None, "win": int(d.group(1)) if d else None, "loss": int(d.group(2)) if d else None})
    elif "재미로 보는" in h:
        a = re.search(r"끝 잔고 ([\d,]+)원", t); b = re.search(r"\(([+\-][\d,]+)원 · ([+\-][\d.]+)%\)", t)
        calc.append({"ts": x["ts"], "end": int(a.group(1).replace(",", "")) if a else None, "pct": float(b.group(2)) if b else None})
print("매도 전체", len(sells), "(가상)", sum(s["virt"] for s in sells), "| 아이콘", Counter(s["flag"] for s in sells), "| pnl 파싱", sum(s["pnl"] is not None for s in sells))
def rep(name, xs):
    p = [s["pnl"] for s in xs if s["pnl"] is not None]
    if not p: print(name, "없음"); return
    w = [v for v in p if v > 0]
    print(f"{name:24} {len(p):3}건 승률 {len(w)/len(p):4.0%} 평균 {st.mean(p):+6.2f}% 중앙 {st.median(p):+6.2f}% 합 {sum(p):+8.1f}%p | 이익평균 {st.mean(w) if w else 0:+.1f}% 손실평균 {st.mean([v for v in p if v<=0]) if any(v<=0 for v in p) else 0:+.1f}%")
rep("전체", sells)
for a, b, nm in (("2026-10-19", "2026-10-31", "10/19~10/31"), ("2026-11-01", "2026-11-08", "11/01~11/08"), ("2026-11-09", "2026-11-16", "11/09~11/16(최근)"), ("2026-11-12", "2026-11-16", "11/12~11/16(최근5일)")):
    rep(nm, [s for s in sells if a <= s["ts"][:10] <= b])
print("-- 실제(가상 아님) vs 가상, 최근 11/09~")
rec = [s for s in sells if s["ts"][:10] >= "2026-11-09"]
rep("최근 실제", [s for s in rec if not s["virt"]]); rep("최근 가상", [s for s in rec if s["virt"]])
print("-- 사유별(최근)")
for k, v in Counter(s["why"] for s in rec).most_common(8): rep("  " + str(k)[:20], [s for s in rec if s["why"] == k])
print("\n일일 정리(신호 → 체결 → 매도 평균/합계/손익 건수), 최근 14개")
for s in summ[-14:]: print(" ", s["ts"][:10], "가상" if s["virt"] else "실제", f"신호 {s['sig']} 체결 {s['fill']} 평균 {s['avg']} 합 {s['sum']} 수익 {s['win']} 손실 {s['loss']}")
print("\n재미로 보는 계산(500만원, 신호당 30만원):")
for c in calc[-15:]: print(" ", c["ts"][:10], c["end"], c["pct"])
cp = [c["pct"] for c in calc if c["pct"] is not None]
print("하루 평균 %.2f%% | 합 %.1f%% | 플러스 %d / %d일" % (st.mean(cp), sum(cp), sum(v > 0 for v in cp), len(cp)))
cr = [c["pct"] for c in calc if c["pct"] is not None and c["ts"][:10] >= "2026-11-09"]
print("최근(11/09~) 하루 평균 %.2f%% 합 %.1f%% 플러스 %d/%d일" % (st.mean(cr), sum(cr), sum(v > 0 for v in cr), len(cr)))
