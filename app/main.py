"""
Employee Attendance & Analytics API - Main Application
Contract compliant with openapi.yaml and DATA_MODEL.md.
"""
import calendar
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, status
from pydantic import BaseModel, Field, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

try:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=2000)
except Exception:
    client = MongoClient("mongodb://localhost:27017", serverSelectionTimeoutMS=2000)
db = client[MONGO_DB]
employees_col = db["employees"]
attendance_logs_col = db["attendance_logs"]

IST = timezone(timedelta(hours=5, minutes=30))


def init_db_indexes():
    """Create all required indexes idempotently at application startup."""
    try:
        # employees indexes
        employees_col.create_index([("emp_code", 1)], unique=True)
        employees_col.create_index([("department", 1), ("emp_code", 1)])
        employees_col.create_index([("joined_on", 1)])
        employees_col.create_index([("department", 1), ("joined_on", 1)])

        # attendance_logs indexes
        attendance_logs_col.create_index([("emp_code", 1), ("date", 1)], unique=True)
        attendance_logs_col.create_index([("date", -1), ("emp_code", 1)])
        attendance_logs_col.create_index([("emp_code", 1), ("date", -1)])
    except Exception:
        # Avoid crashing if the database is temporarily unreachable on import
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Date / Time / Calculation Helpers (Rules R1 - R5, R8)
# --------------------------------------------------------------------------- #
def truncate_to_second(dt: datetime) -> datetime:
    """Truncates datetime to whole seconds per R1."""
    return dt.replace(microsecond=0)


def epoch_ms_to_datetime(ms: int) -> datetime:
    """Converts epoch ms to UTC datetime truncated to whole seconds per R1."""
    truncated_ms = (ms // 1000) * 1000
    return datetime.fromtimestamp(truncated_ms / 1000.0, tz=timezone.utc)


def datetime_to_epoch_ms(dt: datetime) -> int:
    """Converts datetime to epoch milliseconds."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def to_ist(dt: datetime) -> datetime:
    """Converts any datetime to aware IST (UTC+05:30)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    """Returns True if the shift crosses midnight (shift_end <= shift_start)."""
    return shift_end <= shift_start


def get_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: A record's date is the IST calendar date of its punch_in.
    Overnight shifts: a punch-in strictly earlier than shift_end belongs to the
    shift that started the previous calendar day.
    """
    punch_in_ist = to_ist(punch_in_utc)
    cal_date = punch_in_ist.date()
    time_str = punch_in_ist.strftime("%H:%M")
    if is_overnight(shift_start, shift_end) and time_str < shift_end:
        cal_date = cal_date - timedelta(days=1)
    return cal_date.isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, date_str: str) -> int:
    """
    R2: Grace period 10 minutes.
    Late only if punch-in is strictly more than 10:00 after shift_start.
    Then late_minutes = whole minutes (floor) elapsed since shift_start, not grace end.
    """
    punch_in_ist = truncate_to_second(to_ist(punch_in_utc))
    sh, sm = map(int, shift_start.split(":"))
    shift_start_dt = datetime.fromisoformat(date_str).replace(
        hour=sh, minute=sm, second=0, microsecond=0, tzinfo=IST
    )
    delta_seconds = (punch_in_ist - shift_start_dt).total_seconds()
    if delta_seconds > 600:
        return int(delta_seconds // 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    t_in = truncate_to_second(punch_in_utc)
    t_out = truncate_to_second(punch_out_utc)
    diff_sec = int((t_out - t_in).total_seconds())
    hours = Decimal(str(diff_sec)) / Decimal("3600")
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: After punch-out, half_day is true when rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def compute_overtime(
    punch_out_utc: datetime, shift_start: str, shift_end: str, date_str: str
) -> int:
    """
    R3: Overtime = whole minutes (floor) from shift_end to punch_out,
    counted only if that is >= 30; otherwise 0.
    For overnight shifts, shift_end falls on the next calendar day.
    """
    eh, em = map(int, shift_end.split(":"))
    cal_date = datetime.fromisoformat(date_str).date()
    if is_overnight(shift_start, shift_end):
        cal_date += timedelta(days=1)
    shift_end_dt = datetime.combine(cal_date, time(eh, em), tzinfo=IST)
    punch_out_ist = truncate_to_second(to_ist(punch_out_utc))
    diff_sec = (punch_out_ist - shift_end_dt).total_seconds()
    mins = int(diff_sec // 60)
    return mins if mins >= 30 else 0


def compute_monthly_working_days(month_str: str, joined_on_str: str) -> int:
    """
    R7: Working days are Mon-Fri, no holiday calendar.
    For an employee who joined mid-month, working days are counted from joined_on.
    If joined_on is after the month, working days is 0.
    """
    year, month = map(int, month_str.split("-"))
    days_in_month = calendar.monthrange(year, month)[1]
    month_start = date(year, month, 1)
    month_end = date(year, month, days_in_month)

    joined_date = date.fromisoformat(joined_on_str)
    if joined_date > month_end:
        return 0

    cur = max(month_start, joined_date)
    working_days = 0
    while cur <= month_end:
        if cur.isoweekday() <= 5:  # Monday to Friday
            working_days += 1
        cur += timedelta(days=1)
    return working_days


def serialize_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Serializes MongoDB attendance document to OpenAPI AttendanceRecord schema."""
    res = {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": (
            datetime_to_epoch_ms(doc["punch_in"])
            if isinstance(doc.get("punch_in"), datetime)
            else doc.get("punch_in")
        ),
        "punch_out": (
            datetime_to_epoch_ms(doc["punch_out"])
            if isinstance(doc.get("punch_out"), datetime)
            else doc.get("punch_out")
        ),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [],
    }
    raw_history = doc.get("history") or []
    for h in raw_history:
        at_val = (
            datetime_to_epoch_ms(h["at"])
            if isinstance(h.get("at"), datetime)
            else h.get("at")
        )
        changes = {}
        for k, v in h.get("changes", {}).items():
            from_val = v.get("from")
            to_val = v.get("to")
            if isinstance(from_val, datetime):
                from_val = datetime_to_epoch_ms(from_val)
            if isinstance(to_val, datetime):
                to_val = datetime_to_epoch_ms(to_val)
            changes[k] = {"from": from_val, "to": to_val}
        res["history"].append(
            {
                "at": at_val,
                "by": h.get("by"),
                "reason": h.get("reason"),
                "changes": changes,
            }
        )
    return res


# --------------------------------------------------------------------------- #
# Request Schemas
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

    @model_validator(mode="after")
    def validate_shift_times(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000, strict=True)
    status: str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000, strict=True)


class RegularizeRequest(BaseModel):
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000, strict=True)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000, strict=True)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    now_utc = truncate_to_second(datetime.now(timezone.utc))
    doc = body.model_dump()
    doc["created_at"] = now_utc
    try:
        employees_col.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409, detail=f"Employee {body.emp_code} already exists"
        )

    doc.pop("_id", None)
    doc["created_at"] = datetime_to_epoch_ms(doc["created_at"])
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department is not None:
        q["department"] = department
    total = employees_col.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        employees_col.find(q, {"_id": 0})
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
    )
    items = []
    for d in cursor:
        if isinstance(d.get("created_at"), datetime):
            d["created_at"] = datetime_to_epoch_ms(d["created_at"])
        items.append(d)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = employees_col.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(
            status_code=404, detail=f"Employee {body.emp_code} not found"
        )

    if body.punched_at is not None:
        dt_utc = epoch_ms_to_datetime(body.punched_at)
    else:
        dt_utc = truncate_to_second(datetime.now(timezone.utc))

    att_date = get_attendance_date(
        dt_utc, emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30")
    )
    late_mins = compute_late_minutes(dt_utc, emp.get("shift_start", "09:30"), att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": dt_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        attendance_logs_col.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409, detail="Already punched in for this date"
        )

    doc.pop("_id", None)
    return serialize_attendance_doc(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from must be less than or equal to date_to",
        )
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = attendance_logs_col.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        attendance_logs_col.find(q, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )
    items = [serialize_attendance_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutRequest):
    emp = employees_col.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(
            status_code=404, detail=f"Employee {body.emp_code} not found"
        )

    if body.punched_at is not None:
        punch_out_dt = epoch_ms_to_datetime(body.punched_at)
    else:
        punch_out_dt = truncate_to_second(datetime.now(timezone.utc))

    # Find the most recent record whose punch_in <= punch_out_dt
    record = attendance_logs_col.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": punch_out_dt},
        },
        sort=[("punch_in", -1)],
    )

    if not record:
        raise HTTPException(
            status_code=404,
            detail=f"No punch-in found for employee {body.emp_code} at or before punched_at",
        )

    punch_in_dt = record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)
    punch_in_dt = truncate_to_second(punch_in_dt)

    if punch_out_dt.tzinfo is None:
        punch_out_dt = punch_out_dt.replace(tzinfo=timezone.utc)

    # 422 validations per contract:
    # "422 if punched_at <= punch_in or more than 24 hours after it"
    if punch_out_dt <= punch_in_dt:
        raise HTTPException(
            status_code=422,
            detail="punched_at must be strictly greater than punch_in",
        )
    if (punch_out_dt - punch_in_dt).total_seconds() > 24 * 3600:
        raise HTTPException(
            status_code=422,
            detail="punched_at cannot be more than 24 hours after punch_in",
        )

    # 409 if already punched out
    if record.get("punch_out") is not None:
        raise HTTPException(
            status_code=409,
            detail="Attendance record is already punched out",
        )

    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    half_day = compute_half_day(work_hours)
    overtime_mins = compute_overtime(
        punch_out_dt,
        emp.get("shift_start", "09:30"),
        emp.get("shift_end", "18:30"),
        record["date"],
    )

    # Atomic update with condition punch_out: None to handle concurrent punch-outs
    updated_record = attendance_logs_col.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "half_day": half_day,
                "overtime_minutes": overtime_mins,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated_record:
        # A concurrent punch-out succeeded first
        raise HTTPException(
            status_code=409,
            detail="Attendance record is already punched out",
        )

    return serialize_attendance_doc(updated_record)


@app.patch("/attendance/{emp_code}/{date}", status_code=200)
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeRequest,
):
    emp = employees_col.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail=f"Employee {emp_code} not found")

    record = attendance_logs_col.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(
            status_code=404,
            detail=f"Attendance record for {emp_code} on {date} not found",
        )

    # Setting status ABSENT or LEAVE clears both punch times,
    # so supplying punch_in or punch_out with it is 422
    if body.status in ["ABSENT", "LEAVE"]:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="Cannot supply punch_in or punch_out when status is ABSENT or LEAVE",
            )

    new_status = body.status if body.status is not None else record.get("status")

    if new_status in ["ABSENT", "LEAVE"]:
        final_punch_in = None
        final_punch_out = None
        final_work_hours = None
        final_late_minutes = 0
        final_overtime_minutes = 0
        final_half_day = False
    else:
        # Presence status requires a punch_in (supplied or already stored)
        if body.punch_in is not None:
            final_punch_in = epoch_ms_to_datetime(body.punch_in)
        else:
            final_punch_in = record.get("punch_in")

        if final_punch_in is None:
            raise HTTPException(
                status_code=422,
                detail="A presence status requires a punch_in",
            )
        if final_punch_in.tzinfo is None:
            final_punch_in = final_punch_in.replace(tzinfo=timezone.utc)
        final_punch_in = truncate_to_second(final_punch_in)

        # punch_in must stay on the record's attendance date (R1)
        calc_date = get_attendance_date(
            final_punch_in,
            emp.get("shift_start", "09:30"),
            emp.get("shift_end", "18:30"),
        )
        if calc_date != date:
            raise HTTPException(
                status_code=422,
                detail=f"punch_in belongs to attendance date {calc_date}, not record date {date}",
            )

        if body.punch_out is not None:
            final_punch_out = epoch_ms_to_datetime(body.punch_out)
        else:
            final_punch_out = record.get("punch_out")

        if final_punch_out is not None:
            if final_punch_out.tzinfo is None:
                final_punch_out = final_punch_out.replace(tzinfo=timezone.utc)
            final_punch_out = truncate_to_second(final_punch_out)

            # punch_out must be after punch_in and within 24 hours
            if final_punch_out <= final_punch_in:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be strictly greater than punch_in",
                )
            if (final_punch_out - final_punch_in).total_seconds() > 24 * 3600:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out cannot be more than 24 hours after punch_in",
                )

            final_work_hours = compute_work_hours(final_punch_in, final_punch_out)
            final_half_day = compute_half_day(final_work_hours)
            final_overtime_minutes = compute_overtime(
                final_punch_out,
                emp.get("shift_start", "09:30"),
                emp.get("shift_end", "18:30"),
                date,
            )
        else:
            final_work_hours = None
            final_half_day = False
            final_overtime_minutes = 0

        final_late_minutes = compute_late_minutes(
            final_punch_in, emp.get("shift_start", "09:30"), date
        )

    # Normalize existing record dates for comparison
    old_status = record.get("status")
    old_punch_in = record.get("punch_in")
    if old_punch_in is not None and old_punch_in.tzinfo is None:
        old_punch_in = old_punch_in.replace(tzinfo=timezone.utc)
    if old_punch_in is not None:
        old_punch_in = truncate_to_second(old_punch_in)

    old_punch_out = record.get("punch_out")
    if old_punch_out is not None and old_punch_out.tzinfo is None:
        old_punch_out = old_punch_out.replace(tzinfo=timezone.utc)
    if old_punch_out is not None:
        old_punch_out = truncate_to_second(old_punch_out)

    old_work_hours = record.get("work_hours")
    old_late_minutes = record.get("late_minutes", 0)
    old_overtime_minutes = record.get("overtime_minutes", 0)
    old_half_day = record.get("half_day", False)

    changes_mongo = {}
    if old_status != new_status:
        changes_mongo["status"] = {"from": old_status, "to": new_status}
    if old_punch_in != final_punch_in:
        changes_mongo["punch_in"] = {"from": old_punch_in, "to": final_punch_in}
    if old_punch_out != final_punch_out:
        changes_mongo["punch_out"] = {"from": old_punch_out, "to": final_punch_out}
    if old_work_hours != final_work_hours:
        changes_mongo["work_hours"] = {"from": old_work_hours, "to": final_work_hours}
    if old_late_minutes != final_late_minutes:
        changes_mongo["late_minutes"] = {"from": old_late_minutes, "to": final_late_minutes}
    if old_overtime_minutes != final_overtime_minutes:
        changes_mongo["overtime_minutes"] = {"from": old_overtime_minutes, "to": final_overtime_minutes}
    if old_half_day != final_half_day:
        changes_mongo["half_day"] = {"from": old_half_day, "to": final_half_day}

    # "a request that changes nothing is 422"
    if not changes_mongo:
        raise HTTPException(
            status_code=422,
            detail="Request modifies no fields",
        )

    now_utc = truncate_to_second(datetime.now(timezone.utc))
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_mongo,
    }

    current_history_len = len(record.get("history") or [])
    updated = attendance_logs_col.find_one_and_update(
        {
            "_id": record["_id"],
            "$expr": {
                "$eq": [{"$size": {"$ifNull": ["$history", []]}}, current_history_len]
            },
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": final_punch_in,
                "punch_out": final_punch_out,
                "work_hours": final_work_hours,
                "late_minutes": final_late_minutes,
                "overtime_minutes": final_overtime_minutes,
                "half_day": final_half_day,
            },
            "$push": {
                "history": history_entry,
            },
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(
            status_code=409,
            detail="Concurrent modification conflict detected; please reload and retry",
        )

    return serialize_attendance_doc(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = employees_col.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(
            status_code=404, detail=f"Employee {emp_code} not found"
        )

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": f"{month}-01", "$lte": f"{month}-31"},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$in": [
                                            {
                                                "$isoDayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$date",
                                                        "format": "%Y-%m-%d",
                                                    }
                                                }
                                            },
                                            [1, 2, 3, 4, 5],
                                        ]
                                    },
                                ]
                            },
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]

    agg_result = list(attendance_logs_col.aggregate(pipeline))

    if agg_result:
        doc = agg_result[0]
        present_days = float(doc.get("present_days", 0.0))
        leave_days = int(doc.get("leave_days", 0))
        late_count = int(doc.get("late_count", 0))
        total_late_minutes = int(doc.get("total_late_minutes", 0))
        total_overtime_minutes = int(doc.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    working_days = compute_monthly_working_days(
        month, emp.get("joined_on", "2000-01-01")
    )

    if working_days > 0:
        ratio = (Decimal(str(present_days)) / Decimal(str(working_days))) * Decimal("100")
        attendance_pct = float(ratio.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


# --------------------------------------------------------------------------- #
# Step 5: Department Monthly Summary Aggregation
# --------------------------------------------------------------------------- #
def build_department_summary_pipeline(
    month_start: str,
    month_end: str,
    department: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Builds the MongoDB cross-collection aggregation pipeline for department summary.
    Starts from employees to guarantee that:
      1. Headcount includes employees with zero attendance logs (R9).
      2. Employees who joined after the reporting period are excluded.
      3. A department filter can leverage the (department, joined_on) index.
    """
    match_filter: Dict[str, Any] = {
        "joined_on": {"$lte": month_end}
    }
    if department is not None:
        match_filter["department"] = department

    return [
        {"$match": match_filter},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$unwind": {
                "path": "$logs",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$group": {
                "_id": "$department",
                "employees": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {
                                        "$in": [
                                            "$logs.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {
                                        "$in": [
                                            {
                                                "$isoDayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": {
                                                            "$ifNull": [
                                                                "$logs.date",
                                                                "2000-01-01",
                                                            ]
                                                        },
                                                        "format": "%Y-%m-%d",
                                                    }
                                                }
                                            },
                                            [1, 2, 3, 4, 5],
                                        ]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {"$ifNull": ["$logs.half_day", False]},
                                    0.5,
                                    1.0,
                                ]
                            },
                            0.0,
                        ]
                    }
                },
                "total_work_hours": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {
                                        "$in": [
                                            "$logs.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            0.0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {
                                        "$in": [
                                            "$logs.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {
                                        "$gt": [
                                            {"$ifNull": ["$logs.late_minutes", 0]},
                                            0,
                                        ]
                                    },
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$cond": [
                            {"$ne": ["$logs", None]},
                            {"$ifNull": ["$logs.late_minutes", 0]},
                            0,
                        ]
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$eq": ["$logs.status", "LEAVE"]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$eq": ["$logs.status", "ON_DUTY"]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$employees"},
                "present_days": 1,
                "total_work_hours": 1,
                "work_hours_count": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
):
    """
    GET /analytics/departments/summary
    Department summary for a month via a cross-collection MongoDB aggregation.
    """
    year, m = map(int, month.split("-"))
    days_in_month = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{days_in_month:02d}"

    pipeline = build_department_summary_pipeline(
        month_start=month_start,
        month_end=month_end,
        department=department,
    )

    agg_results = list(employees_col.aggregate(pipeline))

    items = []
    for doc in agg_results:
        dept_name = doc["department"]
        headcount = int(doc["headcount"])
        if headcount == 0:
            continue

        raw_present_days = doc.get("present_days", 0.0)
        present_days = float(
            Decimal(str(raw_present_days)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        )

        total_work_hours = doc.get("total_work_hours", 0.0)
        work_hours_count = int(doc.get("work_hours_count", 0))
        if work_hours_count > 0:
            mean_hours = Decimal(str(total_work_hours)) / Decimal(str(work_hours_count))
            avg_work_hours = float(
                mean_hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            )
        else:
            avg_work_hours = None

        late_count = int(doc.get("late_count", 0))
        total_late_minutes = int(doc.get("total_late_minutes", 0))
        leave_count = int(doc.get("leave_count", 0))
        on_duty_count = int(doc.get("on_duty_count", 0))

        items.append(
            {
                "department": dept_name,
                "headcount": headcount,
                "present_days": present_days,
                "avg_work_hours": avg_work_hours,
                "late_count": late_count,
                "total_late_minutes": total_late_minutes,
                "leave_count": leave_count,
                "on_duty_count": on_duty_count,
            }
        )

    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# Step 6: Late-Arrival Leaderboard
# --------------------------------------------------------------------------- #
def build_late_leaderboard_pipeline(
    month_start: str,
    month_end: str,
    limit: int = 10,
    department: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Builds the MongoDB aggregation pipeline for the late-arrival leaderboard.
    1. Matches attendance logs in reporting month where late_minutes > 0.
    2. Groups by emp_code: computes total_late_minutes and late_count.
    3. Joins employees to resolve name and department (ignoring non-existent employees).
    4. Applies optional department filter BEFORE ranking (so ranking is partitioned/scoped to department).
    5. Computes standard competition rank (1, 2, 2, 4) using $setWindowFields with $rank.
    6. Filters rank <= limit AFTER ranking.
    7. Projects the required output fields.
    8. Sorts by total_late_minutes DESC, then emp_code ASC.
    """
    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {
            "$unwind": "$employee"
        },
    ]

    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {
                        "$rank": {}
                    }
                },
            }
        },
        {
            "$match": {
                "rank": {"$lte": limit}
            }
        },
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {
            "$sort": {
                "total_late_minutes": -1,
                "emp_code": 1,
            }
        },
    ])
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    """
    GET /analytics/leaderboard/late
    Late-comers leaderboard for a month. Ranks employees by total late_minutes.
    """
    year, m = map(int, month.split("-"))
    days_in_month = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{days_in_month:02d}"

    pipeline = build_late_leaderboard_pipeline(
        month_start=month_start,
        month_end=month_end,
        limit=limit,
        department=department,
    )

    agg_results = list(attendance_logs_col.aggregate(pipeline))

    items = []
    for doc in agg_results:
        items.append(
            {
                "rank": int(doc["rank"]),
                "emp_code": str(doc["emp_code"]),
                "name": str(doc["name"]),
                "department": str(doc["department"]),
                "total_late_minutes": int(doc["total_late_minutes"]),
                "late_count": int(doc["late_count"]),
            }
        )

    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# Step 7: Department Attendance Trend
# --------------------------------------------------------------------------- #
def build_department_trend_pipeline(
    department: str,
    from_date_str: str,
    to_date_str: str,
) -> List[Dict[str, Any]]:
    """
    Builds the MongoDB aggregation pipeline for daily department attendance trend.
    1. Matches employees by department (uses IXSCAN) and seeds the series.
    2. Uses $densify on BSON Date field date_dt to generate exactly one document
       for every calendar day from from_date_str to to_date_str inclusive.
    3. Computes is_working_day ($isoDayOfWeek 1-5).
    4. Computes daily headcount via $lookup matching department employees joined_on <= date.
    5. Computes daily present_count and late_count via $lookup to attendance_logs.
    6. Calculates attendance_rate (present_count / headcount) on working days with headcount > 0.
    7. Calculates 7-calendar-day moving average using $setWindowFields with $avg over [-6, "current"].
    8. Projects exact required fields and sorts by date ascending.
    """
    d_from = date.fromisoformat(from_date_str)
    d_to = date.fromisoformat(to_date_str)
    from_dt_utc = datetime.combine(d_from, time.min, tzinfo=timezone.utc)
    to_dt_plus_one_utc = datetime.combine(d_to + timedelta(days=1), time.min, tzinfo=timezone.utc)

    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {
            "$project": {
                "_id": 0,
                "date_dt": {
                    "$dateFromString": {
                        "dateString": from_date_str,
                        "format": "%Y-%m-%d",
                    }
                },
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt_utc, to_dt_plus_one_utc],
                },
            }
        },
        {
            "$addFields": {
                "date": {
                    "$dateToString": {
                        "date": "$date_dt",
                        "format": "%Y-%m-%d",
                    }
                },
                "is_working_day": {
                    "$in": [
                        {"$isoDayOfWeek": "$date_dt"},
                        [1, 2, 3, 4, 5],
                    ]
                },
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$day"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "hc",
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"day": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$date", "$$day"]}
                        }
                    },
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "emp",
                        }
                    },
                    {
                        "$match": {
                            "emp.department": department
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                                        0.0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "att",
            }
        },
        {
            "$addFields": {
                "headcount": {"$ifNull": [{"$first": "$hc.count"}, 0]},
                "present_count": {"$ifNull": [{"$first": "$att.present_count"}, 0.0]},
                "late_count": {"$ifNull": [{"$first": "$att.late_count"}, 0]},
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$round": [
                                {"$divide": ["$present_count", "$headcount"]},
                                4,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date_dt": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"],
                        },
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """
    GET /analytics/departments/{department}/trend
    Returns daily attendance trend for a department from from_date to to_date inclusive.
    """
    try:
        d_from = date.fromisoformat(from_date)
        d_to = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format")

    if d_to < d_from:
        raise HTTPException(
            status_code=422, detail="'to' date cannot be earlier than 'from' date"
        )

    num_days = (d_to - d_from).days + 1
    if num_days > 92:
        raise HTTPException(
            status_code=422, detail="Date range cannot exceed 92 days"
        )

    dept_emp = employees_col.find_one({"department": department})
    if not dept_emp:
        raise HTTPException(
            status_code=404, detail=f"Department '{department}' not found"
        )

    pipeline = build_department_trend_pipeline(
        department=department,
        from_date_str=from_date,
        to_date_str=to_date,
    )

    agg_results = list(employees_col.aggregate(pipeline))

    # Format items and enforce exact R8 Half-Up rounding
    items = []
    for doc in agg_results:
        raw_present_count = doc.get("present_count", 0.0)
        present_count = float(
            Decimal(str(raw_present_count)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        )
        headcount = int(doc.get("headcount", 0))
        is_working_day = bool(doc.get("is_working_day", False))
        late_count = int(doc.get("late_count", 0))

        raw_rate = doc.get("attendance_rate")
        if raw_rate is not None:
            attendance_rate = float(
                Decimal(str(raw_rate)).quantize(
                    Decimal("0.0001"), rounding=ROUND_HALF_UP
                )
            )
        else:
            attendance_rate = None

        raw_mavg = doc.get("moving_avg_7d")
        if raw_mavg is not None:
            moving_avg_7d = float(
                Decimal(str(raw_mavg)).quantize(
                    Decimal("0.0001"), rounding=ROUND_HALF_UP
                )
            )
        else:
            moving_avg_7d = None

        items.append(
            {
                "date": str(doc["date"]),
                "is_working_day": is_working_day,
                "headcount": headcount,
                "present_count": present_count,
                "late_count": late_count,
                "attendance_rate": attendance_rate,
                "moving_avg_7d": moving_avg_7d,
            }
        )

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Step 8: MongoDB Explain Endpoint
# --------------------------------------------------------------------------- #
def sanitize_explain_doc(obj: Any) -> Any:
    """Recursively converts BSON datatypes (ObjectId, datetime, Timestamp, etc.) to JSON-safe values."""
    if isinstance(obj, dict):
        return {str(k): sanitize_explain_doc(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple, set)):
        return [sanitize_explain_doc(item) for item in obj]
    elif isinstance(obj, (datetime, date)):
        return obj.isoformat()
    elif isinstance(obj, (bool, int, float, str)) or obj is None:
        return obj
    elif hasattr(obj, "__str__"):
        return str(obj)
    return obj


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
    limit: Optional[int] = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    GET /admin/explain/{endpoint}
    Runs MongoDB explain with executionStats verbosity for the main query or aggregation
    of the named endpoint, built exactly as that endpoint builds it.
    """
    supported_endpoints = [
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ]
    if endpoint not in supported_endpoints:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported endpoint: '{endpoint}'. Expected one of {supported_endpoints}",
        )

    try:
        if endpoint == "attendance_list":
            collection = "attendance_logs"
            if date_from and date_to and date_from > date_to:
                raise HTTPException(
                    status_code=422,
                    detail="date_from must be less than or equal to date_to",
                )
            q = {}
            if emp_code:
                q["emp_code"] = emp_code
            if date_from or date_to:
                q["date"] = {}
                if date_from:
                    q["date"]["$gte"] = date_from
                if date_to:
                    q["date"]["$lte"] = date_to
            if status:
                q["status"] = status
            skip = (page - 1) * page_size
            find_cmd = {
                "find": collection,
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            }
            explain_result = db.command("explain", find_cmd, verbosity="executionStats")

        elif endpoint == "employee_monthly":
            collection = "attendance_logs"
            if not emp_code or not month:
                raise HTTPException(
                    status_code=422,
                    detail="'emp_code' and 'month' are required for employee_monthly",
                )
            pipeline = [
                {
                    "$match": {
                        "emp_code": emp_code,
                        "date": {"$gte": f"{month}-01", "$lte": f"{month}-31"},
                    }
                },
                {
                    "$group": {
                        "_id": None,
                        "present_days": {
                            "$sum": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {
                                                "$in": [
                                                    {
                                                        "$isoDayOfWeek": {
                                                            "$dateFromString": {
                                                                "dateString": "$date",
                                                                "format": "%Y-%m-%d",
                                                            }
                                                        }
                                                    },
                                                    [1, 2, 3, 4, 5],
                                                ]
                                            },
                                        ]
                                    },
                                    {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                                    0.0,
                                ]
                            }
                        },
                        "leave_days": {
                            "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                        },
                        "late_count": {
                            "$sum": {
                                "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
                            }
                        },
                        "total_late_minutes": {
                            "$sum": {"$ifNull": ["$late_minutes", 0]}
                        },
                        "total_overtime_minutes": {
                            "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                        },
                    }
                },
            ]
            agg_cmd = {
                "aggregate": collection,
                "pipeline": pipeline,
                "cursor": {},
            }
            explain_result = db.command("explain", agg_cmd, verbosity="executionStats")

        elif endpoint == "department_summary":
            collection = "employees"
            if not month:
                raise HTTPException(
                    status_code=422,
                    detail="'month' is required for department_summary",
                )
            year, m = map(int, month.split("-"))
            days_in_month = calendar.monthrange(year, m)[1]
            month_start = f"{month}-01"
            month_end = f"{month}-{days_in_month:02d}"
            pipeline = build_department_summary_pipeline(
                month_start=month_start,
                month_end=month_end,
                department=department,
            )
            agg_cmd = {
                "aggregate": collection,
                "pipeline": pipeline,
                "cursor": {},
            }
            explain_result = db.command("explain", agg_cmd, verbosity="executionStats")

        elif endpoint == "late_leaderboard":
            collection = "attendance_logs"
            if not month:
                raise HTTPException(
                    status_code=422,
                    detail="'month' is required for late_leaderboard",
                )
            effective_limit = limit if limit is not None else 10
            year, m = map(int, month.split("-"))
            days_in_month = calendar.monthrange(year, m)[1]
            month_start = f"{month}-01"
            month_end = f"{month}-{days_in_month:02d}"
            pipeline = build_late_leaderboard_pipeline(
                month_start=month_start,
                month_end=month_end,
                limit=effective_limit,
                department=department,
            )
            agg_cmd = {
                "aggregate": collection,
                "pipeline": pipeline,
                "cursor": {},
            }
            explain_result = db.command("explain", agg_cmd, verbosity="executionStats")

        elif endpoint == "department_trend":
            collection = "employees"
            if not department or not from_date or not to_date:
                raise HTTPException(
                    status_code=422,
                    detail="'department', 'from', and 'to' are required for department_trend",
                )
            try:
                d_from = date.fromisoformat(from_date)
                d_to = date.fromisoformat(to_date)
            except ValueError:
                raise HTTPException(status_code=422, detail="Invalid date format")

            if d_to < d_from:
                raise HTTPException(
                    status_code=422,
                    detail="'to' date cannot be earlier than 'from' date",
                )
            num_days = (d_to - d_from).days + 1
            if num_days > 92:
                raise HTTPException(
                    status_code=422, detail="Date range cannot exceed 92 days"
                )
            pipeline = build_department_trend_pipeline(
                department=department,
                from_date_str=from_date,
                to_date_str=to_date,
            )
            agg_cmd = {
                "aggregate": collection,
                "pipeline": pipeline,
                "cursor": {},
            }
            explain_result = db.command("explain", agg_cmd, verbosity="executionStats")

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"Database unavailable: {str(e)}",
        )

    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": sanitize_explain_doc(explain_result),
    }
    