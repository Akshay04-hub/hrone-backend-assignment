from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from app.main import (
    app,
    attendance_logs_col,
    build_late_leaderboard_pipeline,
)

test_client = TestClient(app)


# --------------------------------------------------------------------------- #
# 1. Parameter Validation & Status Codes
# --------------------------------------------------------------------------- #
def test_late_leaderboard_missing_month():
    resp = test_client.get("/analytics/leaderboard/late")
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "bad_month",
    ["2026-7", "2026-13", "2026-00", "invalid", "2026", "2026-07-01"],
)
def test_late_leaderboard_invalid_month_format(bad_month):
    resp = test_client.get(f"/analytics/leaderboard/late?month={bad_month}")
    assert resp.status_code == 422


@pytest.mark.parametrize("bad_limit", [0, -1, 51, 100, "abc"])
def test_late_leaderboard_invalid_limit(bad_limit):
    resp = test_client.get(f"/analytics/leaderboard/late?month=2026-07&limit={bad_limit}")
    assert resp.status_code == 422


def test_late_leaderboard_valid_limit_bounds():
    # Boundary limits 1 and 50 are accepted
    with patch.object(attendance_logs_col, "aggregate", return_value=[]):
        resp1 = test_client.get("/analytics/leaderboard/late?month=2026-07&limit=1")
        assert resp1.status_code == 200
        resp50 = test_client.get("/analytics/leaderboard/late?month=2026-07&limit=50")
        assert resp50.status_code == 200


# --------------------------------------------------------------------------- #
# 2. Pipeline Structure Verification
# --------------------------------------------------------------------------- #
def test_pipeline_structure_unfiltered():
    pipeline = build_late_leaderboard_pipeline(
        month_start="2026-07-01",
        month_end="2026-07-31",
        limit=10,
        department=None,
    )

    # Stage 0: $match date in reporting month and late_minutes > 0
    match_stage = pipeline[0]["$match"]
    assert match_stage["date"] == {"$gte": "2026-07-01", "$lte": "2026-07-31"}
    assert match_stage["late_minutes"] == {"$gt": 0}

    # Stage 1: $group by emp_code
    group_stage = pipeline[1]["$group"]
    assert group_stage["_id"] == "$emp_code"
    assert group_stage["total_late_minutes"] == {"$sum": "$late_minutes"}
    assert group_stage["late_count"] == {"$sum": 1}

    # Stage 2: $lookup from employees
    lookup_stage = pipeline[2]["$lookup"]
    assert lookup_stage["from"] == "employees"
    assert lookup_stage["localField"] == "_id"
    assert lookup_stage["foreignField"] == "emp_code"
    assert lookup_stage["as"] == "employee"

    # Stage 3: $unwind without preserveNullAndEmptyArrays (drops unknown employees)
    assert pipeline[3] == {"$unwind": "$employee"}

    # Stage 4: $setWindowFields with $rank
    window_stage = pipeline[4]["$setWindowFields"]
    assert window_stage["sortBy"] == {"total_late_minutes": -1}
    assert window_stage["output"]["rank"] == {"$rank": {}}

    # Stage 5: $match rank <= limit (applied AFTER ranking)
    assert pipeline[5] == {"$match": {"rank": {"$lte": 10}}}

    # Stage 6: $project output fields
    project_stage = pipeline[6]["$project"]
    assert project_stage["_id"] == 0
    assert project_stage["rank"] == 1
    assert project_stage["emp_code"] == "$_id"
    assert project_stage["name"] == "$employee.name"
    assert project_stage["department"] == "$employee.department"
    assert project_stage["total_late_minutes"] == 1
    assert project_stage["late_count"] == 1

    # Stage 7: $sort by total_late_minutes DESC, emp_code ASC
    assert pipeline[7] == {
        "$sort": {
            "total_late_minutes": -1,
            "emp_code": 1,
        }
    }


def test_pipeline_structure_with_department_filter():
    pipeline = build_late_leaderboard_pipeline(
        month_start="2026-07-01",
        month_end="2026-07-31",
        limit=5,
        department="Engineering",
    )
    # Department filter must be placed before $setWindowFields
    dept_match = pipeline[4]
    assert dept_match == {"$match": {"employee.department": "Engineering"}}

    # Next stage is window ranking within that department
    window_stage = pipeline[5]["$setWindowFields"]
    assert window_stage["sortBy"] == {"total_late_minutes": -1}
    assert window_stage["output"]["rank"] == {"$rank": {}}

    # Limit stage uses limit=5
    assert pipeline[6] == {"$match": {"rank": {"$lte": 5}}}


# --------------------------------------------------------------------------- #
# 3. Competition Ranking & Ties Handling (e.g. 1, 2, 2, 4)
# --------------------------------------------------------------------------- #
def test_competition_ranking_with_ties_1_2_2_4():
    # 4 employees:
    # EMP0001: 90 mins -> Rank 1
    # EMP0002: 60 mins -> Rank 2 (tied)
    # EMP0003: 60 mins -> Rank 2 (tied)
    # EMP0004: 30 mins -> Rank 4 (rank 3 skipped per standard competition ranking)
    mock_agg = [
        {
            "rank": 1,
            "emp_code": "EMP0001",
            "name": "Alice",
            "department": "Engineering",
            "total_late_minutes": 90,
            "late_count": 3,
        },
        {
            "rank": 2,
            "emp_code": "EMP0002",
            "name": "Bob",
            "department": "Engineering",
            "total_late_minutes": 60,
            "late_count": 2,
        },
        {
            "rank": 2,
            "emp_code": "EMP0003",
            "name": "Charlie",
            "department": "Engineering",
            "total_late_minutes": 60,
            "late_count": 2,
        },
        {
            "rank": 4,
            "emp_code": "EMP0004",
            "name": "Dave",
            "department": "Engineering",
            "total_late_minutes": 30,
            "late_count": 1,
        },
    ]

    with patch.object(attendance_logs_col, "aggregate", return_value=mock_agg):
        resp = test_client.get("/analytics/leaderboard/late?month=2026-07")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 4
        ranks = [item["rank"] for item in items]
        assert ranks == [1, 2, 2, 4]


# --------------------------------------------------------------------------- #
# 4. Limit Applied AFTER Ranking (Ties at Cutoff Return More Than Limit Rows)
# --------------------------------------------------------------------------- #
def test_limit_applied_after_ranking_includes_tied_entries_at_cutoff():
    # limit=2 requested.
    # Rank 1: EMP0001 (90 mins)
    # Rank 2: EMP0002 (60 mins) & EMP0003 (60 mins)
    # Because both tied employees have rank 2 <= limit 2,
    # the response must return 3 items!
    mock_agg = [
        {
            "rank": 1,
            "emp_code": "EMP0001",
            "name": "Alice",
            "department": "Engineering",
            "total_late_minutes": 90,
            "late_count": 3,
        },
        {
            "rank": 2,
            "emp_code": "EMP0002",
            "name": "Bob",
            "department": "Engineering",
            "total_late_minutes": 60,
            "late_count": 2,
        },
        {
            "rank": 2,
            "emp_code": "EMP0003",
            "name": "Charlie",
            "department": "Engineering",
            "total_late_minutes": 60,
            "late_count": 2,
        },
    ]

    with patch.object(attendance_logs_col, "aggregate", return_value=mock_agg):
        resp = test_client.get("/analytics/leaderboard/late?month=2026-07&limit=2")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 3  # More than limit (2) because of tie at cutoff!
        assert items[0]["rank"] == 1
        assert items[1]["rank"] == 2
        assert items[2]["rank"] == 2


# --------------------------------------------------------------------------- #
# 5. Deterministic Ordering of Tied Entries by emp_code Ascending
# --------------------------------------------------------------------------- #
def test_deterministic_emp_code_ordering_for_ties():
    # Both EMP0002 and EMP0001 have total_late_minutes = 50.
    # Deterministic secondary sort by emp_code ascending ensures EMP0001 precedes EMP0002.
    mock_agg = [
        {
            "rank": 1,
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "department": "Engineering",
            "total_late_minutes": 50,
            "late_count": 2,
        },
        {
            "rank": 1,
            "emp_code": "EMP0002",
            "name": "Vikram Shah",
            "department": "Engineering",
            "total_late_minutes": 50,
            "late_count": 2,
        },
    ]

    with patch.object(attendance_logs_col, "aggregate", return_value=mock_agg):
        resp = test_client.get("/analytics/leaderboard/late?month=2026-07")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert items[0]["emp_code"] == "EMP0001"
        assert items[1]["emp_code"] == "EMP0002"


# --------------------------------------------------------------------------- #
# 6. Department Filtering & Scoped Ranking
# --------------------------------------------------------------------------- #
def test_department_filter_ranking_scoped_to_department():
    mock_agg = [
        {
            "rank": 1,
            "emp_code": "EMP0003",
            "name": "Neha Iyer",
            "department": "Sales",
            "total_late_minutes": 35,
            "late_count": 1,
        }
    ]

    with patch.object(attendance_logs_col, "aggregate", return_value=mock_agg) as mock_run:
        resp = test_client.get("/analytics/leaderboard/late?month=2026-07&department=Sales")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 1
        assert data["items"][0]["department"] == "Sales"
        assert data["items"][0]["rank"] == 1


# --------------------------------------------------------------------------- #
# 7. Empty Results (No Late Records or Unknown Department)
# --------------------------------------------------------------------------- #
def test_empty_results_when_no_late_records():
    with patch.object(attendance_logs_col, "aggregate", return_value=[]):
        resp = test_client.get("/analytics/leaderboard/late?month=2026-07")
        assert resp.status_code == 200
        data = resp.json()
        assert data["month"] == "2026-07"
        assert data["items"] == []


def test_empty_results_with_department_filter():
    with patch.object(attendance_logs_col, "aggregate", return_value=[]):
        resp = test_client.get(
            "/analytics/leaderboard/late?month=2026-07&department=NonExistentDept"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["month"] == "2026-07"
        assert data["items"] == []


# --------------------------------------------------------------------------- #
# 8. Exact Response Schema Adherence
# --------------------------------------------------------------------------- #
def test_response_schema_exact_fields():
    mock_agg = [
        {
            "rank": 1,
            "emp_code": "EMP0003",
            "name": "Neha Iyer",
            "department": "Sales",
            "total_late_minutes": 35,
            "late_count": 1,
        }
    ]

    with patch.object(attendance_logs_col, "aggregate", return_value=mock_agg):
        resp = test_client.get("/analytics/leaderboard/late?month=2026-07")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {"month", "items"}
        item = body["items"][0]
        expected_keys = {
            "rank",
            "emp_code",
            "name",
            "department",
            "total_late_minutes",
            "late_count",
        }
        assert set(item.keys()) == expected_keys
        assert "_id" not in item
        assert isinstance(item["rank"], int)
        assert isinstance(item["emp_code"], str)
        assert isinstance(item["name"], str)
        assert isinstance(item["department"], str)
        assert isinstance(item["total_late_minutes"], int)
        assert isinstance(item["late_count"], int)
