import re, html, json, sys
from collections import Counter
D = r"C:\Users\dana\Downloads\Telegram Desktop\ChatExport_2026-11-16"
msgs = []
for f in ("messages.html", "messages2.html"):
    s = open(D + "\\" + f, encoding="utf-8").read()
    for m in re.finditer(r'<div class="message default clearfix[^"]*" id="message(\d+)">(.*?)(?=<div class="message (?:default|service)|\Z)', s, re.S):
        blk = m.group(2)
        d = re.search(r'title="(\d{2})\.(\d{2})\.(\d{4}) (\d{2}:\d{2}:\d{2})', blk)
        t = re.search(r'<div class="text">(.*?)</div>', blk, re.S)
        if not d or not t: continue
        txt = html.unescape(re.sub(r"<[^>]+>", "", re.sub(r"<br\s*/?>", "\n", t.group(1)))).strip()
        msgs.append({"id": int(m.group(1)), "ts": f"{d.group(3)}-{d.group(2)}-{d.group(1)} {d.group(4)}", "text": txt})
msgs.sort(key=lambda x: x["ts"])
json.dump(msgs, open(sys.argv[1], "w", encoding="utf-8"), ensure_ascii=False)
print(len(msgs), msgs[0]["ts"], msgs[-1]["ts"])
c = Counter(re.split(r"\n", m["text"])[0][:18] for m in msgs)
print(c.most_common(25))
