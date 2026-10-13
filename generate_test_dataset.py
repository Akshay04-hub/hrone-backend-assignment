#!/usr/bin/env python3
"""Reproducible performance-test dataset generator for the HROne Attendance API.

Generates ~1,000 employees and ~100,000 attendance records that follow DATA_MODEL.md
and the business rules R1-R10 in openapi.yaml.

SAFETY MODEL
  * The target database is the constant TARGET_DB ("hrone_attendance_test"). MONGO_DB is never read.
  * "hrone_attendance" is refused as a target; the script never connects to it.
  * Default mode is a DRY RUN: everything is generated and validated in memory, MongoDB is NOT contacted
    (pymongo is not even imported).
  * Writing requires BOTH --write and --confirm-write-to-hrone_attendance_test.
  * Only insert_many / create_index / count_documents / find_one are used. No delete, drop or update.
  * If either target collection already has documents the script aborts (no append mode).
  * Only MONGO_URI is read (real environment first, then the MONGO_URI key of .env). It is never printed.

USAGE
  python generate_test_dataset.py                       # dry run (default)
  python generate_test_dataset.py --write --confirm-write-to-hrone_attendance_test
  python generate_test_dataset.py --write --confirm-write-to-hrone_attendance_test --skip-indexes
"""
from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import os
import random
import re
import sys
import time
from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------
TARGET_DB = "hrone_attendance_test"
PROTECTED_DB = "hrone_attendance"
CONFIRM_FLAG = "--confirm-write-to-hrone_attendance_test"
DEFAULT_SEED = 20260712
DEFAULT_EMPLOYEES = 1000
DEFAULT_TARGET = 100_000
DEFAULT_BATCH = 5000
MIN_EMPLOYEES = 300  # the tie / role scenarios need enough people per department

TODAY = date(2026, 10, 12)  # fixed so the output is reproducible
WINDOW_START = date(2026, 5, 1)
WINDOW_END = date(2026, 10, 9)

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
NOW_UTC = datetime(2026, 10, 12, 7, 0, 0, tzinfo=UTC)  # 12:30 IST on TODAY

GRACE_SECONDS = 600                      # R2
OVERTIME_MIN = 30                        # R3
HALF_DAY_THRESHOLD = Decimal("4.50")     # R5 (compared against the ROUNDED work_hours)

PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = ("PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE")
TRACKED = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")

# (name, weight, probability that an employee is on an overnight shift)
DEPARTMENTS = [
    ("Engineering", 24, 0.01), ("Sales", 16, 0.0), ("Support", 15, 0.35), ("Operations", 12, 0.15),
    ("Finance", 8, 0.0), ("Marketing", 7, 0.0), ("Human Resources", 6, 0.0), ("Legal", 4, 0.0),
    ("Customer Success", 8, 0.05),
]
# Departments are case-sensitive (DATA_MODEL.md): a tiny lower-case twin checks exact-match handling.
LOWERCASE_DEPT = ("engineering", 3)

DAY_SHIFTS = [("09:30", "18:30", 55), ("09:00", "18:00", 15), ("10:00", "19:00", 12),
              ("08:30", "17:30", 8), ("09:00", "17:00", 5), ("11:00", "20:00", 5)]
NIGHT_SHIFTS = [("22:00", "06:00", 50), ("20:00", "04:00", 20), ("23:00", "07:00", 20), ("21:00", "05:00", 10)]

FIRST = ["Asha", "Vikram", "Neha", "Rohit", "Meera", "Karan", "Priya", "Arjun", "Sneha", "Rahul", "Ananya",
         "Aditya", "Kavya", "Rajesh", "Pooja", "Suresh", "Divya", "Amit", "Nisha", "Manish", "Ishita", "Varun",
         "Tanvi", "Harsh", "Lakshmi", "Sanjay", "Deepa", "Naveen", "Swati", "Gaurav"]
LAST = ["Rao", "Shah", "Iyer", "Das", "Nair", "Bedi", "Sharma", "Reddy", "Patel", "Gupta", "Menon", "Kulkarni",
        "Singh", "Joshi", "Mehta", "Pillai", "Verma", "Chopra", "Banerjee", "Naidu", "Kapoor", "Desai", "Bhat",
        "Agarwal", "Mishra"]

# Engineered monthly late-minute totals -> leaderboard ties (standard competition ranking).
# July  expected ranks: 1,2,2,4,5,5,5 | dept pairs: Finance 1500 x2 (rank 1 in dept), Legal 1400 x2
# Sept  expected ranks: 1,2,2,2,5,5,7 | dept groups: Marketing 1600 x2, Human Resources 1300 x3
TIE_SPECS = {
    "2026-07": [("Engineering", 3000), ("Engineering", 2800), ("Sales", 2800), ("Sales", 2600),
                ("Support", 2400), ("Engineering", 2400), ("Operations", 2400),
                ("Finance", 1500), ("Finance", 1500), ("Legal", 1400), ("Legal", 1400)],
    "2026-09": [("Sales", 2900), ("Engineering", 2700), ("Operations", 2700), ("Support", 2700),
                ("Customer Success", 2500), ("Engineering", 2500), ("Finance", 2200),
                ("Marketing", 1600), ("Marketing", 1600),
                ("Human Resources", 1300), ("Human Resources", 1300), ("Human Resources", 1300)],
}
BOUNDARY_JOIN_DATES = ["2026-05-01", "2026-06-30", "2026-07-01", "2026-07-31", "2026-08-31", "2026-09-30",
                       "2026-10-09"]

# Boundary cases, expressed relative to the employee's own shift so they work for any shift.
#   in_spec : ("off", seconds_from_shift_start, microseconds) | ("abs", "HH:MM" same IST day)
#             | ("after_midnight", seconds after the next IST midnight)
#   out_spec: ("end", seconds_from_shift_end, microseconds) | ("dur", seconds_after_punch_in) | None (open)
DAY_CASES = [
    ("late_grace_exact_10m00s", ("off", 600, 0), ("end", 0, 0)),
    ("late_grace_plus_1s", ("off", 601, 0), ("end", 0, 0)),
    ("late_floor_45m59s", ("off", 2759, 0), ("end", 0, 0)),
    ("late_subsecond_trunc_to_grace", ("off", 600, 900000), ("end", 0, 0)),
    ("late_10m59s", ("off", 659, 0), ("end", 0, 0)),
    ("late_11m00s", ("off", 660, 0), ("end", 0, 0)),
    ("early_arrival", ("off", -1, 0), ("end", 0, 0)),
    ("ot_29m59s", ("off", 0, 0), ("end", 1799, 0)),
    ("ot_30m00s", ("off", 0, 0), ("end", 1800, 0)),
    ("ot_30m59s", ("off", 0, 0), ("end", 1859, 0)),
    ("ot_subsecond_trunc_29m59s", ("off", 0, 0), ("end", 1799, 900000)),
    ("ot_floor_61m59s", ("off", 0, 0), ("end", 3719, 0)),
    ("half_4.485_roundup_to_4.49", ("off", 0, 0), ("dur", 16146)),
    ("half_4.49", ("off", 0, 0), ("dur", 16164)),
    ("half_4.4947", ("off", 0, 0), ("dur", 16181)),
    ("half_4.495_rounds_to_4.50", ("off", 0, 0), ("dur", 16182)),
    ("half_4.4997_rounds_to_4.50", ("off", 0, 0), ("dur", 16199)),
    ("half_exact_4.50", ("off", 0, 0), ("dur", 16200)),
    ("half_4.5028", ("off", 0, 0), ("dur", 16201)),
    ("round_halfup_8.125", ("off", 0, 0), ("dur", 29250)),
    ("round_halfup_8.135", ("off", 0, 0), ("dur", 29286)),
    ("round_halfup_8.145", ("off", 0, 0), ("dur", 29322)),
    ("duration_exactly_24h", ("off", 0, 0), ("dur", 86400)),
    ("open_late", ("off", 1200, 0), None),
    ("ist_vs_utc_date_boundary", ("abs", "02:10"), ("dur", 34200)),
]
# (label, in_spec, out_spec, attendance-date shift in days relative to the chosen start day)
NIGHT_CASES = [
    ("night_in_21_55_ot40", ("off", -300, 0), ("end", 2400, 0), 0),
    ("night_in_after_midnight_belongs_to_prev_day", ("after_midnight", 7200), ("end", 600, 0), 0),
    ("night_in_05_59_59_still_prev_day", ("after_midnight", 21599), ("dur", 21600), 0),
    ("night_in_exactly_shift_end_is_next_date", ("after_midnight", 21600), ("dur", 28800), 1),
    ("night_ot_exactly_30", ("off", 0, 0), ("end", 1800, 0), 0),
    ("night_open_record", ("off", -60, 0), None, 0),
]

BY = ["hr.admin", "hr.manager", "payroll.team", "ops.lead", "site.supervisor"]
REASONS = ["biometric glitch", "forgot to punch out", "manager approved correction", "device offline at gate",
           "client site visit approved", "system sync delay", "approved leave regularization", "shift swap approved"]

STATS = Counter()

ALL_DAYS = [WINDOW_START + timedelta(days=i) for i in range((WINDOW_END - WINDOW_START).days + 1)]
DAY_ISO = [d.isoformat() for d in ALL_DAYS]
DAY_MONTH = [d.strftime("%Y-%m") for d in ALL_DAYS]
WEEKDAY_LIST = [d for d in ALL_DAYS if d.weekday() < 5]


# --------------------------------------------------------------------------------------
# Rule engine (R1 - R5). Instants are aware datetimes; everything is truncated to whole seconds first.
# --------------------------------------------------------------------------------------
def trunc(dt):
    if dt is None:
        return None
    if dt.microsecond:
        STATS["subsecond_instants_truncated"] += 1
    return dt.replace(microsecond=0)


def to_utc(dt):
    return None if dt is None else dt.astimezone(UTC)


def hm(s: str) -> int:
    return int(s[:2]) * 60 + int(s[3:5])


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return hm(shift_end) <= hm(shift_start)


def ist_dt(d: date, hhmm: str) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=IST) + timedelta(minutes=hm(hhmm))


def shift_start_dt(d: date, emp) -> datetime:
    return ist_dt(d, emp["shift_start"])


def shift_end_dt(d: date, emp) -> datetime:
    end = ist_dt(d, emp["shift_end"])
    return end + timedelta(days=1) if is_overnight(emp["shift_start"], emp["shift_end"]) else end


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    """R1: IST calendar date of punch_in; overnight punch-ins earlier than shift_end belong to the previous day."""
    local = punch_in.astimezone(IST)
    d = local.date()
    if is_overnight(shift_start, shift_end):
        if local.hour * 3600 + local.minute * 60 + local.second < hm(shift_end) * 60:
            d -= timedelta(days=1)
    return d


def calc_late(punch_in, emp, d) -> int:
    """R2: late only if strictly more than 10:00 after shift_start; then whole minutes since shift_start."""
    delta = int((punch_in - shift_start_dt(d, emp)).total_seconds())
    return delta // 60 if delta > GRACE_SECONDS else 0


def calc_overtime(punch_out, emp, d) -> int:
    """R3: whole minutes between shift_end and punch_out, only when >= 30."""
    delta = int((punch_out - shift_end_dt(d, emp)).total_seconds())
    mins = delta // 60  # floor; negative (early leave) stays negative and fails the >= 30 test
    return mins if mins >= OVERTIME_MIN else 0


def calc_work_hours(punch_in, punch_out) -> Decimal:
    """R4: seconds / 3600 rounded to 2 decimals, half-up, using exact Decimal arithmetic."""
    secs = int((punch_out - punch_in).total_seconds())
    return (Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def derive(emp, d, status, punch_in, punch_out) -> dict:
    if status in PRESENCE:
        late = calc_late(punch_in, emp, d)
        if punch_out is None:
            return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
        wh = calc_work_hours(punch_in, punch_out)  # rounded Decimal
        return {"work_hours": float(wh), "late_minutes": late,
                "overtime_minutes": calc_overtime(punch_out, emp, d),
                "half_day": bool(wh < HALF_DAY_THRESHOLD)}  # R5: ROUNDED value vs 4.50
    return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}


def make_record(emp, status, punch_in, punch_out, d=None) -> dict:
    punch_in, punch_out = trunc(punch_in), trunc(punch_out)
    if status in PRESENCE:
        if punch_in is None:
            raise ValueError("presence status needs punch_in")
        calc = attendance_date(punch_in, emp["shift_start"], emp["shift_end"])
        if d is not None and d != calc:
            raise ValueError(f"intended date {d} != R1 date {calc} for {emp['emp_code']}")
        d = calc
        if punch_out is not None and not (punch_in < punch_out <= punch_in + timedelta(hours=24)):
            raise ValueError("punch_out must be after punch_in and within 24h")
    elif d is None or punch_in is not None or punch_out is not None:
        raise ValueError("ABSENT/LEAVE: date required, punches must be null")
    derived = derive(emp, d, status, punch_in, punch_out)
    return {
        "emp_code": emp["emp_code"], "date": d.isoformat(), "status": status,
        "punch_in": to_utc(punch_in), "punch_out": to_utc(punch_out),
        "work_hours": derived["work_hours"], "late_minutes": derived["late_minutes"],
        "overtime_minutes": derived["overtime_minutes"], "half_day": derived["half_day"], "history": [],
    }


# --------------------------------------------------------------------------------------
# Employee generation and role assignment
# --------------------------------------------------------------------------------------
def build_employees(rng, n):
    n_lower = LOWERCASE_DEPT[1]
    base_n = n - n_lower
    total_w = sum(w for _, w, _ in DEPARTMENTS)
    exact = [(name, base_n * w / total_w) for name, w, _ in DEPARTMENTS]
    counts = {name: int(x) for name, x in exact}
    leftover = base_n - sum(counts.values())
    order = sorted(range(len(exact)), key=lambda i: (-(exact[i][1] - int(exact[i][1])), i))
    for i in order[:leftover]:
        counts[exact[i][0]] += 1
    dept_list = []
    for name, _, _ in DEPARTMENTS:
        dept_list += [name] * counts[name]
    dept_list += [LOWERCASE_DEPT[0]] * n_lower
    rng.shuffle(dept_list)
    p_night = {name: p for name, _, p in DEPARTMENTS}
    old_span = (date(2026, 4, 30) - date(2022, 1, 3)).days
    emps = []
    for i, dept in enumerate(dept_list, start=1):
        first, last = rng.choice(FIRST), rng.choice(LAST)
        pool = NIGHT_SHIFTS if rng.random() < p_night.get(dept, 0.0) else DAY_SHIFTS
        ss, se, _w = rng.choices(pool, weights=[p[2] for p in pool])[0]
        emps.append({
            "emp_code": f"EMP{i:04d}", "name": f"{first} {last}",
            "email": f"{first.lower()}.{last.lower()}.{i}@example.com",
            "department": dept, "shift_start": ss, "shift_end": se,
            "joined_on": (date(2022, 1, 3) + timedelta(days=rng.randint(0, old_span))).isoformat(),
        })
    return emps


def assign_roles(rng, emps):
    roles, late_rate = {}, {}
    for e in emps:
        r = rng.random()
        late_rate[e["emp_code"]] = 0.10 if r < 0.70 else (0.25 if r < 0.95 else 0.45)
    free = list(range(len(emps)))

    def take(k, pred=None):
        nonlocal free
        cands = [i for i in free if pred is None or pred(emps[i])]
        if len(cands) < k:
            raise SystemExit(f"ERROR: cannot assign {k} employees for a scenario (only {len(cands)} available); "
                             f"use more --employees.")
        chosen = rng.sample(cands, k)
        chosen_set = set(chosen)
        free = [i for i in free if i not in chosen_set]
        return chosen

    tie_plan = {}
    for month in sorted(TIE_SPECS):
        tie_plan[month] = []
        for dept, total in TIE_SPECS[month]:
            (i,) = take(1, lambda e, dept=dept: e["department"] == dept)
            e = emps[i]
            e["shift_start"], e["shift_end"] = "09:30", "18:30"
            roles[e["emp_code"]] = "tie"
            tie_plan[month].append((e["emp_code"], dept, total))

    for i in take(15):  # future joiners: joined_on after TODAY -> no logs
        e = emps[i]
        e["joined_on"] = (date(2026, 10, 13) + timedelta(days=rng.randint(0, 80))).isoformat()
        roles[e["emp_code"]] = "future"
    nolog = take(25)  # employees that will have zero logs
    for pos, i in enumerate(nolog):
        e = emps[i]
        if pos >= 20:  # five of them joined inside the window but never logged anything
            e["joined_on"] = (date(2026, 5, 4) + timedelta(days=rng.randint(0, 140))).isoformat()
        roles[e["emp_code"]] = "nolog"
    for i in take(60):  # mid-month joiners
        e = emps[i]
        e["joined_on"] = date(2026, rng.choice([5, 6, 7, 8, 9]), rng.randint(8, 24)).isoformat()
        roles[e["emp_code"]] = "joiner"
    for i, jd in zip(take(len(BOUNDARY_JOIN_DATES)), BOUNDARY_JOIN_DATES):  # joins on month edges / window edges
        emps[i]["joined_on"] = jd
        roles[emps[i]["emp_code"]] = "joiner"

    case_day = take(30, lambda e: not is_overnight(e["shift_start"], e["shift_end"]))
    case_night = take(3, lambda e: is_overnight(e["shift_start"], e["shift_end"]))
    for i in case_night:
        emps[i]["shift_start"], emps[i]["shift_end"] = "22:00", "06:00"
    for i in case_day + case_night:
        roles[emps[i]["emp_code"]] = "case"
    return (roles, tie_plan, late_rate,
            [emps[i]["emp_code"] for i in case_day], [emps[i]["emp_code"] for i in case_night])


def make_created_at(rng, joined: date):
    if joined <= TODAY:
        base = datetime(joined.year, joined.month, joined.day, 4, 30, tzinfo=UTC)
        return base + timedelta(minutes=rng.randint(0, 59), seconds=rng.randint(0, 59))
    return datetime(2026, 9, 15, 4, 30, tzinfo=UTC) + timedelta(days=rng.randint(0, 25), minutes=rng.randint(0, 59))


# --------------------------------------------------------------------------------------
# Attendance generation
# --------------------------------------------------------------------------------------
def weekdays_in_month(ym: str):
    y, m = int(ym[:4]), int(ym[5:])
    out = []
    for k in range(1, calendar.monthrange(y, m)[1] + 1):
        d = date(y, m, k)
        if d.weekday() < 5 and WINDOW_START <= d <= WINDOW_END:
            out.append(d)
    return out


def split_total(rng, total, n, lo=11, hi=300):
    parts = [total // n] * n
    for i in range(total % n):
        parts[i] += 1
    assert all(lo <= p <= hi for p in parts), (total, n, parts[:3])
    for _ in range(n * 8):
        i, j = rng.randrange(n), rng.randrange(n)
        if i == j:
            continue
        room = min(parts[i] - lo, hi - parts[j])
        if room > 0:
            t = rng.randint(0, room)
            parts[i] -= t
            parts[j] += t
    return parts


def gen_punch_out(rng, emp, d, pi):
    end = shift_end_dt(d, emp)
    micro = rng.randint(0, 999999)
    r = rng.random()
    if r < 0.04:    # short day -> half day territory
        po = pi + timedelta(seconds=rng.randint(7200, 16260), microseconds=micro)
    elif r < 0.14:  # early leave
        po = end + timedelta(seconds=rng.randint(-14400, -1801), microseconds=micro)
    elif r < 0.40:  # overtime (30 min .. 3 h after shift end)
        po = end + timedelta(seconds=rng.randint(1800, 10800), microseconds=micro)
    else:           # around shift end (overtime stays 0)
        po = end + timedelta(seconds=rng.randint(-1800, 1799), microseconds=micro)
    tp, to = trunc_quiet(pi), trunc_quiet(po)
    if not (tp + timedelta(seconds=60) <= to <= tp + timedelta(hours=24)):
        po = pi + timedelta(hours=8)
    return po


def trunc_quiet(dt):
    return dt.replace(microsecond=0)


def gen_punches(rng, emp, d, late_rate, p_open):
    start = shift_start_dt(d, emp)
    if rng.random() < late_rate:
        off = rng.randint(601, 1800) if rng.random() < 0.7 else rng.randint(1801, 4800)
    else:
        off = rng.randint(-2400, 600)
    pi = start + timedelta(seconds=off, microseconds=rng.randint(0, 999999))
    if rng.random() < p_open:
        return pi, None
    return pi, gen_punch_out(rng, emp, d, pi)


def gen_day_record(rng, emp, d, weekend, late_rate):
    if weekend:
        status = rng.choices(PRESENCE, weights=[70, 20, 10])[0]
    else:
        status = rng.choices(ALL_STATUSES, weights=[78, 8, 4, 4, 6])[0]
    if status not in PRESENCE:
        return make_record(emp, status, None, None, d)
    p_open = 0.08 if d >= date(2026, 10, 8) else 0.004
    pi, po = gen_punches(rng, emp, d, late_rate, p_open)
    return make_record(emp, status, pi, po, d)


def case_punches(emp, d, in_spec, out_spec):
    start, end = shift_start_dt(d, emp), shift_end_dt(d, emp)
    if in_spec[0] == "off":
        pi = start + timedelta(seconds=in_spec[1], microseconds=in_spec[2])
    elif in_spec[0] == "abs":
        pi = ist_dt(d, in_spec[1])
    else:
        pi = ist_dt(d + timedelta(days=1), "00:00") + timedelta(seconds=in_spec[1])
    if out_spec is None:
        po = None
    elif out_spec[0] == "end":
        po = end + timedelta(seconds=out_spec[1], microseconds=out_spec[2])
    else:
        po = pi + timedelta(seconds=out_spec[1])
    return pi, po


# ---- regularization (history) -------------------------------------------------------
def propose(rng, emp, rec, late_rate):
    d = date.fromisoformat(rec["date"])
    status, pi, po = rec["status"], rec["punch_in"], rec["punch_out"]

    def ok_times(npi, npo):
        if attendance_date(npi, emp["shift_start"], emp["shift_end"]) != d:
            return False
        return npo is None or npi < npo <= npi + timedelta(hours=24)

    def jitter(max_min):
        return timedelta(minutes=rng.choice([-1, 1]) * rng.randint(1, max_min))

    if status in PRESENCE and po is not None:
        kind = rng.choices(["in", "out", "both", "status_presence", "to_absent", "to_leave"],
                           weights=[25, 25, 15, 10, 5, 5])[0]
    elif status in PRESENCE:
        kind = rng.choices(["close", "in", "to_leave"], weights=[60, 25, 15])[0]
    else:
        kind = rng.choices(["to_presence", "swap"], weights=[70, 30])[0]

    if kind in ("in", "both"):
        for _ in range(8):
            npi = pi + jitter(120)
            npo = po + jitter(180) if kind == "both" else po
            if ok_times(npi, npo):
                return kind, status, npi, npo
        return None
    if kind == "out":
        for _ in range(8):
            npo = po + jitter(180)
            if ok_times(pi, npo):
                return kind, status, pi, npo
        return None
    if kind == "close":
        npo = pi + timedelta(seconds=rng.randint(4 * 3600, 11 * 3600))
        return (kind, status, pi, npo) if ok_times(pi, npo) else None
    if kind == "status_presence":
        return kind, rng.choice([s for s in PRESENCE if s != status]), pi, po
    if kind == "to_absent":
        return kind, "ABSENT", None, None
    if kind == "to_leave":
        return kind, "LEAVE", None, None
    if kind == "swap":
        return kind, ("LEAVE" if status == "ABSENT" else "ABSENT"), None, None
    npi, npo = gen_punches(rng, emp, d, late_rate, 0.0)  # to_presence
    if npo is None or not ok_times(trunc_quiet(npi), trunc_quiet(npo)):
        return None
    return kind, rng.choices(PRESENCE, weights=[70, 20, 10])[0], npi, npo


def apply_correction(emp, rec, status, pi, po, by, reason, at) -> bool:
    d = date.fromisoformat(rec["date"])
    pi, po = to_utc(trunc(pi)), to_utc(trunc(po))
    new = {"status": status, "punch_in": pi, "punch_out": po, **derive(emp, d, status, pi, po)}
    changes = {k: {"from": rec[k], "to": new[k]} for k in TRACKED if rec[k] != new[k]}
    if not changes:
        return False
    rec.update(new)
    rec["history"].append({"at": at, "by": by, "reason": reason, "changes": changes})
    return True


def next_at(rng, rec, prev_at):
    if prev_at is None:
        d = date.fromisoformat(rec["date"])
        at = ist_dt(d, "18:00") + timedelta(days=rng.randint(1, 10), seconds=rng.randint(0, 36000))
        if at > NOW_UTC - timedelta(hours=1):
            at = NOW_UTC - timedelta(minutes=rng.randint(60, 600))
        return trunc_quiet(at.astimezone(UTC))
    at = prev_at + timedelta(hours=rng.randint(2, 120))
    if at > NOW_UTC - timedelta(minutes=30):
        return None
    return trunc_quiet(at)


def regularize(rng, records, emp_by_code, late_rate, blocked, frac=0.016):
    # Records of the engineered tie cohorts (tie employee x tie month) are never corrected: a correction would
    # change their late_minutes and break the exact monthly totals the leaderboard ties depend on.
    keys = [k for k in records if k[1][:7] not in blocked.get(k[0], ())]
    for key in rng.sample(keys, int(len(records) * frac)):
        rec = records[key]
        emp = emp_by_code[rec["emp_code"]]
        n_corr = 2 if rng.random() < 0.12 else 1
        at = None
        for _ in range(n_corr):
            prop = propose(rng, emp, rec, late_rate[emp["emp_code"]])
            if prop is None:
                break
            kind, status, npi, npo = prop
            at = next_at(rng, rec, at)
            if at is None:
                break
            if apply_correction(emp, rec, status, npi, npo, rng.choice(BY), rng.choice(REASONS), at):
                STATS["reg:" + kind] += 1


def make_legacy(rng, records, frac=0.02):
    keys = list(records.keys())
    for key in rng.sample(keys, int(len(keys) * frac)):
        rec = records[key]
        mode = rng.choices(["both", "history", "half_day"], weights=[50, 25, 25])[0]
        dropped = []
        if mode in ("both", "history") and rec["history"] == []:
            del rec["history"]
            dropped.append("history")
        if mode in ("both", "half_day") and rec["half_day"] is False:
            del rec["half_day"]
            dropped.append("half_day")
        if dropped:
            STATS["legacy:" + "+".join(dropped)] += 1


# ---- dataset orchestration ------------------------------------------------------------
def build_dataset(seed, n_emp, target):
    rng = random.Random(seed)
    STATS.clear()
    emps = build_employees(rng, n_emp)
    roles, tie_plan, late_rate, case_day, case_night = assign_roles(rng, emps)
    for e in emps:
        e["created_at"] = make_created_at(rng, date.fromisoformat(e["joined_on"]))
    emp_by_code = {e["emp_code"]: e for e in emps}
    loggable = [i for i, e in enumerate(emps) if roles.get(e["emp_code"]) not in ("future", "nolog")]

    blocked = defaultdict(tuple)  # employee -> months where only the engineered tie records may exist
    for month, plan in tie_plan.items():
        for code, _dept, _tot in plan:
            blocked[code] += (month,)

    records = {}

    def put(rec):
        key = (rec["emp_code"], rec["date"])
        if key in records:
            raise ValueError(f"duplicate key {key}")
        records[key] = rec

    # 1) engineered tie cohorts: every weekday of the month, exact monthly late totals
    for month in sorted(tie_plan):
        days = weekdays_in_month(month)
        for code, _dept, total in tie_plan[month]:
            emp = emp_by_code[code]
            n_late = rng.randint(14, len(days) - 2)
            late_idx = sorted(rng.sample(range(len(days)), n_late))
            late_map = dict(zip(late_idx, split_total(rng, total, n_late)))
            for k, d in enumerate(days):
                status = rng.choices(PRESENCE, weights=[8, 1, 1])[0]
                start = shift_start_dt(d, emp)
                off = late_map[k] * 60 + rng.randint(0, 59) if k in late_map else rng.randint(-2400, 600)
                pi = start + timedelta(seconds=off)
                put(make_record(emp, status, pi, gen_punch_out(rng, emp, d, pi), d))

    # 2) boundary cases
    case_dates = [d for d in ALL_DAYS if d.weekday() < 5 and date(2026, 6, 1) <= d <= date(2026, 9, 25)]

    def place_case(emp, label, in_spec, out_spec, shift=0):
        for _ in range(50):
            d = rng.choice(case_dates)
            if (emp["emp_code"], (d + timedelta(days=shift)).isoformat()) in records:
                continue
            pi, po = case_punches(emp, d, in_spec, out_spec)
            put(make_record(emp, "PRESENT", pi, po, d + timedelta(days=shift)))
            STATS["case:" + label] += 1
            return
        raise RuntimeError("could not place case " + label)

    for j, (label, in_spec, out_spec) in enumerate(DAY_CASES):
        place_case(emp_by_code[case_day[j % len(case_day)]], label, in_spec, out_spec)
    for j, (label, in_spec, out_spec, shift) in enumerate(NIGHT_CASES):
        place_case(emp_by_code[case_night[j % len(case_night)]], label, in_spec, out_spec, shift)

    # 3) candidate (employee, day) slots
    weekday_cands, weekend_cands = [], []
    for ei in loggable:
        e = emps[ei]
        code = e["emp_code"]
        bl = blocked.get(code, ())
        start_idx = bisect_left(ALL_DAYS, max(WINDOW_START, date.fromisoformat(e["joined_on"])))
        for di in range(start_idx, len(ALL_DAYS)):
            if DAY_MONTH[di] in bl or (code, DAY_ISO[di]) in records:
                continue
            (weekday_cands if ALL_DAYS[di].weekday() < 5 else weekend_cands).append((ei, di))

    n_weekend = min(len(weekend_cands), int(round(0.012 * target)))
    sel_weekend = sorted(rng.sample(weekend_cands, n_weekend))
    k_weekday = min(len(weekday_cands), max(0, target - len(records) - n_weekend))
    sel_weekday = sorted(rng.sample(weekday_cands, k_weekday))
    STATS["weekday_slots_available"] = len(weekday_cands)
    for weekend, sel in ((True, sel_weekend), (False, sel_weekday)):
        for ei, di in sel:
            e = emps[ei]
            put(gen_day_record(rng, e, ALL_DAYS[di], weekend, late_rate[e["emp_code"]]))

    # 4) manual corrections (history) and legacy documents
    regularize(rng, records, emp_by_code, late_rate, blocked)
    make_legacy(rng, records)

    logs = sorted(records.values(), key=lambda r: (r["date"], r["emp_code"]))
    emps.sort(key=lambda e: e["emp_code"])
    return emps, logs, roles, tie_plan


# --------------------------------------------------------------------------------------
# Validation (independent re-derivation using epoch integers instead of the generator's datetime math)
# --------------------------------------------------------------------------------------
EMP_RE = re.compile(r"^EMP\d{4,6}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SHIFT_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
EMP_KEYS = {"emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on", "created_at"}
LOG_BASE = {"emp_code", "date", "status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes"}
LOG_ALL = LOG_BASE | {"half_day", "history"}
EPOCH_ORD = date(1970, 1, 1).toordinal()


class Checker:
    def __init__(self):
        self.results = {}

    def check(self, name, cond, detail=None):
        r = self.results.setdefault(name, [0, 0, []])
        if cond:
            r[0] += 1
        else:
            r[1] += 1
            if len(r[2]) < 3:
                r[2].append(detail)

    @property
    def failed(self):
        return {k: v for k, v in self.results.items() if v[1]}


def is_utc_dt(x):
    return isinstance(x, datetime) and x.tzinfo is not None and x.utcoffset() == timedelta(0) and x.microsecond == 0


def v_epoch(dt):
    return calendar.timegm(dt.utctimetuple())


def v_local(epoch_s):
    day, sod = divmod(epoch_s + 19800, 86400)
    return date.fromordinal(EPOCH_ORD + day), sod


def v_local_epoch(d, minutes, extra_days=0):
    return (d.toordinal() - EPOCH_ORD + extra_days) * 86400 + minutes * 60 - 19800


def competition_ranks(rows):
    ranks, prev, rank = [], None, 0
    for i, (_code, tot) in enumerate(rows, 1):
        if tot != prev:
            rank, prev = i, tot
        ranks.append(rank)
    return ranks


def analyze_ties(logs, emp_by_code, tie_plan, chk):
    totals = defaultdict(int)
    for r in logs:
        if r["late_minutes"]:
            totals[(r["date"][:7], r["emp_code"])] += r["late_minutes"]
    lines = []
    for month in sorted(tie_plan):
        plan = tie_plan[month]
        rows = sorted(((c, t) for (m, c), t in totals.items() if m == month and t > 0), key=lambda x: (-x[1], x[0]))
        ranks = competition_ranks(rows)
        n = len(plan)
        chk.check(f"ties {month}: engineered cohort occupies the top {n} rows",
                  [t for _, t in rows[:n]] == sorted((t for _, _, t in plan), reverse=True), month)
        chk.check(f"ties {month}: each cohort member total equals its planned total",
                  all(totals[(month, c)] == t for c, _, t in plan), month)
        top = ", ".join(f"#{rk} {c}({emp_by_code[c]['department']}) {t}" for (c, t), rk in zip(rows[:n], ranks[:n]))
        lines.append(f"{month} GLOBAL top {n} (rank code(dept) total_late_min): {top}")
        by_total = defaultdict(list)
        for c, t in rows[:n]:
            by_total[t].append(emp_by_code[c]["department"])
        cross = [f"{t} min shared across {sorted(set(ds))}" for t, ds in sorted(by_total.items(), reverse=True)
                 if len(ds) > 1 and len(set(ds)) > 1]
        if cross:
            lines.append(f"{month} cross-department ties: " + "; ".join(cross))
        for lim in (2, 3):
            lines.append(f"{month} limit={lim} would return {sum(1 for rk in ranks if rk <= lim)} rows "
                         f"(every row with rank <= {lim})")
        for dept in sorted({d for _, d, _ in plan}):
            members = [t for _, d, t in plan if d == dept]
            drows = [(c, t) for c, t in rows if emp_by_code[c]["department"] == dept]
            dranks = competition_ranks(drows)
            chk.check(f"ties {month}: {dept} cohort is the top of its department",
                      [t for _, t in drows[:len(members)]] == sorted(members, reverse=True), (month, dept))
            if len(members) > 1:
                lines.append(f"{month} {dept}-only ranking: "
                             + ", ".join(f"#{rk} {c} {t}" for (c, t), rk in zip(drows[:len(members)], dranks)))
    return lines


def validate(emps, logs, roles, tie_plan, target):
    chk, cov = Checker(), Counter()
    emp_by_code = {e["emp_code"]: e for e in emps}
    chk.check("employees: unique emp_code", len(emp_by_code) == len(emps))
    for e in emps:
        k = e["emp_code"]
        chk.check("employees: exact field set", set(e) == EMP_KEYS, k)
        chk.check("employees: emp_code matches ^EMP\\d{4,6}$", EMP_RE.fullmatch(k) is not None, k)
        chk.check("employees: name length 1-100", 1 <= len(e["name"]) <= 100, k)
        chk.check("employees: email pattern and length <= 120",
                  EMAIL_RE.fullmatch(e["email"]) is not None and len(e["email"]) <= 120, k)
        chk.check("employees: department length 1-50", 1 <= len(e["department"]) <= 50, k)
        chk.check("employees: shift strings HH:MM", bool(SHIFT_RE.match(e["shift_start"]) and SHIFT_RE.match(e["shift_end"])), k)
        chk.check("employees: shift_start != shift_end", e["shift_start"] != e["shift_end"], k)
        chk.check("employees: joined_on is a valid YYYY-MM-DD string",
                  bool(DATE_RE.match(e["joined_on"])) and date.fromisoformat(e["joined_on"]) is not None, k)
        chk.check("employees: created_at is tz-aware UTC datetime, whole seconds, not in the future",
                  is_utc_dt(e["created_at"]) and e["created_at"] <= NOW_UTC, k)
    chk.check("employees: unique emails", len({e["email"] for e in emps}) == len(emps))

    seen, logs_per_emp, weekday_docs = set(), Counter(), Counter()
    first_log = {}
    for r in logs:
        code = r["emp_code"]
        key = (code, r["date"])
        e = emp_by_code.get(code)
        chk.check("logs: employee exists", e is not None, key)
        if e is None:
            continue
        chk.check("logs: unique (emp_code, date)", key not in seen, key)
        seen.add(key)
        keys = set(r)
        chk.check("logs: required fields present, no unknown fields", LOG_BASE <= keys <= LOG_ALL, key)
        chk.check("logs: date format YYYY-MM-DD", bool(DATE_RE.match(r["date"])), key)
        d = date.fromisoformat(r["date"])
        chk.check("logs: date inside generation window", WINDOW_START <= d <= WINDOW_END, key)
        chk.check("logs: never before employee joined_on", r["date"] >= e["joined_on"], key)
        chk.check("logs: status is one of the 5 values", r["status"] in ALL_STATUSES, key)
        chk.check("logs: derived field types", type(r["late_minutes"]) is int and type(r["overtime_minutes"]) is int
                  and (r.get("half_day", False) in (True, False))
                  and (r["work_hours"] is None or type(r["work_hours"]) is float), key)
        logs_per_emp[code] += 1
        first_log.setdefault(code, r["date"])
        if d.weekday() < 5:
            weekday_docs[code] += 1
        else:
            cov["weekend_record"] += 1
        cov["status_" + r["status"]] += 1
        pi, po = r["punch_in"], r["punch_out"]
        ss, se = hm(e["shift_start"]), hm(e["shift_end"])
        night = se <= ss
        half_stored = r.get("half_day", False)
        if "half_day" not in r:
            cov["legacy_missing_half_day"] += 1
        if "history" not in r:
            cov["legacy_missing_history"] += 1
        if "half_day" not in r and "history" not in r:
            cov["legacy_missing_both"] += 1
        if r["status"] in ("ABSENT", "LEAVE"):
            chk.check("ABSENT/LEAVE: punches null, work_hours null, late/ot 0, half_day false",
                      pi is None and po is None and r["work_hours"] is None and r["late_minutes"] == 0
                      and r["overtime_minutes"] == 0 and half_stored is False, key)
        else:
            chk.check("presence: punch_in is tz-aware UTC datetime, whole seconds", is_utc_dt(pi), key)
            if not is_utc_dt(pi):
                continue
            chk.check("presence: punch_out null or valid UTC datetime", po is None or is_utc_dt(po), key)
            pin = v_epoch(pi)
            local_d, sod = v_local(pin)
            exp_date = local_d - timedelta(days=1) if (night and sod < se * 60) else local_d
            chk.check("R1: stored date equals attendance date of punch_in", exp_date == d, key)
            start = v_local_epoch(d, ss)
            delta = pin - start
            exp_late = delta // 60 if delta > 600 else 0
            chk.check("R2: late_minutes matches independent re-derivation", r["late_minutes"] == exp_late, key)
            chk.check("presence: punch_in <= NOW", pi <= NOW_UTC, key)
            if night:
                cov["overnight_record"] += 1
                if sod < se * 60:
                    cov["night_punch_in_after_midnight"] += 1
                if sod == se * 60:
                    cov["night_punch_in_exactly_shift_end"] += 1
            else:
                cov["standard_record"] += 1
            if local_d != pi.date():
                cov["utc_date_differs_from_ist_date"] += 1
            if exp_late > 0:
                cov["late_arrival"] += 1
            if delta == 600:
                cov["grace_exact_600s_not_late"] += 1
            if delta == 601:
                cov["grace_601s_is_10_min"] += 1
            if po is None:
                cov["open_record"] += 1
                chk.check("open record: work_hours null, overtime 0, half_day false",
                          r["work_hours"] is None and r["overtime_minutes"] == 0 and half_stored is False, key)
            else:
                pout = v_epoch(po)
                dur = pout - pin
                chk.check("punch_out strictly after punch_in and within 24h", 0 < dur <= 86400, key)
                hundredths = (200 * dur + 3600) // 7200  # integer half-up of dur/36 (hundredths of an hour)
                wh = r["work_hours"]
                chk.check("R4: work_hours = half-up 2dp of seconds/3600",
                          wh is not None and round(wh * 100) == hundredths and abs(wh - hundredths / 100) < 1e-9, key)
                end = v_local_epoch(d, se, 1 if night else 0)
                ot = (pout - end) // 60
                ot = ot if ot >= 30 else 0
                chk.check("R3: overtime_minutes matches independent re-derivation", r["overtime_minutes"] == ot, key)
                exp_half = hundredths < 450
                chk.check("R5: half_day = ROUNDED work_hours < 4.50", half_stored == exp_half, key)
                if night:
                    cov["overnight_closed"] += 1
                    if v_local(pout)[0] > d:
                        cov["overnight_punch_out_next_day"] += 1
                else:
                    cov["standard_closed"] += 1
                if ot > 0:
                    cov["overtime"] += 1
                if pout - end == 1800:
                    cov["overtime_exactly_30m"] += 1
                if pout - end == 1799:
                    cov["overtime_29m59s_is_zero"] += 1
                if exp_half:
                    cov["half_day_true"] += 1
                if hundredths == 449 and exp_half:
                    cov["half_boundary_4.49_true"] += 1
                if hundredths == 450 and not exp_half:
                    cov["half_boundary_4.50_false"] += 1
                if 16182 <= dur <= 16199:
                    cov["half_raw_below_4.50_rounds_to_4.50"] += 1
                if (dur * 100) % 3600 == 1800:
                    cov["work_hours_exact_half_cent_tie"] += 1

        # history
        hist = r.get("history", [])
        chk.check("history is a list", isinstance(hist, list), key)
        if hist:
            cov["history_records"] += 1
            if len(hist) > 1:
                cov["history_multi_entry"] += 1
            prev_at = None
            for h in hist:
                ok = (set(h) == {"at", "by", "reason", "changes"} and is_utc_dt(h["at"]) and h["at"] <= NOW_UTC
                      and isinstance(h["by"], str) and 1 <= len(h["by"]) <= 50
                      and isinstance(h["reason"], str) and 5 <= len(h["reason"]) <= 200
                      and isinstance(h["changes"], dict) and len(h["changes"]) > 0
                      and (prev_at is None or h["at"] > prev_at))
                chk.check("history entry shape (at/by/reason/changes), ordered", ok, key)
                prev_at = h["at"] if is_utc_dt(h["at"]) else prev_at
                for fld, ch in h["changes"].items():
                    cov["history_change_" + fld] += 1
                    chk.check("history changes: only tracked fields, {from,to}, from != to",
                              fld in TRACKED and set(ch) == {"from", "to"} and ch["from"] != ch["to"], key)
                    if fld in ("punch_in", "punch_out"):
                        chk.check("history changes: punch values are BSON-date datetimes or null",
                                  all(v is None or is_utc_dt(v) for v in (ch["from"], ch["to"])), key)
            last = hist[-1]["changes"]
            chk.check("history: last entry 'to' values equal the stored record",
                      all(r[f] == v["to"] for f, v in last.items() if f in r), key)
            for i in range(1, len(hist)):
                chk.check("history: entry chain is consistent (to -> next from)",
                          all(hist[i - 1]["changes"][f]["to"] == v["from"]
                              for f, v in hist[i]["changes"].items() if f in hist[i - 1]["changes"]), key)
            if "status" in last and last["status"]["to"] in ("ABSENT", "LEAVE"):
                cov["history_correction_to_absent_leave"] += 1
            if "status" in last and last["status"]["from"] in ("ABSENT", "LEAVE"):
                cov["history_correction_from_absent_leave"] += 1

    # employee-level coverage
    missing_weekday_slots = 0
    for e in emps:
        code, joined = e["emp_code"], date.fromisoformat(e["joined_on"])
        role = roles.get(code)
        if logs_per_emp[code] == 0:
            cov["employees_with_no_logs"] += 1
            if joined > TODAY:
                cov["future_joiners_no_logs"] += 1
        else:
            slots = len(WEEKDAY_LIST) - bisect_left(WEEKDAY_LIST, max(WINDOW_START, joined))
            chk.check("weekday docs never exceed available weekday slots", weekday_docs[code] <= slots, code)
            missing_weekday_slots += slots - weekday_docs[code]
            if WINDOW_START < joined <= WINDOW_END and joined.day > 1:
                cov["mid_month_joiners_with_logs"] += 1
        if role in ("future", "nolog"):
            chk.check("no-log / future employees really have zero logs", logs_per_emp[code] == 0, code)
        if joined > TODAY:
            cov["future_join_dates"] += 1
        chk.check("employees: shift_start != shift_end (re-check)", e["shift_start"] != e["shift_end"], code)
    cov["weekdays_without_a_document"] = missing_weekday_slots

    tie_lines = analyze_ties(logs, emp_by_code, tie_plan, chk)

    must_have = ["status_PRESENT", "status_WFH", "status_ON_DUTY", "status_ABSENT", "status_LEAVE",
                 "standard_closed", "overnight_closed", "overnight_punch_out_next_day", "late_arrival", "overtime",
                 "overtime_exactly_30m", "overtime_29m59s_is_zero", "grace_exact_600s_not_late",
                 "grace_601s_is_10_min", "half_day_true", "half_boundary_4.49_true", "half_boundary_4.50_false",
                 "half_raw_below_4.50_rounds_to_4.50", "work_hours_exact_half_cent_tie", "open_record",
                 "weekend_record", "weekdays_without_a_document", "employees_with_no_logs",
                 "mid_month_joiners_with_logs", "future_join_dates", "future_joiners_no_logs", "history_records",
                 "history_multi_entry", "history_change_status", "history_change_punch_in",
                 "history_change_punch_out", "history_change_work_hours", "history_change_late_minutes",
                 "history_correction_to_absent_leave", "history_correction_from_absent_leave",
                 "legacy_missing_half_day", "legacy_missing_history", "legacy_missing_both",
                 "utc_date_differs_from_ist_date", "night_punch_in_after_midnight",
                 "night_punch_in_exactly_shift_end"]
    for name in must_have:
        chk.check(f"coverage: at least one '{name}'", cov[name] > 0, name)
    chk.check("record count within 2% of target", abs(len(logs) - target) <= 0.02 * target, (len(logs), target))
    chk.check("final emp_code x (emp_code,date) uniqueness", len(seen) == len(logs))
    return chk, cov, tie_lines


# --------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------
def fingerprint(emps, logs):
    h = hashlib.sha256()

    def enc(o):
        if isinstance(o, datetime):
            return o.isoformat()
        raise TypeError(type(o))

    for doc in emps + logs:
        h.update(json.dumps(doc, default=enc).encode())
    return h.hexdigest()[:16]


def print_report(args, emps, logs, roles, chk, cov, tie_lines, elapsed):
    line = "=" * 78
    print(line)
    print(f"DRY RUN - MongoDB was NOT contacted (target would be '{TARGET_DB}'; protected: '{PROTECTED_DB}')")
    print(line)
    print(f"seed={args.seed}  target_records={args.target_records}  generated_in={elapsed:.1f}s  "
          f"fingerprint={fingerprint(emps, logs)}")
    print(f"window: {WINDOW_START} .. {WINDOW_END}   'today' used for future joins: {TODAY}")
    print("\n[1] Employee count:", len(emps))
    print("[2] Attendance record count:", len(logs))
    print("\n[3] Status distribution")
    sc = Counter(r["status"] for r in logs)
    for s in ALL_STATUSES:
        print(f"    {s:<8} {sc[s]:>7}  {100 * sc[s] / len(logs):5.1f}%")
    print("\n[4] Department distribution (employees / attendance records)")
    ec = Counter(e["department"] for e in emps)
    lc = Counter()
    emp_idx = {e["emp_code"]: e for e in emps}
    for r in logs:
        lc[emp_idx[r["emp_code"]]["department"]] += 1
    for dept in sorted(ec):
        print(f"    {dept:<18} {ec[dept]:>5} employees {lc[dept]:>8} records")
    night_codes = {e["emp_code"] for e in emps if is_overnight(e["shift_start"], e["shift_end"])}
    print("\n[5] Overnight shifts")
    print(f"    overnight employees: {len(night_codes)} ({100 * len(night_codes) / len(emps):.1f}%)"
          f"   overnight records: {sum(1 for r in logs if r['emp_code'] in night_codes)}")
    shifts = Counter(f"{e['shift_start']}-{e['shift_end']}" for e in emps)
    print("    shift mix: " + ", ".join(f"{k}:{v}" for k, v in sorted(shifts.items())))
    print("\n[6] Open records (punch_out null, presence status):", cov["open_record"])
    nolog_total = cov["employees_with_no_logs"]
    print(f"\n[7] No-log employees: {nolog_total}  "
          f"(future joiners: {cov['future_joiners_no_logs']}, joined <= today with no logs: "
          f"{nolog_total - cov['future_joiners_no_logs']})")
    print("\n[8] Tie scenarios")
    for tl in tie_lines:
        print("    " + tl)
    print("\n[9] Validation results")
    total_checks = sum(v[0] + v[1] for v in chk.results.values())
    for name, (ok, bad, samples) in chk.results.items():
        print(f"    {'PASS' if not bad else 'FAIL'}  {name}  (ok={ok}" + (f", failed={bad}, e.g. {samples}" if bad else "") + ")")
    print(f"    -> {len(chk.results)} checks, {total_checks} assertions, {len(chk.failed)} failing checks")
    print("\n[extra] Other composition")
    mc = Counter(r["date"][:7] for r in logs)
    print("    records per month: " + ", ".join(f"{k}:{v}" for k, v in sorted(mc.items())))
    print(f"    weekend records: {cov['weekend_record']}   weekdays with no document (employee-days): "
          f"{cov['weekdays_without_a_document']}")
    print(f"    mid-month joiners with logs: {cov['mid_month_joiners_with_logs']}   future join dates: "
          f"{cov['future_join_dates']}   late arrivals: {cov['late_arrival']}   overtime records: {cov['overtime']}"
          f"   half days: {cov['half_day_true']}")
    print(f"    records with history: {cov['history_records']} (multi-entry {cov['history_multi_entry']})   "
          f"legacy: missing half_day {cov['legacy_missing_half_day']}, missing history "
          f"{cov['legacy_missing_history']}, missing both {cov['legacy_missing_both']}")
    print("    correction kinds: " + ", ".join(f"{k[4:]}:{v}" for k, v in sorted(STATS.items()) if k.startswith("reg:")))
    print(f"    boundary cases injected: {sum(v for k, v in STATS.items() if k.startswith('case:'))}"
          f"   sub-second instants truncated before storage: {STATS['subsecond_instants_truncated']}")
    print(f"    punch-in UTC date != IST date: {cov['utc_date_differs_from_ist_date']}   "
          f"night punch-ins after midnight: {cov['night_punch_in_after_midnight']}")


# --------------------------------------------------------------------------------------
# MongoDB writer (only reached with --write AND the confirmation flag)
# --------------------------------------------------------------------------------------
def die(msg, code=1):
    print("ERROR: " + msg, file=sys.stderr)
    raise SystemExit(code)


def get_mongo_uri():
    uri = os.environ.get("MONGO_URI")
    if not uri:
        try:
            from dotenv import dotenv_values  # reads only the file; never touches os.environ
            uri = dotenv_values(".env").get("MONGO_URI")
        except ImportError:
            uri = None
    if not uri:
        die("MONGO_URI is not set (environment variable or MONGO_URI in .env). Nothing was written.")
    return uri


def write_to_mongo(emps, logs, skip_indexes, batch_size):
    from pymongo import ASCENDING, DESCENDING, MongoClient
    # from pymongo.codec_options import CodecOptions
    from bson.codec_options import CodecOptions 
    from pymongo.errors import BulkWriteError

    if TARGET_DB == PROTECTED_DB:
        die("TARGET_DB equals the protected database name; refusing.")
    if os.environ.get("MONGO_DB") == PROTECTED_DB:
        print(f"NOTE: MONGO_DB={PROTECTED_DB} is set in the environment. It is ignored; this script only ever uses "
              f"'{TARGET_DB}'.")
    uri = get_mongo_uri()
    client = MongoClient(uri, tz_aware=True, serverSelectionTimeoutMS=10_000)
    client.admin.command("ping")
    try:
        from urllib.parse import urlsplit
        host = urlsplit(uri).hostname or "<host>"
    except Exception:
        host = "<host>"
    db = client[TARGET_DB]

    def guard():
        if db.name != TARGET_DB or db.name == PROTECTED_DB:
            die("database guard tripped; aborting before any write.")

    guard()
    print(f"Connected to {host}; target database '{db.name}'")
    codec = CodecOptions(tz_aware=True, tzinfo=UTC)
    emp_col = db.get_collection("employees", codec_options=codec)
    log_col = db.get_collection("attendance_logs", codec_options=codec)
    for col in (emp_col, log_col):
        if col.count_documents({}, limit=1) > 0:
            die(f"'{db.name}.{col.name}' already has documents. This script never deletes or appends; "
                f"clear the TEST collections yourself if you want to regenerate. Nothing was written.")

    if skip_indexes:
        print("--skip-indexes: NOT creating indexes (the application's own startup must create them).")
    else:
        guard()
        # Default index names on purpose, so the app's own create_index calls on the same keys do not conflict.
        emp_col.create_index([("emp_code", ASCENDING)], unique=True)
        emp_col.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
        log_col.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
        log_col.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])  # matches sort: date desc, emp_code asc
        log_col.create_index([("status", ASCENDING), ("date", ASCENDING)])
        print("Indexes created.")

    for col, docs in ((emp_col, emps), (log_col, logs)):
        for i in range(0, len(docs), batch_size):
            guard()
            try:
                col.insert_many(docs[i:i + batch_size], ordered=False)
            except BulkWriteError as exc:
                die(f"bulk insert into {col.name} failed: {str(exc.details)[:500]}")
            print(f"  {col.name}: inserted {min(i + batch_size, len(docs))}/{len(docs)}")

    # read-only verification
    n_emp, n_log = emp_col.count_documents({}), log_col.count_documents({})
    with_in = sum(1 for r in logs if r["punch_in"] is not None)
    ok = (n_emp == len(emps) and n_log == len(logs)
          and log_col.count_documents({"punch_in": {"$type": "date"}}) == with_in
          and log_col.count_documents({"punch_in": {"$type": "string"}}) == 0
          and emp_col.count_documents({"created_at": {"$type": "date"}}) == len(emps))
    sample = log_col.find_one({"punch_in": {"$ne": None}})
    ok = ok and sample is not None and sample["punch_in"].tzinfo is not None
    print(f"Verification: employees={n_emp}, attendance_logs={n_log}, BSON date types OK={ok}")
    if not ok:
        die("post-insert verification failed.")


# --------------------------------------------------------------------------------------
def parse_args(argv):
    p = argparse.ArgumentParser(description="Generate the HROne attendance performance-test dataset "
                                            f"(target DB is always '{TARGET_DB}').")
    p.add_argument("--write", action="store_true", help="actually write to MongoDB (needs the confirm flag too)")
    p.add_argument(CONFIRM_FLAG, dest="confirm", action="store_true", help="explicit confirmation to write")
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument("--employees", type=int, default=DEFAULT_EMPLOYEES)
    p.add_argument("--target-records", type=int, default=DEFAULT_TARGET)
    p.add_argument("--skip-indexes", action="store_true", help="do not create indexes (let the app create them)")
    p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH)
    a = p.parse_args(argv)
    if a.write != a.confirm:
        p.error(f"writing needs BOTH --write and {CONFIRM_FLAG}; got only one. Nothing was done.")
    if not (MIN_EMPLOYEES <= a.employees <= 999_999):
        p.error(f"--employees must be between {MIN_EMPLOYEES} and 999999")
    if a.target_records < 1000 or a.batch_size < 1:
        p.error("--target-records must be >= 1000 and --batch-size >= 1")
    return a


def main(argv=None):
    args = parse_args(argv)
    t0 = time.time()
    emps, logs, roles, tie_plan = build_dataset(args.seed, args.employees, args.target_records)
    chk, cov, tie_lines = validate(emps, logs, roles, tie_plan, args.target_records)
    print_report(args, emps, logs, roles, chk, cov, tie_lines, time.time() - t0)
    if chk.failed:
        die("validation failed; nothing will be written.")
    if not args.write:
        print("\nDry run complete. No database connection was made. To write, re-run with:\n"
              f"  python generate_test_dataset.py --write {CONFIRM_FLAG}")
        return 0
    print("\nValidation passed; writing to MongoDB ...")
    write_to_mongo(emps, logs, args.skip_indexes, args.batch_size)
    return 0


if __name__ == "__main__":
    sys.exit(main())
