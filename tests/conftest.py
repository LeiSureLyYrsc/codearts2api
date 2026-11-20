"""Shared test fixtures."""

from __future__ import annotations

import pytest

from codearts2api.config import Settings


@pytest.fixture
def settings(tmp_path) -> Settings:  # type: ignore[no-untyped-def]
    return Settings(
        codearts_ak="test-ak",
        codearts_sk="test-sk",
        api_key="test-local-key",
        scheduler_enabled=False,
        checkin_run_on_startup=False,
        snap_base_url="https://snap-access.cn-north-4.myhuaweicloud.com",
        data_dir=str(tmp_path / "data"),
        auth_dir=str(tmp_path / "auths"),
    )
