from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from app.main import app, employees_col, attendance_logs_col

test_client = TestClient(app)


# --------------------------------------------------------------------------- #
# Tests for POST /attendance/punch-out
# --------------------------------------------------------------------------- #
def test_punch_out_unknown_employee():
    with patch.object(employees_col, "find_one", return_value=None):
        resp = test_client.post("/attendance/punch-out", json={"emp_code": "EMP9999"})
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()


def test_punch_out_no_open_record():
    fake_emp = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
    with patch.object(employees_col, "find_one", return_value=fake_emp):
        with patch.object(attendance_logs_col, "find_one", return_value=None):
            resp = test_client.post("/attendance/punch-out", json={"emp_code": "EMP0001"})
            assert resp.status_code == 404
            assert "no punch-in found" in resp.json()["detail"].lower()


def test_punch_out_already_closed():
    fake_emp = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
    # Record has punch_out already set
    fake_record = {
        "_id": "dummy_id",
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "punch_in": datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc),
        "punch_out": datetime(2026, 7, 6, 13, 0, 0, tzinfo=timezone.utc),
    }
    with patch.object(employees_col, "find_one", return_value=fake_emp):
        with patch.object(attendance_logs_col, "find_one", return_value=fake_record):
            # Attempt to punch out at 13:30 UTC
            resp = test_client.post(
                "/attendance/punch-out",
                json={"emp_code": "EMP0001", "punched_at": 1783344600000},
            )
            assert resp.status_code == 409
            assert "already punched out" in resp.json()["detail"].lower()


def test_punch_out_validation_before_punch_in_or_over_24h():
    fake_emp = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
    punch_in_dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    fake_record = {
        "_id": "dummy_id",
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "punch_in": punch_in_dt,
        "punch_out": None,
    }
    with patch.object(employees_col, "find_one", return_value=fake_emp):
        with patch.object(attendance_logs_col, "find_one", return_value=fake_record):
            # Test 1: punched_at <= punch_in (04:00 UTC = 1783310400000 ms)
            resp = test_client.post(
                "/attendance/punch-out",
                json={"emp_code": "EMP0001", "punched_at": 1783310400000},
            )
            assert resp.status_code == 422

            # Test 2: punched_at > punch_in + 24h
            # 04:00 UTC + 25 hours = 1783400400000 ms
            resp = test_client.post(
                "/attendance/punch-out",
                json={"emp_code": "EMP0001", "punched_at": 1783310400000 + 25 * 3600 * 1000},
            )
            assert resp.status_code == 422


def test_punch_out_successful_standard_shift():
    fake_emp = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
    # punch_in at 09:28 IST (03:58 UTC) on 2026-07-06 = 1783310280000 ms
    # punch_out at 18:35 IST (13:05 UTC) on 2026-07-06 = 1783343100000 ms
    # work_hours = (13:05 - 03:58) = 9 hours 7 mins = 32820 sec / 3600 = 9.1166... -> 9.12
    # overtime = 5 min (< 30 min) -> 0
    # half_day = 9.12 < 4.50 -> False
    fake_record = {
        "_id": "dummy_id",
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    def fake_update(filt, update, return_document=None):
        updated = dict(fake_record)
        updated.update(update["$set"])
        return updated

    with patch.object(employees_col, "find_one", return_value=fake_emp):
        with patch.object(attendance_logs_col, "find_one", return_value=fake_record):
            with patch.object(attendance_logs_col, "find_one_and_update", side_effect=fake_update):
                resp = test_client.post(
                    "/attendance/punch-out",
                    json={"emp_code": "EMP0001", "punched_at": 1783343100000},
                )
                assert resp.status_code == 200
                data = resp.json()
                assert data["emp_code"] == "EMP0001"
                assert data["date"] == "2026-07-06"
                assert data["punch_out"] == 1783343100000
                assert data["work_hours"] == 9.12
                assert data["overtime_minutes"] == 0
                assert data["half_day"] is False
                assert "_id" not in data
                assert "id" not in data


def test_punch_out_overtime_and_overnight_shift():
    # Overnight shift: 22:00 - 06:00
    fake_emp = {"emp_code": "EMP0005", "shift_start": "22:00", "shift_end": "06:00"}
    # Punched in 21:55 IST on 2026-07-06 (16:25 UTC)
    # Punched out 06:40 IST on 2026-07-07 (01:10 UTC next day)
    # Overtime = 40 min (>= 30) -> 40
    # Work hours = (01:10 next day - 16:25) = 8 hours 45 min = 31500 sec / 3600 = 8.75
    fake_record = {
        "_id": "dummy_id",
        "emp_code": "EMP0005",
        "date": "2026-07-06",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 6, 16, 25, 0, tzinfo=timezone.utc),
        "punch_out": None,
        "history": [],
    }

    def fake_update(filt, update, return_document=None):
        updated = dict(fake_record)
        updated.update(update["$set"])
        return updated

    with patch.object(employees_col, "find_one", return_value=fake_emp):
        with patch.object(attendance_logs_col, "find_one", return_value=fake_record):
            with patch.object(attendance_logs_col, "find_one_and_update", side_effect=fake_update):
                # 2026-07-07 01:10:00 UTC = 1783386600000 ms
                resp = test_client.post(
                    "/attendance/punch-out",
                    json={"emp_code": "EMP0005", "punched_at": 1783386600000},
                )
                assert resp.status_code == 200
                data = resp.json()
                assert data["work_hours"] == 8.75
                assert data["overtime_minutes"] == 40
                assert data["half_day"] is False


def test_punch_out_half_day():
    fake_emp = {"emp_code": "EMP0004", "shift_start": "09:30", "shift_end": "18:30"}
    # Worked 3.5 hours: 09:30 to 13:00 IST (04:00 to 07:30 UTC)
    fake_record = {
        "_id": "dummy_id",
        "emp_code": "EMP0004",
        "date": "2026-07-07",
        "status": "WFH",
        "punch_in": datetime(2026, 7, 7, 4, 0, 0, tzinfo=timezone.utc),
        "punch_out": None,
        "history": [],
    }

    def fake_update(filt, update, return_document=None):
        updated = dict(fake_record)
        updated.update(update["$set"])
        return updated

    with patch.object(employees_col, "find_one", return_value=fake_emp):
        with patch.object(attendance_logs_col, "find_one", return_value=fake_record):
            with patch.object(attendance_logs_col, "find_one_and_update", side_effect=fake_update):
                # 2026-07-07 07:30:00 UTC = 1783409400000 ms
                resp = test_client.post(
                    "/attendance/punch-out",
                    json={"emp_code": "EMP0004", "punched_at": 1783409400000},
                )
                assert resp.status_code == 200
                data = resp.json()
                assert data["work_hours"] == 3.5
                assert data["half_day"] is True


def test_punch_out_concurrency_race():
    # Simulates two simultaneous punch-outs: find_one_and_update returns None for loser
    fake_emp = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
    fake_record = {
        "_id": "dummy_id",
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc),
        "punch_out": None,
    }
    with patch.object(employees_col, "find_one", return_value=fake_emp):
        with patch.object(attendance_logs_col, "find_one", return_value=fake_record):
            # Atomic update returns None because concurrent request already set punch_out
            with patch.object(attendance_logs_col, "find_one_and_update", return_value=None):
                resp = test_client.post(
                    "/attendance/punch-out",
                    json={"emp_code": "EMP0001", "punched_at": 1783343100000},
                )
                assert resp.status_code == 409


# --------------------------------------------------------------------------- #
# Tests for PATCH /attendance/{emp_code}/{date}
# --------------------------------------------------------------------------- #
def test_patch_unknown_employee():
    with patch.object(employees_col, "find_one", return_value=None):
        resp = test_client.patch(
            "/attendance/EMP9999/2026-07-06",
            json={"reason": "Correction", "regularized_by": "manager"},
        )
        assert resp.status_code == 404


def test_patch_missing_attendance_record():
    fake_emp = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
    with patch.object(employees_col, "find_one", return_value=fake_emp):
        with patch.object(attendance_logs_col, "find_one", return_value=None):
            resp = test_client.patch(
                "/attendance/EMP0001/2026-07-06",
                json={"reason": "Correction", "regularized_by": "manager"},
            )
            assert resp.status_code == 404


def test_patch_absent_with_punch_times_rejected():
    fake_emp = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
    fake_record = {
        "_id": "dummy_id",
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc),
        "punch_out": datetime(2026, 7, 6, 13, 0, 0, tzinfo=timezone.utc),
    }
    with patch.object(employees_col, "find_one", return_value=fake_emp):
        with patch.object(attendance_logs_col, "find_one", return_value=fake_record):
            # Supplying punch_in with ABSENT is 422
            resp = test_client.patch(
                "/attendance/EMP0001/2026-07-06",
                json={
                    "status": "ABSENT",
                    "punch_in": 1783310280000,
                    "reason": "Sick leave update",
                    "regularized_by": "hr.admin",
                },
            )
            assert resp.status_code == 422


def test_patch_punch_in_wrong_attendance_date_rejected():
    fake_emp = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
    fake_record = {
        "_id": "dummy_id",
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc),
        "punch_out": None,
    }
    with patch.object(employees_col, "find_one", return_value=fake_emp):
        with patch.object(attendance_logs_col, "find_one", return_value=fake_record):
            # Punch in timestamp for 2026-07-07 09:30 IST instead of 2026-07-06
            # 2026-07-07 04:00:00 UTC = 1783396800000 ms
            resp = test_client.patch(
                "/attendance/EMP0001/2026-07-06",
                json={
                    "punch_in": 1783396800000,
                    "reason": "Date mismatch test",
                    "regularized_by": "hr.admin",
                },
            )
            assert resp.status_code == 422
            assert "not record date" in resp.json()["detail"].lower()


def test_patch_changes_nothing_rejected():
    fake_emp = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
    fake_record = {
        "_id": "dummy_id",
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc),
        "punch_out": datetime(2026, 7, 6, 13, 0, 0, tzinfo=timezone.utc),
        "work_hours": 9.03,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    with patch.object(employees_col, "find_one", return_value=fake_emp):
        with patch.object(attendance_logs_col, "find_one", return_value=fake_record):
            # Same status PRESENT, no punch times changed
            resp = test_client.patch(
                "/attendance/EMP0001/2026-07-06",
                json={
                    "status": "PRESENT",
                    "reason": "No actual change",
                    "regularized_by": "hr.admin",
                },
            )
            assert resp.status_code == 422
            assert "modifies no fields" in resp.json()["detail"].lower()


def test_patch_successful_regularization_and_history():
    fake_emp = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
    # Initial state: Late punch in 10:05 IST (04:35 UTC) = 35 min late
    old_punch_in = datetime(2026, 7, 7, 4, 35, 0, tzinfo=timezone.utc)
    old_punch_out = datetime(2026, 7, 7, 13, 0, 0, tzinfo=timezone.utc)
    fake_record = {
        "_id": "dummy_id",
        "emp_code": "EMP0001",
        "date": "2026-07-07",
        "status": "PRESENT",
        "punch_in": old_punch_in,
        "punch_out": old_punch_out,
        "work_hours": 8.42,
        "late_minutes": 35,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    # Correct punch_in to 09:28 IST (03:58 UTC) = 1783396680000 ms
    # Recalculates late_minutes from 35 to 0, work_hours from 8.42 to 9.03
    def fake_update(filt, update, return_document=None):
        updated = dict(fake_record)
        updated.update(update["$set"])
        updated["history"] = list(fake_record.get("history", [])) + [update["$push"]["history"]]
        return updated

    with patch.object(employees_col, "find_one", return_value=fake_emp):
        with patch.object(attendance_logs_col, "find_one", return_value=fake_record):
            with patch.object(attendance_logs_col, "find_one_and_update", side_effect=fake_update):
                resp = test_client.patch(
                    "/attendance/EMP0001/2026-07-07",
                    json={
                        "punch_in": 1783396680000,
                        "reason": "biometric glitch",
                        "regularized_by": "hr.admin",
                    },
                )
                assert resp.status_code == 200
                data = resp.json()
                assert data["late_minutes"] == 0
                assert data["work_hours"] == 9.03
                assert len(data["history"]) == 1
                h = data["history"][0]
                assert h["by"] == "hr.admin"
                assert h["reason"] == "biometric glitch"
                assert "late_minutes" in h["changes"]
                assert h["changes"]["late_minutes"] == {"from": 35, "to": 0}
                assert "work_hours" in h["changes"]
                assert h["changes"]["work_hours"] == {"from": 8.42, "to": 9.03}
                assert "punch_in" in h["changes"]
                assert h["changes"]["punch_in"]["to"] == 1783396680000


def test_patch_concurrency_race():
    fake_emp = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
    fake_record = {
        "_id": "dummy_id",
        "emp_code": "EMP0001",
        "date": "2026-07-07",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 7, 4, 35, 0, tzinfo=timezone.utc),
        "punch_out": datetime(2026, 7, 7, 13, 0, 0, tzinfo=timezone.utc),
        "history": [],
    }
    with patch.object(employees_col, "find_one", return_value=fake_emp):
        with patch.object(attendance_logs_col, "find_one", return_value=fake_record):
            # Optimistic lock check failed (another edit appended to history first)
            with patch.object(attendance_logs_col, "find_one_and_update", return_value=None):
                resp = test_client.patch(
                    "/attendance/EMP0001/2026-07-07",
                    json={
                        "punch_in": 1783396680000,
                        "reason": "concurrent edit",
                        "regularized_by": "hr.admin",
                    },
                )
                assert resp.status_code == 409
                assert "conflict" in resp.json()["detail"].lower()
