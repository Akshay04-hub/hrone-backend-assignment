from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from app.main import app, db

test_client = TestClient(app)


# --------------------------------------------------------------------------- #
# 1. Endpoint Name Validation (Supported vs Unsupported)
# --------------------------------------------------------------------------- #
def test_explain_unsupported_endpoint():
    resp = test_client.get("/admin/explain/non_existent_endpoint")
    assert resp.status_code == 422
    assert "unsupported" in resp.json()["detail"].lower()


@pytest.mark.parametrize(
    "endpoint",
    [
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
)
def test_supported_endpoint_names_recognized(endpoint):
    # Verify the endpoint name itself is recognized (even if params fail or mocked)
    resp = test_client.get(f"/admin/explain/{endpoint}")
    # Will be 422 because of missing required params or 200/503, but not "Unsupported endpoint"
    if resp.status_code == 422:
        assert "unsupported" not in resp.json()["detail"].lower()


# --------------------------------------------------------------------------- #
# 2. Parameter Validation for Each Endpoint
# --------------------------------------------------------------------------- #
def test_explain_attendance_list_invalid_dates():
    resp = test_client.get(
        "/admin/explain/attendance_list?date_from=2026-07-10&date_to=2026-07-05"
    )
    assert resp.status_code == 422
    assert "less than or equal" in resp.json()["detail"].lower()


def test_explain_employee_monthly_missing_params():
    # Both emp_code and month are required
    resp_no_params = test_client.get("/admin/explain/employee_monthly")
    assert resp_no_params.status_code == 422

    resp_no_month = test_client.get("/admin/explain/employee_monthly?emp_code=EMP0001")
    assert resp_no_month.status_code == 422

    resp_bad_month = test_client.get(
        "/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-13"
    )
    assert resp_bad_month.status_code == 422


def test_explain_department_summary_missing_month():
    resp = test_client.get("/admin/explain/department_summary")
    assert resp.status_code == 422

    resp_bad_month = test_client.get("/admin/explain/department_summary?month=bad_month")
    assert resp_bad_month.status_code == 422


def test_explain_late_leaderboard_missing_month_or_bad_limit():
    resp_no_month = test_client.get("/admin/explain/late_leaderboard")
    assert resp_no_month.status_code == 422

    resp_bad_limit = test_client.get(
        "/admin/explain/late_leaderboard?month=2026-07&limit=100"
    )
    assert resp_bad_limit.status_code == 422


def test_explain_department_trend_missing_params_and_invalid_ranges():
    resp_missing = test_client.get("/admin/explain/department_trend")
    assert resp_missing.status_code == 422

    resp_to_before_from = test_client.get(
        "/admin/explain/department_trend?department=Engineering&from=2026-07-10&to=2026-07-01"
    )
    assert resp_to_before_from.status_code == 422

    resp_exceeds_92_days = test_client.get(
        "/admin/explain/department_trend?department=Engineering&from=2026-01-01&to=2026-04-05"
    )
    assert resp_exceeds_92_days.status_code == 422


# --------------------------------------------------------------------------- #
# 3. Successful Explain Execution & Correct Selection of Collection/Query
# --------------------------------------------------------------------------- #
def test_explain_attendance_list_success():
    fake_explain = {
        "queryPlanner": {
            "winningPlan": {
                "stage": "IXSCAN",
                "indexName": "date_-1_emp_code_1",
            }
        },
        "executionStats": {
            "nReturned": 10,
            "totalKeysExamined": 10,
            "totalDocsExamined": 10,
        },
    }
    with patch.object(db, "command", return_value=fake_explain) as mock_cmd:
        resp = test_client.get(
            "/admin/explain/attendance_list?date_from=2026-07-01&date_to=2026-07-31"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["endpoint"] == "attendance_list"
        assert data["collection"] == "attendance_logs"
        assert "queryPlanner" in data["explain"]
        # Verify db.command arguments
        mock_cmd.assert_called_once()
        args, kwargs = mock_cmd.call_args
        assert args[0] == "explain"
        assert args[1]["find"] == "attendance_logs"
        assert kwargs["verbosity"] == "executionStats"


def test_explain_employee_monthly_success():
    fake_explain = {
        "stages": [{"$cursor": {"queryPlanner": {"winningPlan": {"stage": "IXSCAN"}}}}],
        "executionStats": {"nReturned": 1},
    }
    with patch.object(db, "command", return_value=fake_explain) as mock_cmd:
        resp = test_client.get(
            "/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["endpoint"] == "employee_monthly"
        assert data["collection"] == "attendance_logs"
        args, kwargs = mock_cmd.call_args
        assert args[1]["aggregate"] == "attendance_logs"
        assert kwargs["verbosity"] == "executionStats"


def test_explain_department_summary_success():
    fake_explain = {
        "stages": [{"$cursor": {"queryPlanner": {"winningPlan": {"stage": "IXSCAN"}}}}],
        "executionStats": {"nReturned": 3},
    }
    with patch.object(db, "command", return_value=fake_explain) as mock_cmd:
        resp = test_client.get(
            "/admin/explain/department_summary?month=2026-07&department=Engineering"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["endpoint"] == "department_summary"
        assert data["collection"] == "employees"
        args, kwargs = mock_cmd.call_args
        assert args[1]["aggregate"] == "employees"
        assert kwargs["verbosity"] == "executionStats"


def test_explain_late_leaderboard_success():
    fake_explain = {
        "stages": [{"$cursor": {"queryPlanner": {"winningPlan": {"stage": "IXSCAN"}}}}],
        "executionStats": {"nReturned": 5},
    }
    with patch.object(db, "command", return_value=fake_explain) as mock_cmd:
        resp = test_client.get(
            "/admin/explain/late_leaderboard?month=2026-07&limit=10&department=Sales"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["endpoint"] == "late_leaderboard"
        assert data["collection"] == "attendance_logs"
        args, kwargs = mock_cmd.call_args
        assert args[1]["aggregate"] == "attendance_logs"
        assert kwargs["verbosity"] == "executionStats"


def test_explain_department_trend_success():
    fake_explain = {
        "stages": [{"$cursor": {"queryPlanner": {"winningPlan": {"stage": "IXSCAN"}}}}],
        "executionStats": {"nReturned": 7},
    }
    with patch.object(db, "command", return_value=fake_explain) as mock_cmd:
        resp = test_client.get(
            "/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-07"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["endpoint"] == "department_trend"
        assert data["collection"] == "employees"
        args, kwargs = mock_cmd.call_args
        assert args[1]["aggregate"] == "employees"
        assert kwargs["verbosity"] == "executionStats"


# --------------------------------------------------------------------------- #
# 4. Error Handling When Database Fails (503) & Security Check
# --------------------------------------------------------------------------- #
def test_explain_database_unavailable_returns_503():
    with patch.object(db, "command", side_effect=Exception("Connection timed out")):
        resp = test_client.get(
            "/admin/explain/department_summary?month=2026-07"
        )
        assert resp.status_code == 503
        assert "database unavailable" in resp.json()["detail"].lower()


def test_explain_response_does_not_leak_secrets():
    fake_explain = {
        "serverInfo": {"host": "cluster0.example.com", "port": 27017},
        "queryPlanner": {"winningPlan": {"stage": "IXSCAN"}},
    }
    with patch.object(db, "command", return_value=fake_explain):
        resp = test_client.get(
            "/admin/explain/department_summary?month=2026-07"
        )
        assert resp.status_code == 200
        content_str = resp.text
        # Ensure password or connection string credentials never appear in output
        assert "password" not in content_str.lower()
        assert "mongodb+srv://" not in content_str
        assert "mongodb://" not in content_str
        assert "secret" not in content_str.lower()
