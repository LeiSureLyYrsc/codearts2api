"""Multi-account check-in, benefit claim, and chat failover helpers."""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from codearts2api.checkin import CheckinResult, account_type_label, is_credit_account, run_checkin
from codearts2api.client import SnapAccessClient, UpstreamError
from codearts2api.credentials import CredentialResolver, ResolvedCredential
from codearts2api.pool import Account, AccountPool, is_failover_status, now_in_timezone

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


def aggregate_checkin_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    if not results:
        return {
            "status": "error",
            "account_type": "unknown",
            "is_credit_package": None,
            "message": "No enabled accounts to check in",
            "actions": [],
        }
    if len(results) == 1:
        return dict(results[0]["result"])
    statuses = [item["result"].get("status") for item in results]
    unique = set(statuses)
    if unique == {"claimed"}:
        status = "claimed"
        message = f"Check-in completed for {len(results)} accounts"
    elif unique == {"already_claimed"}:
        status = "already_claimed"
        message = f"Already claimed today for {len(results)} accounts"
    elif unique <= {"claimed", "already_claimed"}:
        status = "claimed"
        message = "Check-in finished for all accounts (claimed or already claimed)"
    elif "error" in unique or "confirm_failed" in unique:
        status = "error"
        message = "One or more accounts failed check-in"
    else:
        status = "mixed"
        message = "Check-in finished with mixed per-account results"
    return {
        "status": status,
        "account_type": "pool",
        "is_credit_package": None,
        "message": message,
        "actions": [],
        "account_count": len(results),
    }


async def run_checkin_for_accounts(
    *,
    client: SnapAccessClient,
    resolver: CredentialResolver,
    pool: AccountPool,
    accounts: list[Account],
    campaign_type: str,
    all_claimable: bool,
    timezone_name: str,
) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    checked_in_at = now_in_timezone(timezone_name).isoformat()
    per_account: list[dict[str, Any]] = []
    for account in accounts:
        try:
            cred = resolver.resolve_account(account)
            result = await run_checkin(
                client.bind(cred),
                campaign_type=campaign_type,
                all_claimable=all_claimable,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Check-in failed for account id=%s name=%s", account.id, account.name)
            result = CheckinResult(
                status="error",
                account_type="unknown",
                is_credit_package=None,
                message=f"Check-in failed: {exc}",
                raw_error=type(exc).__name__,
            )
        payload = result.to_dict()
        pool.record_checkin(account.id, payload, checked_in_at)
        logger.info(
            "check-in finished account=%s name=%s status=%s account_type=%s",
            account.id,
            account.name,
            result.status,
            result.account_type,
        )
        per_account.append(
            {
                "account_id": account.id,
                "name": account.name,
                "result": payload,
                "checked_in_at": checked_in_at,
            }
        )
    overall = aggregate_checkin_results(per_account)
    return per_account, overall, checked_in_at


async def run_benefit_claim_for_accounts(
    *,
    client: SnapAccessClient,
    resolver: CredentialResolver,
    pool: AccountPool,
    accounts: list[Account],
    timezone_name: str,
) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    claimed_at = now_in_timezone(timezone_name).isoformat()
    per_account: list[dict[str, Any]] = []
    for account in accounts:
        try:
            cred = resolver.resolve_account(account)
            result = await client.claim_opengw_benefit(credential=cred)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Benefit claim failed for account id=%s name=%s", account.id, account.name)
            result = {
                "status": "error",
                "message": str(exc),
            }
        pool.record_benefit(account.id, result, claimed_at)
        logger.info(
            "benefit-claim finished account=%s name=%s status=%s",
            account.id,
            account.name,
            result.get("status"),
        )
        per_account.append(
            {
                "account_id": account.id,
                "name": account.name,
                "result": result,
                "claimed_at": claimed_at,
            }
        )
    if not per_account:
        overall: dict[str, Any] = {"status": "error", "message": "No enabled accounts"}
    elif len(per_account) == 1:
        overall = dict(per_account[0]["result"])
    else:
        statuses = {item["result"].get("status") for item in per_account}
        if statuses <= {"claimed"}:
            status = "claimed"
        elif "error" in statuses:
            status = "error"
        else:
            status = "mixed"
        overall = {"status": status, "account_count": len(per_account)}
    return per_account, overall, claimed_at


async def probe_account(
    client: SnapAccessClient,
    cred: ResolvedCredential,
) -> dict[str, Any]:
    """Probe an account with the official plugin statistics path."""
    try:
        payload = await client.get_plugin_statistics(credential=cred)
    except UpstreamError as exc:
        logger.info(
            "account probe failed account=%s status=%s",
            cred.account_id,
            exc.status_code,
        )
        return {
            "ok": False,
            "http_status": exc.status_code,
            "message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("account probe failed account=%s", cred.account_id)
        return {"ok": False, "message": str(exc)}
    return {
        "ok": True,
        "http_status": 200,
        "account_type": account_type_label(payload),
        "is_credit_package": is_credit_account(payload),
        "message": "Probe succeeded",
    }


async def post_chat_with_failover(
    *,
    client: SnapAccessClient,
    resolver: CredentialResolver,
    pool: AccountPool,
    payload: dict[str, Any],
) -> Any:
    last_error: UpstreamError | None = None
    tried = 0
    for account in pool.iter_candidates():
        try:
            cred = resolver.resolve_account(account)
        except RuntimeError as exc:
            last_error = UpstreamError(str(exc), 503)
            continue
        tried += 1
        try:
            raw = await client.post_chat(payload, credential=cred)
            pool.clear_cooldown(account.id)
            return raw
        except UpstreamError as exc:
            last_error = exc
            if is_failover_status(exc.status_code):
                pool.mark_cooldown(account.id, exc.status_code)
                logger.info(
                    "chat failover from account=%s name=%s status=%s",
                    account.id,
                    account.name,
                    exc.status_code,
                )
                continue
            raise
    if last_error is not None:
        raise last_error
    raise UpstreamError("No healthy accounts available in the pool", 503)


async def open_chat_stream_with_failover(
    *,
    client: SnapAccessClient,
    resolver: CredentialResolver,
    pool: AccountPool,
    payload: dict[str, Any],
):
    """Return (stream_cm, response) for the first account that accepts the stream."""
    last_error: UpstreamError | None = None
    for account in pool.iter_candidates():
        try:
            cred = resolver.resolve_account(account)
        except RuntimeError as exc:
            last_error = UpstreamError(str(exc), 503)
            continue
        stream_cm = client.stream_chat(payload, credential=cred)
        try:
            response = await stream_cm.__aenter__()
        except UpstreamError as exc:
            await _close_cm(stream_cm, exc)
            last_error = exc
            if is_failover_status(exc.status_code):
                pool.mark_cooldown(account.id, exc.status_code)
                logger.info(
                    "chat stream failover from account=%s name=%s status=%s",
                    account.id,
                    account.name,
                    exc.status_code,
                )
                continue
            raise
        except Exception as exc:  # noqa: BLE001
            await _close_cm(stream_cm, exc)
            raise
        if response.status_code >= 400:
            raw = await response.aread()
            try:
                import json

                parsed = json.loads(raw.decode("utf-8")) if raw else None
            except ValueError:
                parsed = raw.decode("utf-8", errors="replace") if raw else None
            message = str(parsed) if not isinstance(parsed, dict) else (
                parsed.get("message")
                or (parsed.get("error") or {}).get("message")
                or f"Upstream stream failed with HTTP {response.status_code}"
            )
            await _close_cm(stream_cm)
            err = UpstreamError(str(message), response.status_code, parsed)
            last_error = err
            if is_failover_status(response.status_code):
                pool.mark_cooldown(account.id, response.status_code)
                logger.info(
                    "chat stream failover from account=%s name=%s status=%s",
                    account.id,
                    account.name,
                    response.status_code,
                )
                continue
            raise err
        pool.clear_cooldown(account.id)
        return stream_cm, response
    if last_error is not None:
        raise last_error
    raise UpstreamError("No healthy accounts available in the pool", 503)


async def list_models_with_failover(
    *,
    client: SnapAccessClient,
    resolver: CredentialResolver,
    pool: AccountPool,
) -> Any:
    last_error: UpstreamError | None = None
    for account in pool.iter_candidates():
        try:
            cred = resolver.resolve_account(account)
        except RuntimeError as exc:
            last_error = UpstreamError(str(exc), 503)
            continue
        try:
            payload = await client.list_builtin_models(credential=cred)
            pool.clear_cooldown(account.id)
            return payload
        except UpstreamError as exc:
            last_error = exc
            if is_failover_status(exc.status_code):
                pool.mark_cooldown(account.id, exc.status_code)
                logger.info(
                    "models failover from account=%s name=%s status=%s",
                    account.id,
                    account.name,
                    exc.status_code,
                )
                continue
            raise
    if last_error is not None:
        raise last_error
    raise UpstreamError("No healthy accounts available in the pool", 503)


async def _close_cm(stream_cm: Any, exc: BaseException | None = None) -> None:
    try:
        if exc is None:
            await stream_cm.__aexit__(None, None, None)
        else:
            await stream_cm.__aexit__(type(exc), exc, exc.__traceback__)
    except Exception:  # noqa: BLE001
        logger.debug("Failed to close upstream stream after failover", exc_info=True)
