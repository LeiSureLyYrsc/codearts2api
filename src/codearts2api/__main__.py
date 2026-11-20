"""CLI entry: ``uv run codearts2api`` / ``uv run codearts2api login``."""

from __future__ import annotations

import argparse
import json
import sys

import uvicorn

from codearts2api.config import Settings
from codearts2api.credentials import CredentialResolver
from codearts2api.logging_setup import configure_logging
from codearts2api.oauth.flow import LoginError
from codearts2api.oauth.session import SessionStore
from codearts2api.pool import AccountPool, collect_redaction_secrets


def _settings() -> Settings:
    settings = Settings()
    pool = AccountPool(settings)
    pool.load_or_migrate()
    configure_logging(settings.log_level, collect_redaction_secrets(settings, pool))
    return settings


def _pool(settings: Settings) -> AccountPool:
    pool = AccountPool(settings)
    pool.load_or_migrate()
    return pool


def cmd_serve(args: argparse.Namespace) -> int:
    settings = _settings()
    uvicorn.run(
        "codearts2api.app:app",
        host=args.host or settings.host,
        port=args.port or settings.port,
        reload=args.reload,
        log_config=None,
    )
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    settings = _settings()
    resolver = CredentialResolver(settings)
    pool = resolver.pool
    print_only = bool(args.print_only)
    account_name = getattr(args, "account", None)
    try:
        from codearts2api.oauth.dpop import generate_p256_keypair
        from codearts2api.oauth.flow import CALLBACK_TIMEOUT_SECONDS, CallbackServer, build_authorize_url
        from codearts2api.oauth.pkce import generate_pkce_pair, generate_ticket_id

        endpoints = resolver.endpoints()
        pkce = generate_pkce_pair()
        ticket_id = generate_ticket_id()
        dpop = generate_p256_keypair()
        server = CallbackServer(endpoints, port=settings.oauth_callback_port)
        server.start(pkce, ticket_id, dpop)
        authorize_url = build_authorize_url(
            endpoints,
            port=server.port,
            code_challenge=pkce.code_challenge,
            ticket_id=ticket_id,
            code_challenge_method=pkce.code_challenge_method,
        )
        print("Authorize URL:")
        print(authorize_url)
        print()
        print(f"Local callback: {server.callback_url}")
        if print_only:
            print(
                "Print-only mode: open the URL in a browser (on this machine, or via an SSH "
                "local port forward to 127.0.0.1). The official client always redirects to "
                "127.0.0.1; a remote browser cannot reach this process without a tunnel."
            )
        else:
            import webbrowser

            opened = webbrowser.open(authorize_url, new=1)
            if not opened:
                print("Could not open a local browser; open the URL above yourself.")
        try:
            session = server.wait(timeout=CALLBACK_TIMEOUT_SECONDS)
            if account_name:
                account = pool.find_or_create_oauth(account_name)
            else:
                account = pool.find_or_create_oauth(use_legacy_session=True)
            path = pool.attach_session(account, session)
            view = session.public_view()
            print("Login succeeded.")
            print(f"  account: {account.name} ({account.id})")
            print(f"  expires_at: {view.get('expires_at')}")
            print(f"  has_refresh_token: {view.get('has_refresh_token')}")
            print(f"  saved to: {path}")
            return 0
        finally:
            server.stop()
    except LoginError as exc:
        print(f"Login failed: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Login failed: {exc}", file=sys.stderr)
        return 1


def cmd_logout(args: argparse.Namespace) -> int:
    settings = _settings()
    pool = _pool(settings)
    account_name = getattr(args, "account", None)
    if account_name:
        account = pool.find_by_name(account_name) or pool.get(account_name)
        if account is None or account.kind != "oauth":
            print(f"OAuth account not found: {account_name}", file=sys.stderr)
            return 1
        store = pool.session_store_for(account)
        store.clear()
        print(f"Removed OAuth session for account {account.name} ({account.id}) at {store.path}")
        return 0
    store = SessionStore(settings.auth_dir)
    store.clear()
    print(f"Removed OAuth session under {store.directory}")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    settings = _settings()
    resolver = CredentialResolver(settings)
    status = resolver.status()
    print(f"credential_source: {status['credential_source']}")
    print(f"env_configured: {status['env_configured']}")
    print(f"oauth_session: {status['oauth_session']}")
    pool = status.get("pool") or {}
    print(f"pool_count: {pool.get('count')}")
    print(f"pool_enabled: {pool.get('enabled')}")
    print(f"pool_strategy: {pool.get('strategy')}")
    accounts = pool.get("accounts") or []
    if not accounts:
        print("accounts: (none)")
        return 0
    print("accounts:")
    for account in accounts:
        # public_accounts already masks secrets; still never print raw keys.
        line = {
            "id": account.get("id"),
            "name": account.get("name"),
            "kind": account.get("kind"),
            "enabled": account.get("enabled"),
            "ak_masked": account.get("ak_masked"),
            "oauth_expires_at": (account.get("oauth") or {}).get("expires_at"),
            "in_cooldown": account.get("in_cooldown"),
            "last_checkin_at": account.get("last_checkin_at"),
            "last_checkin_status": (account.get("last_checkin") or {}).get("status"),
        }
        print(f"  {json.dumps(line, ensure_ascii=False)}")
    return 0


def main() -> None:
    settings = Settings()
    parser = argparse.ArgumentParser(description="codearts2api — CodeArts Agent OpenAI-compatible gateway")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="Start the local OpenAI-compatible gateway")
    serve.add_argument("--host", default=None, help="Bind host (default 127.0.0.1)")
    serve.add_argument("--port", type=int, default=None, help="Bind port (default 8787)")
    serve.add_argument("--reload", action="store_true", help="Enable uvicorn reload (development only)")
    serve.set_defaults(func=cmd_serve)

    login = sub.add_parser("login", help="Log in with a Huawei Cloud account (official CLI browser SSO)")
    login.add_argument(
        "--print-only",
        action="store_true",
        help="Print the authorize URL and wait for the local callback; do not open a browser",
    )
    login.add_argument(
        "--account",
        default=None,
        help="Attach the resulting session to this pool account name (created if missing)",
    )
    login.set_defaults(func=cmd_login)

    logout = sub.add_parser("logout", help="Delete the locally stored OAuth session")
    logout.add_argument(
        "--account",
        default=None,
        help="Clear the OAuth session file for this pool account name or id",
    )
    logout.set_defaults(func=cmd_logout)

    status = sub.add_parser("status", help="Show the account pool and whether env / OAuth credentials are available")
    status.set_defaults(func=cmd_status)

    # Backward compatible: flags without a subcommand still start the server.
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--reload", action="store_true")

    args = parser.parse_args()
    if args.command is None:
        args.host = args.host or settings.host
        args.port = args.port or settings.port
        raise SystemExit(cmd_serve(args))
    if args.command == "serve":
        if args.host is None:
            args.host = settings.host
        if args.port is None:
            args.port = settings.port
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
