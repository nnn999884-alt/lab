"""설정: config.yaml + .env (+ 환경변수로 상태 폴더·허브 포트 덮어쓰기). v2 에는 주문 코드가 없다 — 매매 일꾼을 만들 때 실거래 안전장치(mode: live + PENNYBOT_LIVE_CONFIRM=YES 둘 다)를 함께 둔다."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Config:
    state_dir: Path = ROOT / "state"
    min_price: float = 0.5
    max_price: float = 5.0
    hub_port: int = 8765
    webhook: str = ""
    telegram_token: str = ""
    telegram_chat: str = ""
    ls_app_key: str = field(default="", repr=False)
    ls_app_secret: str = field(default="", repr=False)

    @property
    def db_path(self) -> Path:
        return self.state_dir / "market.db"


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load(yaml_path: Path | None = None, env_path: Path | None = None) -> Config:
    _load_env(env_path or ROOT / ".env")
    p = yaml_path or ROOT / "config.yaml"
    raw = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.exists() else {}
    names = {f.name for f in fields(Config)}
    cfg = Config(**{k: v for k, v in raw.items() if k in names and k != "state_dir"})
    if "state_dir" in raw:
        cfg.state_dir = Path(raw["state_dir"])
    env = os.environ.get
    if env("PENNYBOT_STATE_DIR"):
        cfg.state_dir = Path(env("PENNYBOT_STATE_DIR"))
    if env("PENNYBOT_HUB_PORT"):
        cfg.hub_port = int(env("PENNYBOT_HUB_PORT"))
    cfg.webhook = env("PENNYBOT_WEBHOOK", cfg.webhook)
    cfg.telegram_token = env("PENNYBOT_TELEGRAM_TOKEN", cfg.telegram_token)
    cfg.telegram_chat = env("PENNYBOT_TELEGRAM_CHAT", cfg.telegram_chat)
    cfg.ls_app_key = env("LS_APP_KEY", "")
    cfg.ls_app_secret = env("LS_APP_SECRET", "")
    return cfg
