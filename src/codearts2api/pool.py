"""Persisted multi-account credential pool and runtime settings overlay."""

from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from pydantic import SecretStr

from codearts2api.config import OVERRIDABLE_FIELDS, Settings
from codearts2api.oauth.session import SESSION_FILENAME, OAuthSession, SessionStore

logger = logging.getLogger(__name__)

CONFIG_FILENAME = "config.json"
ENV_ACCOUNT_ID = "env"
DEFAULT_OAUTH_NAME = "oauth"
FAILOVER_STATUSES = frozenset({401, 403, 429, *range(500, 600)})
ACCOUNT_KINDS = frozenset({"aksk", "oauth"})
POOL_STRATEGIES = frozenset({"round_robin", "first_available", "random"})


def is_failover_status(status_code: int) -> bool:
    return status_code in FAILOVER_STATUSES


def mask_secret(value: str | None, *, prefix: int = 4) -> str | None:
    if not value:
        return None
    if len(value) <= prefix:
        return "****"
    return value[:prefix] + "****"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_in_timezone(tz_name: str) -> datetime:
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        tz = timezone.utc
    return datetime.now(tz)


@dataclass
class Account:
    id: str
    name: str
    kind: str
    enabled: bool = True
    note: str = ""
    ak: str | None = None
    sk: str | None = None
    security_token: str | None = None
    project_id: str | None = None
    session_file: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    last_checkin: dict[str, Any] | None = None
    last_checkin_at: str | None = None
    last_error: str | None = None
    last_benefit: dict[str, Any] | None = None
    last_benefit_at: str | None = None

    def has_aksk(self) -> bool:
        return bool(self.ak and self.sk)

    def to_storage(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "enabled": self.enabled,
            "note": self.note,
            "ak": self.ak,
            "sk": self.sk,
            "security_token": self.security_token,
            "project_id": self.project_id,
            "session_file": self.session_file,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_checkin": self.last_checkin,
            "last_checkin_at": self.last_checkin_at,
            "last_error": self.last_error,
            "last_benefit": self.last_benefit,
            "last_benefit_at": self.last_benefit_at,
        }

    @classmethod
    def from_storage(cls, payload: dict[str, Any]) -> "Account":
        kind = str(payload.get("kind") or "aksk").strip().lower()
        if kind not in ACCOUNT_KINDS:
            kind = "aksk"
        return cls(
            id=str(payload.get("id") or uuid.uuid4()),
            name=str(payload.get("name") or payload.get("id") or "account"),
            kind=kind,
            enabled=bool(payload.get("enabled", True)),
            note=str(payload.get("note") or ""),
            ak=payload.get("ak") or None,
            sk=payload.get("sk") or None,
            security_token=payload.get("security_token") or None,
            project_id=payload.get("project_id") or None,
            session_file=payload.get("session_file") or None,
            created_at=payload.get("created_at"),
            updated_at=payload.get("updated_at"),
            last_checkin=payload.get("last_checkin") if isinstance(payload.get("last_checkin"), dict) else None,
            last_checkin_at=payload.get("last_checkin_at"),
            last_error=payload.get("last_error"),
            last_benefit=payload.get("last_benefit") if isinstance(payload.get("last_benefit"), dict) else None,
            last_benefit_at=payload.get("last_benefit_at"),
        )


class AccountPool:
    """Source of truth for accounts and overridable runtime settings (data/config.json)."""

    def __init__(self, settings: Settings, session_store: SessionStore | None = None) -> None:
        self.settings = settings
        self.data_dir = Path(settings.data_dir)
        self.path = self.data_dir / CONFIG_FILENAME
        self.session_store = session_store or SessionStore(settings.auth_dir)
        self.accounts: list[Account] = []
        self.persisted_settings: dict[str, Any] = {}
        self.api_key_persisted = False
        self._lock = threading.RLock()
        self._rr_index = 0
        self._cooldowns: dict[str, float] = {}
        self._cooldown_until_wall: dict[str, str] = {}
        self._cooldown_status: dict[str, int] = {}
        self._loaded = False

    def load_or_migrate(self) -> None:
        with self._lock:
            self._load_file()
            self._apply_persisted_settings()
            changed = self._migrate()
            if changed or not self.path.is_file():
                # Always write after first load so the file becomes the source of truth
                # once any account was seeded; skip write if still empty and nothing to persist.
                if self.accounts or self.persisted_settings:
                    self._save_unlocked()
            self._loaded = True

    def _load_file(self) -> None:
        if not self.path.is_file():
            self.accounts = []
            self.persisted_settings = {}
            self.api_key_persisted = False
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.exception("Failed to read pool config %s", self.path)
            self.accounts = []
            self.persisted_settings = {}
            return
        if not isinstance(payload, dict):
            self.accounts = []
            self.persisted_settings = {}
            return
        raw_accounts = payload.get("accounts") or []
        self.accounts = [
            Account.from_storage(item) for item in raw_accounts if isinstance(item, dict)
        ]
        settings_block = payload.get("settings") if isinstance(payload.get("settings"), dict) else {}
        self.persisted_settings = dict(settings_block)
        self.api_key_persisted = bool(self.persisted_settings.get("api_key"))
        pool_block = payload.get("pool") if isinstance(payload.get("pool"), dict) else {}
        if "rr_index" in pool_block:
            try:
                self._rr_index = int(pool_block["rr_index"])
            except (TypeError, ValueError):
                self._rr_index = 0

    def _save_unlocked(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "settings": self.persisted_settings,
            "accounts": [account.to_storage() for account in self.accounts],
            "pool": {"rr_index": self._rr_index},
        }
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)
        os.chmod(self.path, 0o600)
        logger.info("Runtime config saved to %s accounts=%s", self.path, len(self.accounts))

    def save(self) -> None:
        with self._lock:
            self._save_unlocked()

    def _migrate(self) -> bool:
        changed = False
        if self.settings.has_credentials() and not self.accounts:
            env_account = self._build_env_account()
            self.accounts.append(env_account)
            logger.info("Seeded account pool from CODEARTS_AK/SK id=%s", env_account.id)
            changed = True
        elif self.settings.has_credentials():
            existing = self.get(ENV_ACCOUNT_ID)
            if existing is not None and existing.kind == "aksk":
                if self._sync_env_account(existing):
                    changed = True

        if self._legacy_session_exists() and not self._account_uses_session_file(SESSION_FILENAME):
            account = self._build_oauth_account(
                account_id=str(uuid.uuid4()),
                name=DEFAULT_OAUTH_NAME,
                session_file=SESSION_FILENAME,
            )
            self.accounts.append(account)
            logger.info("Imported OAuth session.json as pool account id=%s", account.id)
            changed = True
        return changed

    def _build_env_account(self) -> Account:
        token = None
        if self.settings.codearts_security_token:
            token = self.settings.codearts_security_token.get_secret_value() or None
        now = utc_now_iso()
        return Account(
            id=ENV_ACCOUNT_ID,
            name="env",
            kind="aksk",
            enabled=True,
            note="Seeded from CODEARTS_AK/SK environment variables",
            ak=self.settings.codearts_ak.get_secret_value() if self.settings.codearts_ak else None,
            sk=self.settings.codearts_sk.get_secret_value() if self.settings.codearts_sk else None,
            security_token=token,
            project_id=self.settings.codearts_project_id,
            created_at=now,
            updated_at=now,
        )

    def _sync_env_account(self, account: Account) -> bool:
        ak = self.settings.codearts_ak.get_secret_value() if self.settings.codearts_ak else None
        sk = self.settings.codearts_sk.get_secret_value() if self.settings.codearts_sk else None
        token = None
        if self.settings.codearts_security_token:
            token = self.settings.codearts_security_token.get_secret_value() or None
        project_id = self.settings.codearts_project_id
        changed = False
        if account.ak != ak or account.sk != sk or account.security_token != token or account.project_id != project_id:
            account.ak = ak
            account.sk = sk
            account.security_token = token
            account.project_id = project_id
            account.updated_at = utc_now_iso()
            changed = True
        return changed

    def _legacy_session_exists(self) -> bool:
        path = Path(self.settings.auth_dir) / SESSION_FILENAME
        return path.is_file()

    def _account_uses_session_file(self, filename: str) -> bool:
        return any(
            account.kind == "oauth" and (account.session_file or SESSION_FILENAME) == filename
            for account in self.accounts
        )

    def _build_oauth_account(self, *, account_id: str, name: str, session_file: str) -> Account:
        now = utc_now_iso()
        return Account(
            id=account_id,
            name=name,
            kind="oauth",
            enabled=True,
            session_file=session_file,
            created_at=now,
            updated_at=now,
        )

    def _apply_persisted_settings(self) -> None:
        if not self.persisted_settings:
            return
        apply_settings_overlay(self.settings, self.persisted_settings)
        if self.persisted_settings.get("api_key"):
            self.api_key_persisted = True

    def get(self, account_id: str) -> Account | None:
        for account in self.accounts:
            if account.id == account_id:
                return account
        return None

    def find_by_name(self, name: str) -> Account | None:
        needle = name.strip().lower()
        for account in self.accounts:
            if account.name.strip().lower() == needle:
                return account
        return None

    def enabled_accounts(self, *, kind: str | None = None) -> list[Account]:
        result: list[Account] = []
        for account in self.accounts:
            if not account.enabled:
                continue
            if kind is not None and account.kind != kind:
                continue
            result.append(account)
        return result

    def session_store_for(self, account: Account) -> SessionStore:
        filename = account.session_file or f"{account.id}.json"
        return self.session_store.for_file(filename)

    def attach_session(self, account: Account, session: OAuthSession) -> Path:
        if not account.session_file:
            account.session_file = f"{account.id}.json"
        path = self.session_store_for(account).save(session)
        account.updated_at = utc_now_iso()
        self.save()
        return path

    def find_or_create_oauth(self, name: str | None = None, *, use_legacy_session: bool = False) -> Account:
        with self._lock:
            if name:
                existing = self.find_by_name(name)
                if existing is not None:
                    if existing.kind != "oauth":
                        raise ValueError(f"Account {name!r} exists but is not an OAuth account")
                    return existing
            elif use_legacy_session:
                for account in self.accounts:
                    if account.kind == "oauth" and (account.session_file or "") == SESSION_FILENAME:
                        return account
                existing_default = self.find_by_name(DEFAULT_OAUTH_NAME)
                if existing_default is not None and existing_default.kind == "oauth":
                    return existing_default
            account_id = str(uuid.uuid4())
            session_file = SESSION_FILENAME if use_legacy_session else f"{account_id}.json"
            account = self._build_oauth_account(
                account_id=account_id,
                name=name or DEFAULT_OAUTH_NAME,
                session_file=session_file,
            )
            self.accounts.append(account)
            self._save_unlocked()
            logger.info("Created OAuth pool account id=%s name=%s", account.id, account.name)
            return account

    def add_account(
        self,
        *,
        name: str,
        kind: str,
        enabled: bool = True,
        note: str = "",
        ak: str | None = None,
        sk: str | None = None,
        security_token: str | None = None,
        project_id: str | None = None,
    ) -> Account:
        kind = kind.strip().lower()
        if kind not in ACCOUNT_KINDS:
            raise ValueError("kind must be aksk or oauth")
        name = name.strip()
        if not name:
            raise ValueError("name is required")
        if kind == "aksk" and (not ak or not sk):
            raise ValueError("AK/SK accounts require ak and sk")
        with self._lock:
            now = utc_now_iso()
            account_id = str(uuid.uuid4())
            account = Account(
                id=account_id,
                name=name,
                kind=kind,
                enabled=enabled,
                note=note or "",
                ak=ak or None,
                sk=sk or None,
                security_token=security_token or None,
                project_id=project_id or None,
                session_file=f"{account_id}.json" if kind == "oauth" else None,
                created_at=now,
                updated_at=now,
            )
            self.accounts.append(account)
            self._save_unlocked()
            logger.info("Added pool account id=%s name=%s kind=%s", account.id, account.name, account.kind)
            return account

    def update_account(self, account_id: str, data: dict[str, Any]) -> Account:
        with self._lock:
            account = self.get(account_id)
            if account is None:
                raise KeyError(account_id)
            if "name" in data and data["name"] is not None:
                name = str(data["name"]).strip()
                if not name:
                    raise ValueError("name must not be empty")
                account.name = name
            if "enabled" in data and data["enabled"] is not None:
                account.enabled = bool(data["enabled"])
            if "note" in data and data["note"] is not None:
                account.note = str(data["note"])
            if "project_id" in data:
                value = data["project_id"]
                account.project_id = None if value in (None, "") else str(value)
            # Blank secret fields mean keep the existing value.
            if "ak" in data and data["ak"]:
                account.ak = str(data["ak"])
            if "sk" in data and data["sk"]:
                account.sk = str(data["sk"])
            if "security_token" in data:
                token = data["security_token"]
                if token:
                    account.security_token = str(token)
                elif token is None:
                    account.security_token = None
            if account.kind == "aksk" and not account.has_aksk():
                raise ValueError("AK/SK accounts require ak and sk")
            account.updated_at = utc_now_iso()
            self._save_unlocked()
            logger.info("Updated pool account id=%s name=%s", account.id, account.name)
            return account

    def delete_account(self, account_id: str) -> None:
        with self._lock:
            account = self.get(account_id)
            if account is None:
                raise KeyError(account_id)
            self.accounts = [item for item in self.accounts if item.id != account_id]
            self._cooldowns.pop(account_id, None)
            self._cooldown_until_wall.pop(account_id, None)
            self._cooldown_status.pop(account_id, None)
            self._save_unlocked()
            logger.info("Deleted pool account id=%s name=%s", account_id, account.name)

    def record_checkin(self, account_id: str, result: dict[str, Any], checked_in_at: str) -> None:
        with self._lock:
            account = self.get(account_id)
            if account is None:
                return
            account.last_checkin = result
            account.last_checkin_at = checked_in_at
            if result.get("status") == "error":
                account.last_error = str(result.get("message") or "error")
            else:
                account.last_error = None
            account.updated_at = utc_now_iso()
            self._save_unlocked()

    def record_benefit(self, account_id: str, result: dict[str, Any], claimed_at: str) -> None:
        with self._lock:
            account = self.get(account_id)
            if account is None:
                return
            account.last_benefit = result
            account.last_benefit_at = claimed_at
            account.updated_at = utc_now_iso()
            self._save_unlocked()

    def mark_cooldown(self, account_id: str, status_code: int, seconds: int | None = None) -> None:
        duration = self.settings.cooldown_seconds if seconds is None else seconds
        if duration <= 0:
            return
        deadline = time.monotonic() + duration
        until = (datetime.now(timezone.utc) + timedelta(seconds=duration)).isoformat()
        with self._lock:
            self._cooldowns[account_id] = deadline
            self._cooldown_until_wall[account_id] = until
            self._cooldown_status[account_id] = status_code
        account = self.get(account_id)
        name = account.name if account else account_id
        logger.info(
            "Account id=%s name=%s entered cooldown for %ss after HTTP %s",
            account_id,
            name,
            duration,
            status_code,
        )

    def clear_cooldown(self, account_id: str) -> None:
        with self._lock:
            self._cooldowns.pop(account_id, None)
            self._cooldown_until_wall.pop(account_id, None)
            self._cooldown_status.pop(account_id, None)

    def in_cooldown(self, account_id: str) -> bool:
        deadline = self._cooldowns.get(account_id)
        if deadline is None:
            return False
        if time.monotonic() >= deadline:
            self.clear_cooldown(account_id)
            return False
        return True

    def cooldown_info(self, account_id: str) -> dict[str, Any]:
        active = self.in_cooldown(account_id)
        return {
            "in_cooldown": active,
            "cooldown_until": self._cooldown_until_wall.get(account_id) if active else None,
            "cooldown_http_status": self._cooldown_status.get(account_id) if active else None,
        }

    def iter_candidates(self) -> list[Account]:
        """Enabled accounts that can be tried, ordered by the configured strategy.

        Healthy (not in cooldown) accounts are preferred. If every enabled account
        is in cooldown, the enabled list is still returned so a single-account
        setup is not hard-blocked until the cooldown expires.
        """
        with self._lock:
            kinds = allowed_kinds(self.settings.credential_source)
            enabled = [
                account for account in self.accounts
                if account.enabled and (kinds is None or account.kind in kinds)
            ]
            healthy = [account for account in enabled if not self.in_cooldown(account.id)]
            chosen = healthy or enabled
            return self._order(chosen)

    def _order(self, accounts: list[Account]) -> list[Account]:
        if not accounts:
            return []
        strategy = self.settings.pool_strategy
        if strategy == "first_available":
            return list(accounts)
        if strategy == "random":
            shuffled = list(accounts)
            random.shuffle(shuffled)
            return shuffled
        n = len(accounts)
        start = self._rr_index % n
        self._rr_index = (start + 1) % n
        return accounts[start:] + accounts[:start]

    def update_runtime_settings(self, updates: dict[str, Any]) -> dict[str, Any]:
        cleaned = validate_settings_update(updates)
        with self._lock:
            for key, value in cleaned.items():
                if key == "api_key":
                    if value:
                        self.persisted_settings["api_key"] = value
                        self.api_key_persisted = True
                    continue
                if value is None and key == "opengw_base_url":
                    self.persisted_settings[key] = None
                    continue
                if value is None and key == "x_language":
                    self.persisted_settings[key] = None
                    continue
                self.persisted_settings[key] = value
            apply_settings_overlay(self.settings, self.persisted_settings)
            self._save_unlocked()
        return self.public_config()

    def public_config(self) -> dict[str, Any]:
        payload = self.settings.public_config()
        payload["api_key_source"] = "ui" if self.api_key_persisted else "env"
        return payload

    def public_account(self, account: Account, session: OAuthSession | None = None) -> dict[str, Any]:
        info = self.cooldown_info(account.id)
        oauth_view = None
        if account.kind == "oauth":
            loaded = session
            if loaded is None:
                loaded = self.session_store_for(account).load()
            oauth_view = loaded.public_view() if loaded else {
                "source": "oauth",
                "has_access_key": False,
                "has_refresh_token": False,
                "expires_at": None,
                "expired": True,
                "can_refresh": False,
            }
        return {
            "id": account.id,
            "name": account.name,
            "kind": account.kind,
            "enabled": account.enabled,
            "note": account.note,
            "ak_masked": mask_secret(account.ak),
            "has_ak": bool(account.ak),
            "has_sk": bool(account.sk),
            "has_security_token": bool(account.security_token),
            "project_id": account.project_id,
            "session_file": account.session_file if account.kind == "oauth" else None,
            "oauth": oauth_view,
            "last_checkin": account.last_checkin,
            "last_checkin_at": account.last_checkin_at,
            "last_error": account.last_error,
            "last_benefit": account.last_benefit,
            "last_benefit_at": account.last_benefit_at,
            "created_at": account.created_at,
            "updated_at": account.updated_at,
            **info,
        }

    def public_accounts(self) -> list[dict[str, Any]]:
        return [self.public_account(account) for account in self.accounts]

    def status_summary(self) -> dict[str, Any]:
        enabled = self.enabled_accounts()
        return {
            "count": len(self.accounts),
            "enabled": len(enabled),
            "strategy": self.settings.pool_strategy,
            "cooldown_seconds": self.settings.cooldown_seconds,
            "accounts": self.public_accounts(),
        }

    def all_secrets(self) -> list[str]:
        values: list[str] = []
        for account in self.accounts:
            for item in (account.ak, account.sk, account.security_token):
                if item:
                    values.append(item)
            if account.kind != "oauth":
                continue
            session = self.session_store_for(account).load()
            if session is None:
                continue
            for item in (
                session.access_key_id,
                session.secret_access_key,
                session.security_token,
                session.refresh_token,
            ):
                if item:
                    values.append(item)
        if self.persisted_settings.get("api_key"):
            values.append(str(self.persisted_settings["api_key"]))
        return values


def apply_settings_overlay(settings: Settings, overlay: dict[str, Any]) -> None:
    for key in OVERRIDABLE_FIELDS:
        if key not in overlay:
            continue
        value = overlay[key]
        if key == "api_key":
            if value:
                settings.api_key = SecretStr(str(value))
            continue
        setattr(settings, key, value)


def validate_settings_update(updates: dict[str, Any]) -> dict[str, Any]:
    forbidden = {"host", "port", "data_dir", "auth_dir", "codearts_ak", "codearts_sk", "codearts_security_token"}
    unknown = [key for key in updates if key not in OVERRIDABLE_FIELDS and key not in forbidden]
    blocked = [key for key in updates if key in forbidden]
    if blocked:
        raise ValueError(f"These settings cannot be changed from the WebUI: {', '.join(blocked)}")
    if unknown:
        raise ValueError(f"Unknown settings: {', '.join(unknown)}")

    cleaned: dict[str, Any] = {}
    for key, value in updates.items():
        if key not in OVERRIDABLE_FIELDS:
            continue
        cleaned[key] = value

    if "timezone" in cleaned and cleaned["timezone"] is not None:
        tz = str(cleaned["timezone"]).strip()
        try:
            ZoneInfo(tz)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"Invalid timezone: {tz}") from exc
        cleaned["timezone"] = tz

    if "checkin_cron" in cleaned and cleaned["checkin_cron"] is not None:
        cron = str(cleaned["checkin_cron"]).strip()
        tz_name = str(cleaned.get("timezone") or "Asia/Shanghai")
        try:
            CronTrigger.from_crontab(cron, timezone=ZoneInfo(tz_name))
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"Invalid cron expression: {cron}") from exc
        cleaned["checkin_cron"] = cron

    if "pool_strategy" in cleaned and cleaned["pool_strategy"] is not None:
        strategy = str(cleaned["pool_strategy"]).strip().lower().replace("-", "_")
        if strategy not in POOL_STRATEGIES:
            raise ValueError("pool_strategy must be round_robin, first_available, or random")
        cleaned["pool_strategy"] = strategy

    if "credential_source" in cleaned and cleaned["credential_source"] is not None:
        source = str(cleaned["credential_source"]).strip().lower()
        if source not in {"auto", "oauth", "env"}:
            raise ValueError("credential_source must be auto, oauth, or env")
        cleaned["credential_source"] = source

    if "log_level" in cleaned and cleaned["log_level"] is not None:
        level = str(cleaned["log_level"]).strip().upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("log_level must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
        cleaned["log_level"] = level

    if "cooldown_seconds" in cleaned and cleaned["cooldown_seconds"] is not None:
        seconds = int(cleaned["cooldown_seconds"])
        if seconds < 0:
            raise ValueError("cooldown_seconds must be >= 0")
        cleaned["cooldown_seconds"] = seconds

    if "model_cache_ttl" in cleaned and cleaned["model_cache_ttl"] is not None:
        ttl = int(cleaned["model_cache_ttl"])
        if ttl < 0:
            raise ValueError("model_cache_ttl must be >= 0")
        cleaned["model_cache_ttl"] = ttl

    if "http_timeout" in cleaned and cleaned["http_timeout"] is not None:
        timeout = float(cleaned["http_timeout"])
        if timeout <= 0:
            raise ValueError("http_timeout must be > 0")
        cleaned["http_timeout"] = timeout

    if "http_stream_timeout" in cleaned and cleaned["http_stream_timeout"] is not None:
        timeout = float(cleaned["http_stream_timeout"])
        if timeout <= 0:
            raise ValueError("http_stream_timeout must be > 0")
        cleaned["http_stream_timeout"] = timeout

    if "oauth_callback_port" in cleaned and cleaned["oauth_callback_port"] is not None:
        port = int(cleaned["oauth_callback_port"])
        if port < 0 or port > 65535:
            raise ValueError("oauth_callback_port must be between 0 and 65535")
        cleaned["oauth_callback_port"] = port

    for url_key in ("snap_base_url", "oauth_portal_url", "oauth_sts_url"):
        if url_key in cleaned and cleaned[url_key] is not None:
            cleaned[url_key] = str(cleaned[url_key]).strip().rstrip("/")

    if "opengw_base_url" in cleaned:
        value = cleaned["opengw_base_url"]
        if value is None or str(value).strip() == "":
            cleaned["opengw_base_url"] = None
        else:
            cleaned["opengw_base_url"] = str(value).strip().rstrip("/")

    if "x_language" in cleaned:
        value = cleaned["x_language"]
        if value is None or str(value).strip() == "":
            cleaned["x_language"] = None
        else:
            cleaned["x_language"] = str(value).strip()

    for bool_key in ("checkin_run_on_startup", "scheduler_enabled", "checkin_claim_all"):
        if bool_key in cleaned and cleaned[bool_key] is not None:
            cleaned[bool_key] = bool(cleaned[bool_key])

    if "campaign_type" in cleaned and cleaned["campaign_type"] is not None:
        cleaned["campaign_type"] = str(cleaned["campaign_type"]).strip()

    if "oauth_client_id" in cleaned and cleaned["oauth_client_id"] is not None:
        cleaned["oauth_client_id"] = str(cleaned["oauth_client_id"]).strip()

    if "oauth_locale" in cleaned and cleaned["oauth_locale"] is not None:
        cleaned["oauth_locale"] = str(cleaned["oauth_locale"]).strip()

    if "api_key" in cleaned:
        value = cleaned["api_key"]
        if value is None or str(value).strip() == "":
            cleaned.pop("api_key")
        else:
            cleaned["api_key"] = str(value)

    return cleaned


def collect_redaction_secrets(settings: Settings, pool: AccountPool | None) -> list[str]:
    values = list(settings.secret_values_for_redaction())
    if pool is not None:
        values.extend(pool.all_secrets())
    # Deduplicate while preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for item in values:
        if item and item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def allowed_kinds(credential_source: str) -> set[str] | None:
    if credential_source == "oauth":
        return {"oauth"}
    if credential_source == "env":
        return {"aksk"}
    return None
