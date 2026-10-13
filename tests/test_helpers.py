from datetime import datetime, timezone
import pytest
from app.main import (
    IST,
    truncate_to_second,
    epoch_ms_to_datetime,
    datetime_to_epoch_ms,
    to_ist,
    get_attendance_date,
    compute_late_minutes,
    compute_work_hours,
    compute_half_day,
    compute_overtime,
    is_overnight,
)


def test_epoch_ms_conversions_and_truncation():
    # 1783312500900 ms -> should truncate to 1783312500000
    ms = 1783312500900
    dt = epoch_ms_to_datetime(ms)
    assert dt.microsecond == 0
    assert datetime_to_epoch_ms(dt) == 1783312500000


def test_r1_overnight_shift_detection():
    assert is_overnight("22:00", "06:00") is True
    assert is_overnight("09:30", "18:30") is False
    assert is_overnight("18:30", "18:30") is True


def test_r1_attendance_date_standard_shift():
    # 09:30 IST on 2026-07-06 is 04:00 UTC
    dt_utc = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    date_str = get_attendance_date(dt_utc, "09:30", "18:30")
    assert date_str == "2026-07-06"


def test_r1_attendance_date_overnight_shift():
    # Overnight shift: 22:00 - 06:00
    # Punch in at 21:55 IST on 2026-07-06 (16:25 UTC) -> date is 2026-07-06
    dt_evening = datetime(2026, 7, 6, 16, 25, 0, tzinfo=timezone.utc)
    assert get_attendance_date(dt_evening, "22:00", "06:00") == "2026-07-06"

    # Punch in at 05:40 IST on 2026-07-07 (00:10 UTC) -> strictly before 06:00, belongs to 2026-07-06
    dt_morning = datetime(2026, 7, 7, 0, 10, 0, tzinfo=timezone.utc)
    assert get_attendance_date(dt_morning, "22:00", "06:00") == "2026-07-06"

    # Punch in at 06:05 IST on 2026-07-07 -> not earlier than 06:00, belongs to 2026-07-07
    dt_next_day = datetime(2026, 7, 7, 0, 35, 0, tzinfo=timezone.utc)
    assert get_attendance_date(dt_next_day, "22:00", "06:00") == "2026-07-07"


def test_r2_late_calculation():
    # Shift start 09:30 IST on 2026-07-06
    # 09:30:00 IST = 04:00:00 UTC
    date_str = "2026-07-06"

    # Example 1: 09:40:00 IST (exactly 10 min grace) -> 0 late
    dt_1 = datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_1, "09:30", date_str) == 0

    # Example 2: 09:40:01 IST (strictly > 10 min grace) -> 10 minutes late
    dt_2 = datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_2, "09:30", date_str) == 10

    # Example 3: 10:15:59 IST -> 45 minutes late
    dt_3 = datetime(2026, 7, 6, 4, 45, 59, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_3, "09:30", date_str) == 45


def test_r3_overtime_calculation_standard():
    # Shift 09:30 - 18:30 IST on 2026-07-06
    # 18:30 IST = 13:00 UTC
    date_str = "2026-07-06"

    # Punch out at 18:55 IST (25 min overtime < 30) -> 0
    dt_out_1 = datetime(2026, 7, 6, 13, 25, 0, tzinfo=timezone.utc)
    assert compute_overtime(dt_out_1, "09:30", "18:30", date_str) == 0

    # Punch out at 19:00 IST (30 min overtime == 30) -> 30
    dt_out_2 = datetime(2026, 7, 6, 13, 30, 0, tzinfo=timezone.utc)
    assert compute_overtime(dt_out_2, "09:30", "18:30", date_str) == 30

    # Punch out at 19:10 IST (40 min overtime) -> 40
    dt_out_3 = datetime(2026, 7, 6, 13, 40, 0, tzinfo=timezone.utc)
    assert compute_overtime(dt_out_3, "09:30", "18:30", date_str) == 40


def test_r3_overtime_calculation_overnight():
    # Overnight shift 22:00 - 06:00 starting 2026-07-06
    # Shift end is 2026-07-07 06:00 IST = 00:30 UTC
    date_str = "2026-07-06"

    # Punch out at 2026-07-07 06:40 IST = 01:10 UTC (40 min overtime) -> 40
    dt_out = datetime(2026, 7, 7, 1, 10, 0, tzinfo=timezone.utc)
    assert compute_overtime(dt_out, "22:00", "06:00", date_str) == 40


def test_r4_work_hours_half_up_rounding():
    # 16290 seconds = 4.525 hours -> Half-up rounding MUST yield 4.53 (Banker's round would yield 4.52)
    t_in = datetime(2026, 7, 6, 0, 0, 0, tzinfo=timezone.utc)
    t_out = datetime(2026, 7, 6, 4, 31, 30, tzinfo=timezone.utc) # 4*3600 + 31*60 + 30 = 14400 + 1860 + 30 = 16290 sec
    assert compute_work_hours(t_in, t_out) == 4.53


def test_r5_half_day():
    assert compute_half_day(4.49) is True
    assert compute_half_day(4.50) is False
    assert compute_half_day(8.00) is False
    assert compute_half_day(None) is False
