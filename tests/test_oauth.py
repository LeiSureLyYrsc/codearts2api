"""OAuth SSO helpers verified against the official CodeArts CLI 26.11.11 package."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

from codearts2api.credentials import CredentialResolver
from codearts2api.config import Settings
from codearts2api.oauth.dpop import build_dpop_proof, generate_p256_keypair
from codearts2api.oauth.flow import (
    OAuthEndpoints,
    build_authorize_url,
    map_ticket_response,
    map_token_response,
    parse_callback_query,
    success_redirect_url,
)
from codearts2api.oauth.pkce import generate_pkce_pair, generate_ticket_id, sha256_b64url
from codearts2api.oauth.session import OAuthSession, SessionStore


def test_pkce_pair_matches_official_cli_shape() -> None:
    pair = generate_pkce_pair()
    assert len(pair.code_verifier) == 128
    int(pair.code_verifier, 16)  # hex
    assert pair.code_challenge_method == "SHA-256"
    assert pair.code_challenge == sha256_b64url(pair.code_verifier)
    assert "=" not in pair.code_challenge
    ticket = generate_ticket_id()
    assert len(ticket) == 64
    int(ticket, 16)


def test_authorize_url_matches_official_cli_query() -> None:
    endpoints = OAuthEndpoints()
    url = build_authorize_url(
        endpoints,
        port=54321,
        code_challenge="CHALLENGE",
        ticket_id="abc",
    )
    assert url.startswith("https://codearts.huaweicloud.com/portal/authorize?")
    assert "client_id=CodeArts_Tui" in url
    assert "port=54321" in url
    assert "code_challenge=CHALLENGE" in url
    assert "code_challenge_method=SHA-256" in url
    assert "ticket_id=abc" in url
    assert "locale=zh-cn" in url
    # Official CLI 26.11.11 does not send theme / uri_scheme / plugin-name on this URL.
    assert "uri_scheme=" not in url
    assert "plugin-name=" not in url


def test_success_redirect() -> None:
    url = success_redirect_url(OAuthEndpoints(), True)
    assert url == "https://codearts.huaweicloud.com/portal/login?login_succeed=true&locale=zh-cn"


def test_dpop_proof_header_and_claims() -> None:
    keys = generate_p256_keypair()
    token_url = "https://sts.cn-north-4.myhuaweicloud.com/v1/oauth2/tokens"
    proof = build_dpop_proof(keys, "POST", token_url, now=1_700_000_000)
    parts = proof.split(".")
    assert len(parts) == 3

    def decode(segment: str) -> dict:
        padded = segment + "=" * ((4 - len(segment) % 4) % 4)
        return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))

    header = decode(parts[0])
    payload = decode(parts[1])
    assert header["alg"] == "ES256"
    assert header["typ"] == "dpop+jwt"
    assert header["jwk"]["kty"] == "EC"
    assert header["jwk"]["crv"] == "P-256"
    assert "d" not in header["jwk"]
    assert payload["htm"] == "POST"
    assert payload["htu"] == token_url
    assert payload["iat"] == 1_700_000_000
    assert len(payload["jti"]) == 32
    # Raw P-256 signature is 64 bytes -> 86 unpadded base64url chars.
    assert "=" not in parts[2]
    raw = base64.urlsafe_b64decode(parts[2] + "=" * ((4 - len(parts[2]) % 4) % 4))
    assert len(raw) == 64


def test_map_token_and_ticket_shapes() -> None:
    dpop = generate_p256_keypair()
    session = map_token_response(
        {
            "refresh_token": "rt-1",
            "credentials": {
                "access_key_id": "HSTAEXAMPLE",
                "secret_access_key": "secret",
                "security_token": "tok",
                "expiration": "2026-11-20T12:00:00Z",
            },
        },
        client_id="CodeArts_Tui",
        dpop=dpop,
    )
    assert session.access_key_id == "HSTAEXAMPLE"
    assert session.refresh_token == "rt-1"
    assert session.security_token == "tok"
    assert session.dpop is not None

    ticket = map_ticket_response(
        {
            "credential": {
                "access": "AK",
                "secret": "SK",
                "securitytoken": "ST",
                "expires_at": "2026-11-20T12:00:00Z",
            }
        },
        client_id="CodeArts_Tui",
    )
    assert ticket.access_key_id == "AK"
    assert ticket.secret_access_key == "SK"
    assert ticket.security_token == "ST"
    assert ticket.refresh_token == ""
    assert ticket.source == "oauth-ticket"


def test_parse_callback_query() -> None:
    empty = parse_callback_query("")
    assert empty["code"] is None and empty["secret"] is None
    both = parse_callback_query("secret=s1&redirect=https://example.invalid/r")
    assert both["secret"] == "s1"
    assert both["redirect"] == "https://example.invalid/r"
    coded = parse_callback_query("code=authcode")
    assert coded["code"] == "authcode"


def test_session_roundtrip_and_expiry(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = SessionStore(tmp_path)
    now = datetime.now(timezone.utc)
    session = OAuthSession(
        access_key_id="ak",
        secret_access_key="sk",
        security_token="st",
        refresh_token="rt",
        expires_at=now + timedelta(hours=2),
        client_id="CodeArts_Tui",
        dpop=generate_p256_keypair(),
    )
    store.save(session)
    loaded = store.load()
    assert loaded is not None
    assert loaded.access_key_id == "ak"
    assert loaded.can_refresh() is True
    assert loaded.is_expired(now=now) is False
    assert loaded.is_expired(now=now + timedelta(hours=2), skew_seconds=600) is True
    store.clear()
    assert store.load() is None


def test_credential_precedence_oauth_over_env(tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = Settings(
        codearts_ak="env-ak",
        codearts_sk="env-sk",
        api_key="k",
        auth_dir=str(tmp_path / "auths"),
        data_dir=str(tmp_path / "data"),
        credential_source="auto",
        scheduler_enabled=False,
        checkin_run_on_startup=False,
        pool_strategy="first_available",
    )
    resolver = CredentialResolver(settings)
    # First-run seed from env AK/SK.
    resolved = resolver.resolve()
    assert resolved.access_key_id == "env-ak"
    assert resolved.account_id == "env"

    store = resolver.store
    session = OAuthSession(
        access_key_id="oauth-ak",
        secret_access_key="oauth-sk",
        security_token="oauth-st",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        source="oauth",
    )
    store.save(session)
    # Legacy session.json is imported into the pool on the next migrate pass.
    resolver.pool.load_or_migrate()
    oauth_accounts = [item for item in resolver.pool.accounts if item.kind == "oauth"]
    assert len(oauth_accounts) == 1
    oauth_resolved = resolver.resolve_account(oauth_accounts[0])
    assert oauth_resolved.access_key_id == "oauth-ak"
    assert oauth_resolved.security_token == "oauth-st"

    settings_env = Settings(
        codearts_ak="env-ak",
        codearts_sk="env-sk",
        api_key="k",
        auth_dir=str(tmp_path / "auths"),
        data_dir=str(tmp_path / "data-env"),
        credential_source="env",
        scheduler_enabled=False,
        checkin_run_on_startup=False,
    )
    only_env = CredentialResolver(settings_env).resolve()
    assert only_env.access_key_id == "env-ak"
    assert only_env.account_id == "env"
