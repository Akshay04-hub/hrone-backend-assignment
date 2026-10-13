from datetime import date, timedelta
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from app.main import (
    app,
    employees_col,
    attendance_logs_col,
    build_department_trend_pipeline,
)

test_client = TestClient(app)


# --------------------------------------------------------------------------- #
# 1. Parameter Validation & Status Codes
# --------------------------------------------------------------------------- #
def test_trend_missing_query_params():
    # Missing 'from' or 'to'
    resp = test_client.get("/analytics/departments/Engineering/trend")
    assert resp.status_code == 422
    resp_from_only = test_client.get(
        "/analytics/departments/Engineering/trend?from=2026-07-01"
    )
    assert resp_from_only.status_code == 422
    resp_to_only = test_client.get(
        "/analytics/departments/Engineering/trend?to=2026-07-31"
    )
    assert resp_to_only.status_code == 422


@pytest.mark.parametrize(
    "bad_date", ["2026-7-1", "invalid", "2026-07", "2026/07/01"]
)
def test_trend_invalid_date_format(bad_date):
    resp = test_client.get(
        f"/analytics/departments/Engineering/trend?from={bad_date}&to=2026-07-31"
    )
    assert resp.status_code == 422


def test_trend_to_before_from():
    resp = test_client.get(
        "/analytics/departments/Engineering/trend?from=2026-07-10&to=2026-07-05"
    )
    assert resp.status_code == 422
    assert "earlier" in resp.json()["detail"].lower()


def test_trend_range_exceeds_92_days():
    # 2026-01-01 to 2026-04-04 is 94 days (exceeds 92 days)
    resp = test_client.get(
        "/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-04-04"
    )
    assert resp.status_code == 422
    assert "92 days" in resp.json()["detail"].lower()


def test_trend_unknown_department_returns_404():
    with patch.object(employees_col, "find_one", return_value=None):
        resp = test_client.get(
            "/analytics/departments/NonExistent/trend?from=2026-07-01&to=2026-07-07"
        )
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()


# --------------------------------------------------------------------------- #
# 2. Pipeline Structure & Stages
# --------------------------------------------------------------------------- #
def test_pipeline_structure_stages():
    pipeline = build_department_trend_pipeline(
        department="Engineering",
        from_date_str="2026-07-01",
        to_date_str="2026-07-07",
    )
    # Stage 0: $match department
    assert pipeline[0] == {"$match": {"department": "Engineering"}}

    # Stage 1: $limit 1
    assert pipeline[1] == {"$limit": 1}

    # Stage 2: $project date_dt
    assert "$dateFromString" in str(pipeline[2])

    # Stage 3: $densify
    densify_stage = pipeline[3]["$densify"]
    assert densify_stage["field"] == "date_dt"
    assert densify_stage["range"]["unit"] == "day"
    assert densify_stage["range"]["step"] == 1
    assert len(densify_stage["range"]["bounds"]) == 2

    # Stage 4: $addFields date and is_working_day
    assert "$dateToString" in str(pipeline[4])
    assert "$isoDayOfWeek" in str(pipeline[4])

    # Stage 5: $lookup headcount
    lookup_hc = pipeline[5]["$lookup"]
    assert lookup_hc["from"] == "employees"
    assert lookup_hc["as"] == "hc"

    # Stage 6: $lookup attendance_logs
    lookup_att = pipeline[6]["$lookup"]
    assert lookup_att["from"] == "attendance_logs"
    assert lookup_att["as"] == "att"

    # Stage 9: $setWindowFields with $avg over [-6, "current"]
    window_stage = pipeline[9]["$setWindowFields"]
    assert window_stage["sortBy"] == {"date_dt": 1}
    mavg = window_stage["output"]["moving_avg_7d"]
    assert mavg["$avg"] == "$attendance_rate"
    assert mavg["window"] == {"documents": [-6, "current"]}

    # Final stage: $sort by date ascending
    assert pipeline[11] == {"$sort": {"date": 1}}


# --------------------------------------------------------------------------- #
# 3. Complete Daily Rows, Gap-Filling, and Date Boundaries
# --------------------------------------------------------------------------- #
def test_trend_daily_rows_include_weekends_and_no_record_days():
    # 7 calendar days: 2026-07-01 (Wed) to 2026-07-07 (Tue)
    mock_agg = [
        {"date": "2026-07-01", "is_working_day": True, "headcount": 5, "present_count": 5.0, "late_count": 0, "attendance_rate": 1.0, "moving_avg_7d": 1.0},
        {"date": "2026-07-02", "is_working_day": True, "headcount": 5, "present_count": 4.0, "late_count": 1, "attendance_rate": 0.8, "moving_avg_7d": 0.9},
        {"date": "2026-07-03", "is_working_day": True, "headcount": 5, "present_count": 5.0, "late_count": 0, "attendance_rate": 1.0, "moving_avg_7d": 0.9333},
        {"date": "2026-07-04", "is_working_day": False, "headcount": 5, "present_count": 0.0, "late_count": 0, "attendance_rate": None, "moving_avg_7d": 0.9333},
        {"date": "2026-07-05", "is_working_day": False, "headcount": 5, "present_count": 0.0, "late_count": 0, "attendance_rate": None, "moving_avg_7d": 0.9333},
        {"date": "2026-07-06", "is_working_day": True, "headcount": 5, "present_count": 5.0, "late_count": 0, "attendance_rate": 1.0, "moving_avg_7d": 0.95},
        {"date": "2026-07-07", "is_working_day": True, "headcount": 5, "present_count": 3.0, "late_count": 0, "attendance_rate": 0.6, "moving_avg_7d": 0.88},
    ]

    with patch.object(employees_col, "find_one", return_value={"department": "Engineering"}):
        with patch.object(employees_col, "aggregate", return_value=mock_agg):
            resp = test_client.get(
                "/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-07"
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["department"] == "Engineering"
            items = data["items"]
            assert len(items) == 7
            dates = [it["date"] for it in items]
            assert dates == [
                "2026-07-01",
                "2026-07-02",
                "2026-07-03",
                "2026-07-04",
                "2026-07-05",
                "2026-07-06",
                "2026-07-07",
            ]


# --------------------------------------------------------------------------- #
# 4. Attendance Rate on Weekends and Working Days with Zero Records
# --------------------------------------------------------------------------- #
def test_attendance_rate_null_on_weekends_and_zero_on_working_days_with_no_records():
    mock_agg = [
        # Saturday: weekend -> attendance_rate is null
        {"date": "2026-07-04", "is_working_day": False, "headcount": 2, "present_count": 0.0, "late_count": 0, "attendance_rate": None, "moving_avg_7d": None},
        # Sunday: weekend -> attendance_rate is null
        {"date": "2026-07-05", "is_working_day": False, "headcount": 2, "present_count": 0.0, "late_count": 0, "attendance_rate": None, "moving_avg_7d": None},
        # Monday: working day with eligible employees but no logs -> rate 0.0, not null
        {"date": "2026-07-06", "is_working_day": True, "headcount": 2, "present_count": 0.0, "late_count": 0, "attendance_rate": 0.0, "moving_avg_7d": 0.0},
    ]

    with patch.object(employees_col, "find_one", return_value={"department": "Engineering"}):
        with patch.object(employees_col, "aggregate", return_value=mock_agg):
            resp = test_client.get(
                "/analytics/departments/Engineering/trend?from=2026-07-04&to=2026-07-06"
            )
            assert resp.status_code == 200
            items = resp.json()["items"]
            assert items[0]["is_working_day"] is False
            assert items[0]["attendance_rate"] is None
            assert items[1]["is_working_day"] is False
            assert items[1]["attendance_rate"] is None
            assert items[2]["is_working_day"] is True
            assert items[2]["attendance_rate"] == 0.0


# --------------------------------------------------------------------------- #
# 5. Headcount Zero Behavior & Changing Headcount Across Dates
# --------------------------------------------------------------------------- #
def test_headcount_zero_yields_null_attendance_rate():
    # Employees haven't joined yet (joined_on in future) -> headcount = 0 -> rate is null
    mock_agg = [
        {"date": "2026-07-01", "is_working_day": True, "headcount": 0, "present_count": 0.0, "late_count": 0, "attendance_rate": None, "moving_avg_7d": None},
    ]

    with patch.object(employees_col, "find_one", return_value={"department": "Engineering"}):
        with patch.object(employees_col, "aggregate", return_value=mock_agg):
            resp = test_client.get(
                "/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-01"
            )
            assert resp.status_code == 200
            item = resp.json()["items"][0]
            assert item["headcount"] == 0
            assert item["attendance_rate"] is None


def test_headcount_changing_on_join_date():
    # Employee joins mid-period on 2026-07-03 -> headcount increases from 1 to 2
    mock_agg = [
        {"date": "2026-07-01", "is_working_day": True, "headcount": 1, "present_count": 1.0, "late_count": 0, "attendance_rate": 1.0, "moving_avg_7d": 1.0},
        {"date": "2026-07-02", "is_working_day": True, "headcount": 1, "present_count": 1.0, "late_count": 0, "attendance_rate": 1.0, "moving_avg_7d": 1.0},
        {"date": "2026-07-03", "is_working_day": True, "headcount": 2, "present_count": 1.0, "late_count": 0, "attendance_rate": 0.5, "moving_avg_7d": 0.8333},
    ]

    with patch.object(employees_col, "find_one", return_value={"department": "Engineering"}):
        with patch.object(employees_col, "aggregate", return_value=mock_agg):
            resp = test_client.get(
                "/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-03"
            )
            assert resp.status_code == 200
            items = resp.json()["items"]
            assert items[0]["headcount"] == 1
            assert items[1]["headcount"] == 1
            assert items[2]["headcount"] == 2
            assert items[2]["attendance_rate"] == 0.5


# --------------------------------------------------------------------------- #
# 6. Seven-Day Moving Average Across First 6 Days and Beyond
# --------------------------------------------------------------------------- #
def test_moving_average_shorter_window_for_first_days_and_7d_window():
    # Day 1: [Day 1] (rate 1.0) -> mavg = 1.0
    # Day 2: [Day 1, 2] (rates 1.0, 0.5) -> mavg = 0.75
    # Day 3: [Day 1, 2, 3] (rates 1.0, 0.5, 0.5) -> mavg = 2.0 / 3 = 0.6667
    mock_agg = [
        {"date": "2026-07-01", "is_working_day": True, "headcount": 2, "present_count": 2.0, "late_count": 0, "attendance_rate": 1.0, "moving_avg_7d": 1.0},
        {"date": "2026-07-02", "is_working_day": True, "headcount": 2, "present_count": 1.0, "late_count": 0, "attendance_rate": 0.5, "moving_avg_7d": 0.75},
        {"date": "2026-07-03", "is_working_day": True, "headcount": 2, "present_count": 1.0, "late_count": 0, "attendance_rate": 0.5, "moving_avg_7d": 0.6667},
    ]

    with patch.object(employees_col, "find_one", return_value={"department": "Engineering"}):
        with patch.object(employees_col, "aggregate", return_value=mock_agg):
            resp = test_client.get(
                "/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-03"
            )
            assert resp.status_code == 200
            items = resp.json()["items"]
            assert items[0]["moving_avg_7d"] == 1.0
            assert items[1]["moving_avg_7d"] == 0.75
            assert items[2]["moving_avg_7d"] == 0.6667


# --------------------------------------------------------------------------- #
# 7. Half-Day and Presence Status Handling
# --------------------------------------------------------------------------- #
def test_half_day_counts_as_half_present():
    # 1 full day (1.0) + 1 half day (0.5) = 1.5 present_count
    # Headcount = 2 -> attendance_rate = 1.5 / 2 = 0.75
    mock_agg = [
        {"date": "2026-07-01", "is_working_day": True, "headcount": 2, "present_count": 1.5, "late_count": 0, "attendance_rate": 0.75, "moving_avg_7d": 0.75},
    ]

    with patch.object(employees_col, "find_one", return_value={"department": "Engineering"}):
        with patch.object(employees_col, "aggregate", return_value=mock_agg):
            resp = test_client.get(
                "/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-01"
            )
            assert resp.status_code == 200
            item = resp.json()["items"][0]
            assert item["present_count"] == 1.5
            assert item["attendance_rate"] == 0.75


# --------------------------------------------------------------------------- #
# 8. Exact Response Schema Adherence
# --------------------------------------------------------------------------- #
def test_response_schema_exact_keys():
    mock_agg = [
        {"date": "2026-07-01", "is_working_day": True, "headcount": 3, "present_count": 2.5, "late_count": 1, "attendance_rate": 0.8333, "moving_avg_7d": 0.8333},
    ]

    with patch.object(employees_col, "find_one", return_value={"department": "Engineering"}):
        with patch.object(employees_col, "aggregate", return_value=mock_agg):
            resp = test_client.get(
                "/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-01"
            )
            assert resp.status_code == 200
            body = resp.json()
            assert set(body.keys()) == {"department", "items"}
            assert body["department"] == "Engineering"
            item = body["items"][0]
            expected_keys = {
                "date",
                "is_working_day",
                "headcount",
                "present_count",
                "late_count",
                "attendance_rate",
                "moving_avg_7d",
            }
            assert set(item.keys()) == expected_keys
            assert isinstance(item["date"], str)
            assert isinstance(item["is_working_day"], bool)
            assert isinstance(item["headcount"], int)
            assert isinstance(item["present_count"], float)
            assert isinstance(item["late_count"], int)
            assert isinstance(item["attendance_rate"], float)
            assert isinstance(item["moving_avg_7d"], float)
