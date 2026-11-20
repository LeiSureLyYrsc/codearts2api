"""Resolve upstream IAM/STS credentials from an OAuth session and/or env AK/SK."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from codearts2api.config import Settings
from codearts2api.oauth.flow import OAuthEndpoints, refresh_oauth_session
from codearts2api.oauth.session import OAuthSession, SessionStore

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ResolvedCredential:
    access_key_id: str
    secret_access_key: str
    security_token: str | None
    source: str
    session: OAuthSession | None = None


class CredentialResolver:
    def __init__(self, settings: Settings, store: SessionStore | None = None) -> None:
        self.settings = settings
        self.store = store or SessionStore(settings.auth_dir)
        self._lock = threading.Lock()

    def endpoints(self) -> OAuthEndpoints:
        return OAuthEndpoints(
            portal_url=self.settings.oauth_portal_url,
            sts_url=self.settings.oauth_sts_url,
            snap_manager_url=self.settings.snap_base_url.rstrip("/") + "/snap-manager",
            client_id=self.settings.oauth_client_id,
            locale=self.settings.oauth_locale,
        )

    def env_credential(self) -> ResolvedCredential | None:
        if not self.settings.has_credentials():
            return None
        token = None
        if self.settings.codearts_security_token:
            token = self.settings.codearts_security_token.get_secret_value() or None
        return ResolvedCredential(
            access_key_id=self.settings.codearts_ak.get_secret_value(),  # type: ignore[union-attr]
            secret_access_key=self.settings.codearts_sk.get_secret_value(),  # type: ignore[union-attr]
            security_token=token,
            source="env",
        )

    def resolve(self) -> ResolvedCredential:
        source = self.settings.credential_source
        session = self.store.load()
        env = self.env_credential()

        if source == "env":
            if env is None:
                raise RuntimeError("CODEARTS_AK / CODEARTS_SK are not configured")
            return env

        if session is not None:
            session = self._refresh_if_needed(session)
            if session is not None:
                return ResolvedCredential(
                    access_key_id=session.access_key_id,
                    secret_access_key=session.secret_access_key,
                    security_token=session.security_token,
                    source=session.source,
                    session=session,
                )
            if source == "oauth":
                raise RuntimeError("OAuth session expired and could not be refreshed; run: uv run codearts2api login")

        if source == "oauth":
            raise RuntimeError("No OAuth session found; run: uv run codearts2api login")
        if env is None:
            raise RuntimeError(
                "No credentials available. Run `uv run codearts2api login` or set CODEARTS_AK and CODEARTS_SK."
            )
        return env

    def has_any(self) -> bool:
        try:
            self.resolve()
            return True
        except RuntimeError:
            return False

    def status(self) -> dict[str, object]:
        session = self.store.load()
        env = self.env_credential()
        return {
            "credential_source": self.settings.credential_source,
            "env_configured": env is not None,
            "oauth_session": session.public_view() if session else None,
        }

    def _refresh_if_needed(self, session: OAuthSession) -> OAuthSession | None:
        if not session.is_expired():
            return session
        if not session.can_refresh():
            logger.warning("OAuth session expired and has no refresh_token / DPoP key")
            return None
        with self._lock:
            latest = self.store.load() or session
            if latest and not latest.is_expired():
                return latest
            try:
                refreshed = refresh_oauth_session(latest, self.endpoints())
            except Exception:  # noqa: BLE001
                logger.exception("OAuth token refresh failed")
                return None
            self.store.save(refreshed)
            return refreshed
