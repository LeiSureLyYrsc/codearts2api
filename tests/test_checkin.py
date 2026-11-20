"""Check-in state machine: ineligible / already claimed / claim+confirm / claim without confirm."""

from __future__ import annotations

from typing import Any

import pytest

from codearts2api.checkin import (
    account_type_label,
    extract_campaigns,
    interpret_claim_response,
    is_already_claimed,
    is_claimable,
    is_credit_account,
    run_checkin,
    select_campaigns,
)


class FakeTransport:
    def __init__(
        self,
        plugin: Any,
        activities: Any,
        claim_result: tuple[Any, int] | Exception = ({"id": None}, 200),
        confirm_result: tuple[Any, int] | Exception = ({}, 200),
    ) -> None:
        self.plugin = plugin
        self.activities = activities
        self.claim_result = claim_result
        self.confirm_result = confirm_result
        self.claim_calls: list[Any] = []
        self.confirm_calls: list[Any] = []

    async def get_plugin_statistics(self) -> Any:
        return self.plugin

    async def list_activities(self, channel: str = "IDE") -> Any:
        assert channel == "IDE"
        return self.activities

    async def claim_campaign(self, campaign_id: Any, channel: str = "IDE") -> tuple[Any, int]:
        self.claim_calls.append((campaign_id, channel))
        if isinstance(self.claim_result, Exception):
            raise self.claim_result
        return self.claim_result

    async def confirm_campaign(self, campaign_id: Any) -> tuple[Any, int]:
        self.confirm_calls.append(campaign_id)
        if isinstance(self.confirm_result, Exception):
            raise self.confirm_result
        return self.confirm_result


CREDIT = {"package": {"is_credit_package": True, "plan": "standard"}}
TOKEN = {"package": {"is_credit_package": False}}
USER_LOGIN = {
    "campaignId": "camp-login",
    "type": "USER_LOGIN",
    "name": "每日签到得积分",
    "claimable": True,
    "status": "CLAIMABLE",
}


def test_credit_account_detection() -> None:
    assert is_credit_account(CREDIT) is True
    assert account_type_label(CREDIT) == "credit"
    assert is_credit_account(TOKEN) is False
    assert account_type_label(TOKEN) == "token"
    assert is_credit_account({"data": {"package": {"is_credit_package": True}}}) is True
    assert is_credit_account({}) is False
    assert is_credit_account({"package": {"is_credit_package": "true"}}) is False


def test_select_campaigns_defaults_to_user_login() -> None:
    campaigns = extract_campaigns(
        {
            "data": [
                USER_LOGIN,
                {"campaignId": "other", "type": "OTHER", "claimable": True},
            ]
        }
    )
    selected = select_campaigns(campaigns, campaign_type="USER_LOGIN", all_claimable=False)
    assert [c["campaignId"] for c in selected] == ["camp-login"]

    all_claimable = select_campaigns(campaigns, campaign_type="USER_LOGIN", all_claimable=True)
    assert {c["campaignId"] for c in all_claimable} == {"camp-login", "other"}


def test_claimable_and_already_claimed_flags() -> None:
    assert is_already_claimed({"claimed": True}) is True
    assert is_already_claimed({"status": "CLAIMED", "claimable": False}) is True
    assert is_claimable({"claimable": True}) is True
    assert is_claimable({"status": "CLAIMED"}) is False
    assert is_claimable({"claimable": False, "status": "EXPIRED"}) is False
    assert is_already_claimed({"claimable": True, "status": "CLAIMABLE"}) is False


@pytest.mark.asyncio
async def test_ineligible_token_account_skips_claim() -> None:
    transport = FakeTransport(plugin=TOKEN, activities={"data": [USER_LOGIN]})
    result = await run_checkin(transport)
    assert result.status == "ineligible"
    assert result.is_credit_package is False
    assert result.account_type == "token"
    assert transport.claim_calls == []
    assert transport.confirm_calls == []


@pytest.mark.asyncio
async def test_already_claimed_from_campaign_status() -> None:
    campaign = {**USER_LOGIN, "claimable": False, "status": "CLAIMED"}
    transport = FakeTransport(plugin=CREDIT, activities={"data": [campaign]})
    result = await run_checkin(transport)
    assert result.status == "already_claimed"
    assert result.actions[0].status == "already_claimed"
    assert transport.claim_calls == []


@pytest.mark.asyncio
async def test_already_claimed_business_code_40001() -> None:
    transport = FakeTransport(
        plugin=CREDIT,
        activities={"data": [USER_LOGIN]},
        claim_result=({"code": "40001", "message": "already claimed"}, 200),
    )
    result = await run_checkin(transport)
    assert result.status == "already_claimed"
    assert result.actions[0].business_code == "40001"
    assert transport.confirm_calls == []


@pytest.mark.asyncio
async def test_activity_missing_40401() -> None:
    transport = FakeTransport(
        plugin=CREDIT,
        activities={"data": [USER_LOGIN]},
        claim_result=({"error_code": "40401"}, 404),
    )
    result = await run_checkin(transport)
    assert result.status == "activity_missing"
    assert result.actions[0].status == "activity_missing"
    assert transport.confirm_calls == []


@pytest.mark.asyncio
async def test_claim_and_confirm_when_id_is_not_null() -> None:
    transport = FakeTransport(
        plugin=CREDIT,
        activities={"data": [USER_LOGIN]},
        claim_result=({"id": "record-1", "campaignId": "camp-login"}, 200),
        confirm_result=({"ok": True}, 200),
    )
    result = await run_checkin(transport)
    assert result.status == "claimed"
    assert transport.claim_calls == [("camp-login", "IDE")]
    assert transport.confirm_calls == ["camp-login"]
    assert result.actions[0].needs_confirm is True
    assert result.actions[0].confirmed is True


@pytest.mark.asyncio
async def test_claim_without_confirm_when_id_is_null() -> None:
    transport = FakeTransport(
        plugin=CREDIT,
        activities={"data": [USER_LOGIN]},
        claim_result=({"id": None}, 200),
        confirm_result=({"ok": True}, 200),
    )
    result = await run_checkin(transport)
    assert result.status == "claimed"
    assert transport.claim_calls == [("camp-login", "IDE")]
    assert transport.confirm_calls == []
    assert result.actions[0].needs_confirm is False
    assert result.actions[0].confirmed is False


@pytest.mark.asyncio
async def test_confirm_failure_is_reported() -> None:
    transport = FakeTransport(
        plugin=CREDIT,
        activities={"data": [USER_LOGIN]},
        claim_result=({"id": "record-1"}, 200),
        confirm_result=RuntimeError("timeout"),
    )
    result = await run_checkin(transport)
    assert result.status == "confirm_failed"
    assert "pending" in result.message.lower()
    assert result.actions[0].confirmed is False


@pytest.mark.asyncio
async def test_claim_all_claimable_processes_multiple() -> None:
    other = {"campaignId": "bonus", "type": "BONUS", "claimable": True}
    transport = FakeTransport(
        plugin=CREDIT,
        activities={"data": [USER_LOGIN, other]},
        claim_result=({"id": None}, 200),
    )
    result = await run_checkin(transport, all_claimable=True)
    assert result.status == "claimed"
    assert [cid for cid, _ch in transport.claim_calls] == ["camp-login", "bonus"]


def test_interpret_claim_id_in_wrapper() -> None:
    interpretation = interpret_claim_response({"data": {"id": "x"}}, 200)
    assert interpretation.needs_confirm is True
    assert interpretation.claim_record_id == "x"
    interpretation_null = interpret_claim_response({"data": {"id": None}}, 200)
    assert interpretation_null.needs_confirm is False
