"""Resolve upstream IAM/STS credentials from the account pool."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from codearts2api.config import Settings
from codearts2api.oauth.flow import OAuthEndpoints, refresh_oauth_session
from codearts2api.oauth.session import OAuthSession, SessionStore
from codearts2api.pool import Account, AccountPool, allowed_kinds

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ResolvedCredential:
    access_key_id: str
    secret_access_key: str
    security_token: str | None
    source: str
    session: OAuthSession | None = None
    account_id: str | None = None
    account_name: str | None = None
    project_id: str | None = None


class CredentialResolver:
    def __init__(
        self,
        settings: Settings,
        store: SessionStore | None = None,
        pool: AccountPool | None = None,
    ) -> None:
        self.settings = settings
        self.store = store or SessionStore(settings.auth_dir)
        self.pool = pool or AccountPool(settings, self.store)
        if pool is None:
            self.pool.load_or_migrate()
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
            account_id="env",
            account_name="env",
            project_id=self.settings.codearts_project_id,
        )

    def resolve_account(self, account: Account) -> ResolvedCredential:
        kinds = allowed_kinds(self.settings.credential_source)
        if kinds is not None and account.kind not in kinds:
            raise RuntimeError(
                f"Account {account.name} ({account.id}) is {account.kind}, "
                f"but CA2A_CREDENTIAL_SOURCE={self.settings.credential_source}"
            )
        if account.kind == "aksk":
            if not account.has_aksk():
                raise RuntimeError(f"AK/SK account {account.name} ({account.id}) is missing keys")
            return ResolvedCredential(
                access_key_id=account.ak or "",
                secret_access_key=account.sk or "",
                security_token=account.security_token,
                source=f"aksk:{account.id}",
                account_id=account.id,
                account_name=account.name,
                project_id=account.project_id,
            )

        session_store = self.pool.session_store_for(account)
        session = session_store.load()
        if session is None:
            raise RuntimeError(
                f"OAuth account {account.name} ({account.id}) has no session; "
                "run `uv run codearts2api login --account NAME` or complete login in the WebUI"
            )
        session = self._refresh_if_needed(session, session_store)
        if session is None:
            raise RuntimeError(
                f"OAuth session for {account.name} ({account.id}) expired and could not be refreshed; "
                "run `uv run codearts2api login --account NAME`"
            )
        return ResolvedCredential(
            access_key_id=session.access_key_id,
            secret_access_key=session.secret_access_key,
            security_token=session.security_token,
            source=session.source,
            session=session,
            account_id=account.id,
            account_name=account.name,
            project_id=account.project_id,
        )

    def resolve(self) -> ResolvedCredential:
        """Pick one usable account from the pool (first candidate)."""
        last_error: RuntimeError | None = None
        for account in self.pool.iter_candidates():
            try:
                return self.resolve_account(account)
            except RuntimeError as exc:
                last_error = exc
                continue
        if last_error is not None:
            raise last_error
        source = self.settings.credential_source
        if source == "env":
            env = self.env_credential()
            if env is not None:
                return env
            raise RuntimeError("CODEARTS_AK / CODEARTS_SK are not configured")
        if source == "oauth":
            raise RuntimeError("No usable OAuth account in the pool; run: uv run codearts2api login")
        raise RuntimeError(
            "No credentials available. Add an account in the WebUI, "
            "run `uv run codearts2api login`, or set CODEARTS_AK and CODEARTS_SK."
        )

    def has_any(self) -> bool:
        for account in self.pool.enabled_accounts():
            try:
                self.resolve_account(account)
                return True
            except RuntimeError:
                continue
        return False

    def status(self) -> dict[str, object]:
        session = self.store.load()
        env = self.env_credential()
        return {
            "credential_source": self.settings.credential_source,
            "env_configured": env is not None,
            "oauth_session": session.public_view() if session else None,
            "pool": {
                "count": len(self.pool.accounts),
                "enabled": len(self.pool.enabled_accounts()),
                "strategy": self.settings.pool_strategy,
                "accounts": self.pool.public_accounts(),
            },
        }

    def _refresh_if_needed(self, session: OAuthSession, store: SessionStore) -> OAuthSession | None:
        if not session.is_expired():
            return session
        if not session.can_refresh():
            logger.warning("OAuth session expired and has no refresh_token / DPoP key")
            return None
        with self._lock:
            latest = store.load() or session
            if latest and not latest.is_expired():
                return latest
            try:
                refreshed = refresh_oauth_session(latest, self.endpoints())
            except Exception:  # noqa: BLE001
                logger.exception("OAuth token refresh failed")
                return None
            store.save(refreshed)
            return refreshed
