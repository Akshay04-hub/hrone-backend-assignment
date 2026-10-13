from decimal import Decimal, ROUND_HALF_UP
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from app.main import (
    app,
    employees_col,
    build_department_summary_pipeline,
)

test_client = TestClient(app)


# --------------------------------------------------------------------------- #
# 1. Parameter Validation & Status Codes
# --------------------------------------------------------------------------- #
def test_department_summary_missing_month():
    resp = test_client.get("/analytics/departments/summary")
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "bad_month",
    ["2026-7", "2026-13", "2026-00", "invalid", "2026", "2026-07-01"],
)
def test_department_summary_invalid_month_format(bad_month):
    resp = test_client.get(f"/analytics/departments/summary?month={bad_month}")
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# 2. Pipeline Structure & Query Construction
# --------------------------------------------------------------------------- #
def test_pipeline_structure_unfiltered():
    pipeline = build_department_summary_pipeline(
        month_start="2026-07-01",
        month_end="2026-07-31",
        department=None,
    )
    # Stage 0: $match on joined_on <= month_end
    match_stage = pipeline[0]["$match"]
    assert match_stage == {"joined_on": {"$lte": "2026-07-31"}}
    assert "department" not in match_stage

    # Stage 1: $lookup from attendance_logs
    lookup_stage = pipeline[1]["$lookup"]
    assert lookup_stage["from"] == "attendance_logs"
    assert lookup_stage["let"] == {"emp": "$emp_code"}

    # Stage 2: $unwind with preserveNullAndEmptyArrays
    unwind_stage = pipeline[2]["$unwind"]
    assert unwind_stage["path"] == "$logs"
    assert unwind_stage["preserveNullAndEmptyArrays"] is True

    # Stage 3: $group by _id: $department
    group_stage = pipeline[3]["$group"]
    assert group_stage["_id"] == "$department"
    assert group_stage["employees"] == {"$addToSet": "$emp_code"}

    # Stage 4: $sort by _id ascending
    assert pipeline[4] == {"$sort": {"_id": 1}}

    # Stage 5: $project
    project_stage = pipeline[5]["$project"]
    assert project_stage["department"] == "$_id"
    assert project_stage["headcount"] == {"$size": "$employees"}


def test_pipeline_structure_with_department_filter():
    pipeline = build_department_summary_pipeline(
        month_start="2026-07-01",
        month_end="2026-07-31",
        department="Engineering",
    )
    match_stage = pipeline[0]["$match"]
    assert match_stage["joined_on"] == {"$lte": "2026-07-31"}
    assert match_stage["department"] == "Engineering"


# --------------------------------------------------------------------------- #
# 3. Empty-Result Cases & Department Filter
# --------------------------------------------------------------------------- #
def test_department_summary_empty_when_no_eligible_employees():
    with patch.object(employees_col, "aggregate", return_value=[]):
        resp = test_client.get("/analytics/departments/summary?month=2026-07")
        assert resp.status_code == 200
        data = resp.json()
        assert data["month"] == "2026-07"
        assert data["items"] == []


def test_department_summary_filter_nonexistent_department():
    with patch.object(employees_col, "aggregate", return_value=[]):
        resp = test_client.get(
            "/analytics/departments/summary?month=2026-07&department=NonExistent"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["month"] == "2026-07"
        assert data["items"] == []


# --------------------------------------------------------------------------- #
# 4. Multiple Departments & Sorting Ascending
# --------------------------------------------------------------------------- #
def test_multiple_departments_sorted_ascending():
    # Database returns sorted items
    mock_agg = [
        {
            "department": "Engineering",
            "headcount": 10,
            "present_days": 180.0,
            "total_work_hours": 1500.0,
            "work_hours_count": 180,
            "late_count": 5,
            "total_late_minutes": 75,
            "leave_count": 2,
            "on_duty_count": 3,
        },
        {
            "department": "HR",
            "headcount": 4,
            "present_days": 70.0,
            "total_work_hours": 560.0,
            "work_hours_count": 70,
            "late_count": 1,
            "total_late_minutes": 15,
            "leave_count": 0,
            "on_duty_count": 0,
        },
        {
            "department": "Sales",
            "headcount": 8,
            "present_days": 140.0,
            "total_work_hours": 1120.0,
            "work_hours_count": 140,
            "late_count": 8,
            "total_late_minutes": 120,
            "leave_count": 4,
            "on_duty_count": 6,
        },
    ]

    with patch.object(employees_col, "aggregate", return_value=mock_agg):
        resp = test_client.get("/analytics/departments/summary?month=2026-07")
        assert resp.status_code == 200
        data = resp.json()
        assert data["month"] == "2026-07"
        departments = [item["department"] for item in data["items"]]
        assert departments == ["Engineering", "HR", "Sales"]
        assert sorted(departments) == departments


# --------------------------------------------------------------------------- #
# 5. Headcount: Zero-Log Employees Included, Late Joiners Excluded
# --------------------------------------------------------------------------- #
def test_headcount_includes_zero_log_employees_and_multiple_in_same_dept():
    # Engineering has 3 employees; 2 have zero logs, 1 has 10 logs.
    # Headcount must be 3.
    mock_agg = [
        {
            "department": "Engineering",
            "headcount": 3,
            "present_days": 10.0,
            "total_work_hours": 85.0,
            "work_hours_count": 10,
            "late_count": 1,
            "total_late_minutes": 20,
            "leave_count": 0,
            "on_duty_count": 0,
        }
    ]

    with patch.object(employees_col, "aggregate", return_value=mock_agg):
        resp = test_client.get("/analytics/departments/summary?month=2026-07&department=Engineering")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 1
        item = data["items"][0]
        assert item["department"] == "Engineering"
        assert item["headcount"] == 3
        assert item["present_days"] == 10.0


def test_department_omitted_if_headcount_zero():
    # If a department somehow reports headcount 0, it must be omitted per contract
    mock_agg = [
        {
            "department": "FutureDept",
            "headcount": 0,
            "present_days": 0.0,
            "total_work_hours": 0.0,
            "work_hours_count": 0,
            "late_count": 0,
            "total_late_minutes": 0,
            "leave_count": 0,
            "on_duty_count": 0,
        }
    ]

    with patch.object(employees_col, "aggregate", return_value=mock_agg):
        resp = test_client.get("/analytics/departments/summary?month=2026-07")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []


# --------------------------------------------------------------------------- #
# 6. Average Work Hours: Across Records, NOT Average of Employee Averages
# --------------------------------------------------------------------------- #
def test_average_work_hours_calculated_across_records():
    # Emp 1 worked 1 record: 10.0 hours (emp avg = 10.0)
    # Emp 2 worked 9 records: 6.0 hours each = 54.0 hours (emp avg = 6.0)
    # Average of averages would be (10.0 + 6.0) / 2 = 8.00 (WRONG)
    # Correct average across records is (10.0 + 54.0) / 10 = 64.0 / 10 = 6.40
    mock_agg = [
        {
            "department": "Engineering",
            "headcount": 2,
            "present_days": 10.0,
            "total_work_hours": 64.0,
            "work_hours_count": 10,
            "late_count": 0,
            "total_late_minutes": 0,
            "leave_count": 0,
            "on_duty_count": 0,
        }
    ]

    with patch.object(employees_col, "aggregate", return_value=mock_agg):
        resp = test_client.get("/analytics/departments/summary?month=2026-07")
        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert item["avg_work_hours"] == 6.40
        assert item["avg_work_hours"] != 8.00


# --------------------------------------------------------------------------- #
# 7. Departments with No Qualifying Attendance (null avg_work_hours)
# --------------------------------------------------------------------------- #
def test_department_no_qualifying_attendance():
    # Employees exist, but no punch-outs with work_hours exist (e.g. open records or absent/leave)
    mock_agg = [
        {
            "department": "Support",
            "headcount": 5,
            "present_days": 0.0,
            "total_work_hours": 0.0,
            "work_hours_count": 0,
            "late_count": 0,
            "total_late_minutes": 0,
            "leave_count": 3,
            "on_duty_count": 0,
        }
    ]

    with patch.object(employees_col, "aggregate", return_value=mock_agg):
        resp = test_client.get("/analytics/departments/summary?month=2026-07")
        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert item["department"] == "Support"
        assert item["headcount"] == 5
        assert item["present_days"] == 0.0
        assert item["avg_work_hours"] is None
        assert item["leave_count"] == 3


# --------------------------------------------------------------------------- #
# 8. Rounding Requirements (R8 Half-Up Rounding)
# --------------------------------------------------------------------------- #
def test_rounding_half_up_for_avg_work_hours():
    # 69.0 hours over 8 records = 8.625
    # Banker's rounding rounds 8.625 to 8.62.
    # Half-up rounding (R8) must round 8.625 to 8.63.
    mock_agg = [
        {
            "department": "Finance",
            "headcount": 1,
            "present_days": 8.0,
            "total_work_hours": 69.0,
            "work_hours_count": 8,
            "late_count": 0,
            "total_late_minutes": 0,
            "leave_count": 0,
            "on_duty_count": 0,
        }
    ]

    with patch.object(employees_col, "aggregate", return_value=mock_agg):
        resp = test_client.get("/analytics/departments/summary?month=2026-07")
        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert item["avg_work_hours"] == 8.63


# --------------------------------------------------------------------------- #
# 9. Exact Schema Adherence
# --------------------------------------------------------------------------- #
def test_response_schema_exact_fields():
    mock_agg = [
        {
            "department": "Engineering",
            "headcount": 5,
            "present_days": 95.5,
            "total_work_hours": 810.25,
            "work_hours_count": 95,
            "late_count": 4,
            "total_late_minutes": 68,
            "leave_count": 1,
            "on_duty_count": 2,
        }
    ]

    with patch.object(employees_col, "aggregate", return_value=mock_agg):
        resp = test_client.get("/analytics/departments/summary?month=2026-07")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {"month", "items"}
        item = body["items"][0]
        expected_keys = {
            "department",
            "headcount",
            "present_days",
            "avg_work_hours",
            "late_count",
            "total_late_minutes",
            "leave_count",
            "on_duty_count",
        }
        assert set(item.keys()) == expected_keys
        assert isinstance(item["department"], str)
        assert isinstance(item["headcount"], int)
        assert isinstance(item["present_days"], float)
        assert isinstance(item["avg_work_hours"], float)
        assert isinstance(item["late_count"], int)
        assert isinstance(item["total_late_minutes"], int)
        assert isinstance(item["leave_count"], int)
        assert isinstance(item["on_duty_count"], int)
