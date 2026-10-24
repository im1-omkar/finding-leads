from copy import deepcopy

from fastapi.testclient import TestClient

from lead_crm_loader import config as config_module
from lead_crm_loader.api import create_app
from lead_crm_loader.config import Settings


def test_research_setting_defaults_on_locally_and_off_in_production(monkeypatch) -> None:
    monkeypatch.setattr(config_module, "load_dotenv", lambda: None)
    monkeypatch.delenv("RESEARCH_ENABLED", raising=False)

    monkeypatch.setenv("APP_ENV", "development")
    assert Settings.from_env().research_enabled is True

    monkeypatch.setenv("APP_ENV", "production")
    assert Settings.from_env().research_enabled is False


def test_disabled_research_api_does_not_enqueue_runs_or_approvals(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("APP_SESSION_SECRET", "test-session-secret-that-is-long-enough")
    app = create_app(Settings(database_url="memory://", mock_mode=True, research_enabled=False))

    with TestClient(app) as client:
        registration = client.post(
            "/auth/register",
            json={"email": "demo@example.com", "password": "valid-password-123"},
        )
        assert registration.status_code == 201
        csrf_token = client.cookies.get("lead_csrf")
        assert csrf_token
        headers = {"X-CSRF-Token": csrf_token}

        config = client.get("/config")
        assert config.status_code == 200
        assert config.json()["research_enabled"] is False
        assert config.headers["cache-control"] == "no-store"
        assert client.get("/runs").headers["cache-control"] == "no-store"

        run_response = client.post(
            "/runs",
            headers=headers,
            json={
                "request_id": "disabled-run",
                "industry": "AI software",
                "region": "Bengaluru",
                "qualification_criteria": ["B2B software company"],
            },
        )
        assert run_response.status_code == 503
        assert run_response.headers["cache-control"] == "no-store"
        run_store = app.state.run_store
        assert run_store.runs == {}

        run_store.runs["saved-run"] = {
            "run_id": "saved-run",
            "request_id": "saved-request",
            "status": "awaiting_approval",
            "request": {},
            "approval": {"proposal_hash": "saved-proposal", "changes": []},
            "report": None,
            "error": None,
            "created_at": "2026-10-24T00:00:00+00:00",
            "owner_id": registration.json()["user"]["user_id"],
        }
        saved_run_before = deepcopy(run_store.runs["saved-run"])

        approval_response = client.post(
            "/runs/saved-run/approval",
            headers=headers,
            json={"approved": False, "proposal_hash": "saved-proposal"},
        )
        assert approval_response.status_code == 503
        assert approval_response.headers["cache-control"] == "no-store"
        assert run_store.runs["saved-run"] == saved_run_before
