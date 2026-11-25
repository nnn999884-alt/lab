"""LS 접근 토큰. 서버가 매일 한국시간 07:00 에 토큰을 무효로 만들므로 그 전에 새로 받는다. 파일에 저장해 재시작 때 재사용."""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

REST = "https://openapi.ls-sec.co.kr:8080"
KST = timezone(timedelta(hours=9))
BAD_TOKEN_CODES = ("IGW00121", "IGW00105", "IGW00106")


def next_0700_kst(after: float) -> float:
    t = datetime.fromtimestamp(after, KST)
    b = t.replace(hour=7, minute=0, second=0, microsecond=0)
    return (b + timedelta(days=1) if b <= t else b).timestamp()


class LsAuth:
    def __init__(self, key: str, secret: str, path: Path):
        self.key, self.secret, self.path = key, secret, Path(path)
        self._lock = threading.Lock()

    def _load(self):
        try:
            j = json.loads(self.path.read_text(encoding="utf-8"))
            if j.get("key") == self.key[:6]:
                return j["token"], min(float(j["expires_at"]), next_0700_kst(float(j["issued_at"])))
        except Exception:
            pass
        return None

    def valid_until(self) -> float:
        cur = self._load()
        return cur[1] if cur else 0.0

    def token(self, force: bool = False) -> str:
        with self._lock:
            cur = self._load()
            if cur and not force and cur[1] - time.time() > 60:
                return cur[0]
            r = requests.post(f"{REST}/oauth2/token", headers={"content-type": "application/x-www-form-urlencoded"},
                              data={"grant_type": "client_credentials", "appkey": self.key,
                                    "appsecretkey": self.secret, "scope": "oob"}, timeout=15)
            j = r.json() if r.content else {}
            tok = j.get("access_token")
            if not tok:
                raise RuntimeError(f"LS 토큰 발급 실패 HTTP {r.status_code}: {j.get('error_description') or j.get('error') or j}")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"key": self.key[:6], "token": tok, "issued_at": time.time(),
                                       "expires_at": time.time() + int(j.get("expires_in") or 86400)}), encoding="utf-8")
            os.replace(tmp, self.path)
            return tok
