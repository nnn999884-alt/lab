"""설정: config.yaml + .env. 기본은 모의매매. 실거래는 mode: live 와 PENNYBOT_LIVE_CONFIRM=YES 둘 다 있어야 한다."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Config:
    mode: str = "paper"
    state_dir: Path = ROOT / "state"
    paper_cash: float = 2000.0
    min_price: float = 0.5
    max_price: float = 5.0
    scan_seconds: int = 15
    webhook: str = ""
    telegram_token: str = ""
    telegram_chat: str = ""
    ls_app_key: str = field(default="", repr=False)
    ls_app_secret: str = field(default="", repr=False)

    @property
    def live(self) -> bool:
        return self.mode == "live" and os.environ.get("PENNYBOT_LIVE_CONFIRM") == "YES"

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
    if cfg.mode not in ("paper", "live"):
        raise ValueError(f"mode 는 paper 또는 live: {cfg.mode!r}")
    env = os.environ.get
    cfg.webhook = env("PENNYBOT_WEBHOOK", cfg.webhook)
    cfg.telegram_token = env("PENNYBOT_TELEGRAM_TOKEN", cfg.telegram_token)
    cfg.telegram_chat = env("PENNYBOT_TELEGRAM_CHAT", cfg.telegram_chat)
    cfg.ls_app_key = env("LS_APP_KEY", "")
    cfg.ls_app_secret = env("LS_APP_SECRET", "")
    return cfg
