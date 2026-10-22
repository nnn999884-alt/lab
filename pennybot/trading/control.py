"""운영 제어 (state/control.json). 대시보드 버튼과 매매 봇이 이 파일로 소통한다.

  kill     : 신규 매수 전면 중단 (보유 종목 청산 규칙은 계속 동작)
  flatten  : 보유 종목 즉시 전량 청산 요청 (처리되면 봇이 false 로 되돌림)
  strategies: 전략별 켜기/끄기
예전 방식의 state/KILL 파일도 킬스위치로 인정한다.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


class Control:
    def __init__(self, state_dir: str | Path):
        self.dir = Path(state_dir)
        self.path = self.dir / "control.json"
        self.kill_file = self.dir / "KILL"

    def load(self) -> dict:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        d.setdefault("kill", False)
        d.setdefault("flatten", False)
        d.setdefault("strategies", {})
        if self.kill_file.exists():
            d["kill"] = True
        return d

    def save(self, d: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def update(self, **changes) -> dict:
        d = self.load()
        if "kill" in changes and not changes["kill"] and self.kill_file.exists():
            self.kill_file.unlink()
        for k, v in changes.items():
            if k == "strategy":
                name, enabled = v
                d["strategies"][name] = bool(enabled)
            else:
                d[k] = v
        self.save(d)
        return d

    def strategy_on(self, name: str, default: bool = True) -> bool:
        return bool(self.load()["strategies"].get(name, default))
