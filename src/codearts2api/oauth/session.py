"""Persist and load OAuth session credentials under a local data directory.

Secrets are never logged. The session file is written with mode 0o600.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from codearts2api.oauth.dpop import DPoPKeyPair

logger = logging.getLogger(__name__)

SESSION_FILENAME = "session.json"
REFRESH_SKEW_SECONDS = 600  # official CLI uses 600_000 ms


def _parse_expires_at(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:  # milliseconds
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    text = str(value).strip()
    if text.isdigit():
        return _parse_expires_at(int(text))
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass(slots=True)
class OAuthSession:
    access_key_id: str
    secret_access_key: str
    security_token: str | None = None
    refresh_token: str | None = None
    expires_at: datetime | None = None
    client_id: str = "CodeArts_Tui"
    account_id: str | None = None
    dpop: DPoPKeyPair | None = None
    source: str = "oauth"

    def is_expired(self, *, skew_seconds: int = REFRESH_SKEW_SECONDS, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        current = now or datetime.now(timezone.utc)
        expires = self.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return current.timestamp() >= (expires.timestamp() - skew_seconds)

    def can_refresh(self) -> bool:
        return bool(self.refresh_token and self.dpop is not None)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source": self.source,
            "client_id": self.client_id,
            "access_key_id": self.access_key_id,
            "secret_access_key": self.secret_access_key,
            "security_token": self.security_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "account_id": self.account_id,
        }
        if self.dpop is not None:
            payload["dpop"] = self.dpop.to_dict()
        return payload

    def public_view(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "client_id": self.client_id,
            "has_access_key": bool(self.access_key_id),
            "has_security_token": bool(self.security_token),
            "has_refresh_token": bool(self.refresh_token),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "expired": self.is_expired(),
            "can_refresh": self.can_refresh(),
            "account_id": self.account_id,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "OAuthSession":
        dpop_raw = payload.get("dpop") or payload.get("dpop_key_pair")
        dpop = DPoPKeyPair.from_dict(dpop_raw) if isinstance(dpop_raw, dict) else None
        return cls(
            access_key_id=str(payload.get("access_key_id") or payload.get("accessKeyId") or ""),
            secret_access_key=str(payload.get("secret_access_key") or payload.get("secretAccessKey") or ""),
            security_token=payload.get("security_token") or payload.get("securitytoken") or payload.get("securityToken"),
            refresh_token=payload.get("refresh_token") or payload.get("refreshToken"),
            expires_at=_parse_expires_at(payload.get("expires_at") or payload.get("expiration") or payload.get("expiresAt")),
            client_id=str(payload.get("client_id") or "CodeArts_Tui"),
            account_id=payload.get("account_id") or payload.get("accountId"),
            dpop=dpop,
            source=str(payload.get("source") or "oauth"),
        )


class SessionStore:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.path = self.directory / SESSION_FILENAME

    def load(self) -> OAuthSession | None:
        if not self.path.is_file():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.exception("Failed to read OAuth session file")
            return None
        if not isinstance(payload, dict):
            return None
        session = OAuthSession.from_dict(payload)
        if not session.access_key_id or not session.secret_access_key:
            return None
        return session

    def save(self, session: OAuthSession) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(session.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)
        os.chmod(self.path, 0o600)
        logger.info("OAuth session saved to %s", self.path)
        return self.path

    def clear(self) -> None:
        if self.path.is_file():
            self.path.unlink()
            logger.info("OAuth session file removed")
