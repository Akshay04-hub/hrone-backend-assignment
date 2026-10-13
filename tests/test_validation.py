import pytest
from pydantic import ValidationError
from app.main import EmployeeCreate, PunchInRequest


def test_employee_create_valid():
    emp = EmployeeCreate(
        emp_code="EMP0001",
        name="Asha Rao",
        email="asha@example.com",
        department="Engineering",
        shift_start="09:30",
        shift_end="18:30",
        joined_on="2026-01-05",
    )
    assert emp.emp_code == "EMP0001"


def test_employee_create_equal_shifts_rejected():
    with pytest.raises(ValidationError):
        EmployeeCreate(
            emp_code="EMP0001",
            name="Asha Rao",
            email="asha@example.com",
            department="Engineering",
            shift_start="09:30",
            shift_end="09:30",
            joined_on="2026-01-05",
        )


def test_employee_create_invalid_emp_code():
    # Only 3 digits (pattern requires EMP followed by 4-6 digits)
    with pytest.raises(ValidationError):
        EmployeeCreate(
            emp_code="EMP123",
            name="Asha Rao",
            email="asha@example.com",
            department="Engineering",
            joined_on="2026-01-05",
        )

    # Lowercase emp
    with pytest.raises(ValidationError):
        EmployeeCreate(
            emp_code="emp0001",
            name="Asha Rao",
            email="asha@example.com",
            department="Engineering",
            joined_on="2026-01-05",
        )


def test_employee_create_invalid_email():
    with pytest.raises(ValidationError):
        EmployeeCreate(
            emp_code="EMP0001",
            name="Asha Rao",
            email="invalid-email",
            department="Engineering",
            joined_on="2026-01-05",
        )


def test_punch_in_request_valid():
    p = PunchInRequest(emp_code="EMP0001", punched_at=1783312500000, status="WFH")
    assert p.status == "WFH"


def test_punch_in_request_invalid_status():
    # Only PRESENT, WFH, ON_DUTY allowed for punch-in
    with pytest.raises(ValidationError):
        PunchInRequest(emp_code="EMP0001", status="ABSENT")

    with pytest.raises(ValidationError):
        PunchInRequest(emp_code="EMP0001", status="LEAVE")


def test_punch_in_request_seconds_looking_timestamp():
    # Less than 100000000000
    with pytest.raises(ValidationError):
        PunchInRequest(emp_code="EMP0001", punched_at=1783312500)


def test_punch_in_request_float_timestamp():
    with pytest.raises(ValidationError):
        PunchInRequest(emp_code="EMP0001", punched_at=1783312500000.5)
