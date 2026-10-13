from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
import pytest
from pymongo.errors import DuplicateKeyError

from app.main import app, db, employees_col, attendance_logs_col

test_client = TestClient(app)


def test_health_success():
    with patch.object(db, "command", return_value={"ok": 1}):
        resp = test_client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


def test_health_failure():
    with patch.object(db, "command", side_effect=Exception("Connection timed out")):
        resp = test_client.get("/health")
        assert resp.status_code == 503
        assert resp.json()["detail"] == "Database unavailable"


def test_create_employee_success():
    mock_body = {
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    }
    with patch.object(employees_col, "insert_one") as mock_insert:
        resp = test_client.post("/employees", json=mock_body)
        assert resp.status_code == 201
        data = resp.json()
        assert data["emp_code"] == "EMP0001"
        assert "_id" not in data
        assert isinstance(data["created_at"], int)
        assert data["created_at"] >= 100000000000


def test_create_employee_duplicate_conflict():
    mock_body = {
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    }
    with patch.object(employees_col, "insert_one", side_effect=DuplicateKeyError("duplicate")):
        resp = test_client.post("/employees", json=mock_body)
        assert resp.status_code == 409


def test_create_employee_validation_equal_shifts():
    mock_body = {
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "09:30",
        "joined_on": "2026-01-05",
    }
    resp = test_client.post("/employees", json=mock_body)
    assert resp.status_code == 422


def test_list_employees_pagination_and_sorting():
    fake_docs = [
        {"emp_code": "EMP0001", "name": "Asha", "email": "a@ex.com", "department": "Eng", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-05", "created_at": None}
    ]
    with patch.object(employees_col, "count_documents", return_value=1) as mock_count:
        with patch.object(employees_col, "find") as mock_find:
            mock_cursor = MagicMock()
            mock_cursor.sort.return_value.skip.return_value.limit.return_value = fake_docs
            mock_find.return_value = mock_cursor

            resp = test_client.get("/employees?department=Eng&page=1&page_size=20")
            assert resp.status_code == 200
            data = resp.json()
            assert data["total"] == 1
            assert data["page"] == 1
            assert len(data["items"]) == 1
            mock_count.assert_called_once_with({"department": "Eng"})
            mock_cursor.sort.assert_called_once_with("emp_code", 1)


def test_punch_in_unknown_employee():
    with patch.object(employees_col, "find_one", return_value=None):
        resp = test_client.post("/attendance/punch-in", json={"emp_code": "EMP9999"})
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()


def test_punch_in_duplicate_conflict():
    fake_emp = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
    with patch.object(employees_col, "find_one", return_value=fake_emp):
        with patch.object(attendance_logs_col, "insert_one", side_effect=DuplicateKeyError("duplicate")):
            resp = test_client.post("/attendance/punch-in", json={"emp_code": "EMP0001"})
            assert resp.status_code == 409


def test_punch_in_success_no_id_returned():
    fake_emp = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
    with patch.object(employees_col, "find_one", return_value=fake_emp):
        with patch.object(attendance_logs_col, "insert_one") as mock_insert:
            resp = test_client.post(
                "/attendance/punch-in",
                json={"emp_code": "EMP0001", "punched_at": 1783311001000, "status": "PRESENT"}
            )
            assert resp.status_code == 201
            data = resp.json()
            assert data["emp_code"] == "EMP0001"
            assert data["date"] == "2026-07-06"
            assert data["late_minutes"] == 10
            assert "id" not in data
            assert "_id" not in data
            assert data["punch_out"] is None
            assert data["work_hours"] is None
            assert data["overtime_minutes"] == 0
            assert data["half_day"] is False
            assert data["history"] == []


def test_list_attendance_date_range_validation():
    resp = test_client.get("/attendance?date_from=2026-07-10&date_to=2026-07-01")
    assert resp.status_code == 422
    assert "date_from" in resp.json()["detail"].lower()
