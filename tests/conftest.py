"""Shared test fixtures."""

from __future__ import annotations

import pytest

from codearts2api.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        codearts_ak="test-ak",
        codearts_sk="test-sk",
        api_key="test-local-key",
        scheduler_enabled=False,
        checkin_run_on_startup=False,
        snap_base_url="https://snap-access.cn-north-4.myhuaweicloud.com",
    )
