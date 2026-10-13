from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from app.main import app, employees_col, attendance_logs_col

test_client = TestClient(app)


def test_monthly_unknown_employee():
    with patch.object(employees_col, "find_one", return_value=None):
        resp = test_client.get("/analytics/employees/EMP9999/monthly?month=2026-07")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()


def test_monthly_invalid_month_format():
    # Invalid months must return 422
    for bad_month in ["2026-7", "2026-13", "2026-00", "invalid", "2026"]:
        resp = test_client.get(f"/analytics/employees/EMP0001/monthly?month={bad_month}")
        assert resp.status_code == 422


def test_monthly_no_attendance_records():
    # Employee joined in Jan 2026, query July 2026 (23 working days)
    fake_emp = {"emp_code": "EMP0001", "joined_on": "2026-01-05"}
    with patch.object(employees_col, "find_one", return_value=fake_emp):
        with patch.object(attendance_logs_col, "aggregate", return_value=[]):
            resp = test_client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
            assert resp.status_code == 200
            data = resp.json()
            assert data["emp_code"] == "EMP0001"
            assert data["month"] == "2026-07"
            assert data["working_days"] == 23
            assert data["present_days"] == 0.0
            assert data["leave_days"] == 0
            assert data["late_count"] == 0
            assert data["total_late_minutes"] == 0
            assert data["total_overtime_minutes"] == 0
            assert data["attendance_pct"] == 0.0


def test_monthly_mid_month_joiner():
    # Employee joined on 2026-07-13
    # Weekdays July 13 to July 31 = 15 working days
    fake_emp = {"emp_code": "EMP0002", "joined_on": "2026-07-13"}
    # 15 present days
    mock_agg = [
        {
            "present_days": 15.0,
            "leave_days": 0,
            "late_count": 1,
            "total_late_minutes": 10,
            "total_overtime_minutes": 40,
        }
    ]
    with patch.object(employees_col, "find_one", return_value=fake_emp):
        with patch.object(attendance_logs_col, "aggregate", return_value=mock_agg):
            resp = test_client.get("/analytics/employees/EMP0002/monthly?month=2026-07")
            assert resp.status_code == 200
            data = resp.json()
            assert data["working_days"] == 15
            assert data["present_days"] == 15.0
            assert data["attendance_pct"] == 100.0


def test_monthly_joined_after_requested_month():
    # Employee joined on 2026-08-01, query July 2026 -> 0 working days, attendance_pct is null
    fake_emp = {"emp_code": "EMP0003", "joined_on": "2026-08-01"}
    with patch.object(employees_col, "find_one", return_value=fake_emp):
        with patch.object(attendance_logs_col, "aggregate", return_value=[]):
            resp = test_client.get("/analytics/employees/EMP0003/monthly?month=2026-07")
            assert resp.status_code == 200
            data = resp.json()
            assert data["working_days"] == 0
            assert data["attendance_pct"] is None


def test_monthly_attendance_with_half_days_and_rounding():
    fake_emp = {"emp_code": "EMP0001", "joined_on": "2026-01-05"}
    # Working days = 23
    # 19.5 present days (e.g. 19 full days + 1 half day)
    # attendance_pct = 19.5 / 23 * 100 = 84.7826... -> 84.78
    mock_agg = [
        {
            "present_days": 19.5,
            "leave_days": 2,
            "late_count": 3,
            "total_late_minutes": 55,
            "total_overtime_minutes": 80,
        }
    ]
    with patch.object(employees_col, "find_one", return_value=fake_emp):
        with patch.object(attendance_logs_col, "aggregate", return_value=mock_agg):
            resp = test_client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
            assert resp.status_code == 200
            data = resp.json()
            assert data["working_days"] == 23
            assert data["present_days"] == 19.5
            assert data["leave_days"] == 2
            assert data["late_count"] == 3
            assert data["total_late_minutes"] == 55
            assert data["total_overtime_minutes"] == 80
            assert data["attendance_pct"] == 84.78


def test_monthly_pipeline_structure():
    # Verify the aggregation pipeline passed to MongoDB
    fake_emp = {"emp_code": "EMP0001", "joined_on": "2026-01-05"}
    with patch.object(employees_col, "find_one", return_value=fake_emp):
        with patch.object(attendance_logs_col, "aggregate", return_value=[]) as mock_agg:
            resp = test_client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
            assert resp.status_code == 200
            mock_agg.assert_called_once()
            pipeline = mock_agg.call_args[0][0]
            # Match stage checks emp_code and date range
            assert pipeline[0]["$match"]["emp_code"] == "EMP0001"
            assert pipeline[0]["$match"]["date"] == {"$gte": "2026-07-01", "$lte": "2026-07-31"}
            # Group stage contains present_days, leave_days, late_count, total_late_minutes, total_overtime_minutes
            group_fields = pipeline[1]["$group"]
            assert "present_days" in group_fields
            assert "leave_days" in group_fields
            assert "late_count" in group_fields
            assert "total_late_minutes" in group_fields
            assert "total_overtime_minutes" in group_fields
