"""Gateway auth and health endpoints."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from codearts2api.app import create_app
from codearts2api.config import Settings


@pytest.fixture
def client(settings: Settings) -> TestClient:
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def test_healthz_is_open(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_models_requires_bearer(client: TestClient) -> None:
    response = client.get("/v1/models")
    assert response.status_code == 401
    response = client.get("/v1/models", headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401
    # Valid local key still hits upstream; without a live Huawei session that is not asserted here.


def test_admin_status_with_key(client: TestClient) -> None:
    response = client.get("/admin/status", headers={"Authorization": "Bearer test-local-key"})
    assert response.status_code == 200
    body = response.json()
    assert body["scheduler"]["enabled"] is False
    assert body["last_checkin"] is None
