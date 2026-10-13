# Employee Attendance & Analytics API

A FastAPI and MongoDB application providing employee management, real-time attendance tracking, regularization audit trails, and aggregation-driven analytics compliant with `openapi.yaml` and `DATA_MODEL.md`.

---

## 1. Prerequisites

* **Python**: Version 3.11 or newer
* **MongoDB**: Version 6.0 or newer (tested with MongoDB 7.0 / MongoDB Atlas)

---

## 2. Environment Setup & Configuration

### Virtual Environment & Dependencies
```bash
# Create virtual environment
python -m venv .venv

# Activate virtual environment
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# Linux / macOS:
source .venv/bin/activate

# Install required dependencies
pip install -r requirements.txt
```

### Environment Variables
Configure the MongoDB connection in a local `.env` file in the project root:

```ini
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

For MongoDB Atlas, use standard URI format with your credentials placeholder:
```ini
MONGO_URI=mongodb+srv://<username>:<password>@<cluster-address>/?retryWrites=true&w=majority
MONGO_DB=attendance_db
```

> **Security Note:** The `.env` file is excluded in `.gitignore`. Never commit `.env`, credentials, passwords, or connection tokens to version control.

---

## 3. Database Initialization & Seeding

All required database indexes are created idempotently at application startup via FastAPI's lifespan handler.

To load the initial sample dataset into MongoDB:
```bash
python sample_seed.py
```

---

## 4. Starting the Application

Start the development server on port 8000:
```bash
uvicorn app.main:app --port 8000
```

The OpenAPI documentation is accessible at `http://localhost:8000/docs`.

---

## 5. Running the Test Suite

> **Note:** Testing requires `pytest`. If `pytest` is not installed in your environment (as test runners are omitted from production `requirements.txt`), install it via:
> ```bash
> pip install pytest
> ```

Execute the automated test suite using the standard Python module runner:
```bash
python -m pytest -v
```

All test cases validate status codes, edge cases (overnight shifts, ties, weekend rules, mid-month joiners), and schema compliance.

---

## 6. Core API Endpoints & Usage

### System & Health
* **`GET /health`**
  Readiness probe returning 200 OK if MongoDB responds to ping, or 503 if unavailable.
  ```bash
  curl -X GET "http://localhost:8000/health"
  ```

### Employees
* **`POST /employees`**
  Create an employee. Enforces unique `emp_code` (409 on duplicate), differing shift times (422), and valid date formats.
  ```bash
  curl -X POST "http://localhost:8000/employees" \
       -H "Content-Type: application/json" \
       -d '{
         "emp_code": "EMP0001",
         "name": "Asha Rao",
         "email": "asha@example.com",
         "department": "Engineering",
         "shift_start": "09:30",
         "shift_end": "18:30",
         "joined_on": "2026-01-05"
       }'
  ```

* **`GET /employees`**
  List employees with department filtering and pagination, sorted by `emp_code` ascending.
  ```bash
  curl -X GET "http://localhost:8000/employees?department=Engineering&page=1&page_size=20"
  ```

### Attendance Tracking
* **`POST /attendance/punch-in`**
  Record daily punch-in. Computes attendance date and late minutes (R1, R2). Returns 409 if already punched in for the date.
  ```bash
  curl -X POST "http://localhost:8000/attendance/punch-in" \
       -H "Content-Type: application/json" \
       -d '{
         "emp_code": "EMP0001",
         "punched_at": 1783312500000,
         "status": "PRESENT"
       }'
  ```

* **`POST /attendance/punch-out`**
  Record punch-out. Computes work hours with decimal half-up rounding (R4), overtime (R3), and half-day status (R5).
  ```bash
  curl -X POST "http://localhost:8000/attendance/punch-out" \
       -H "Content-Type: application/json" \
       -d '{
         "emp_code": "EMP0001",
         "punched_at": 1783344600000
       }'
  ```

* **`GET /attendance`**
  Query attendance logs with date ranges, employee code, and status filters.
  ```bash
  curl -X GET "http://localhost:8000/attendance?emp_code=EMP0001&date_from=2026-07-01&date_to=2026-07-31"
  ```

* **`PATCH /attendance/{emp_code}/{date}`**
  Regularize a day's record, recalculating derived fields and recording an audit trail in `history`.
  ```bash
  curl -X PATCH "http://localhost:8000/attendance/EMP0001/2026-07-06" \
       -H "Content-Type: application/json" \
       -d '{
         "status": "PRESENT",
         "reason": "Card reader malfunction at front gate",
         "regularized_by": "HR Admin"
       }'
  ```

### Analytics (Aggregation Pipelines)
* **`GET /analytics/employees/{emp_code}/monthly`**
  Monthly summary for a single employee (present days, work hours, overtime, late arrival counts).
  ```bash
  curl -X GET "http://localhost:8000/analytics/employees/EMP0001/monthly?month=2026-07"
  ```

* **`GET /analytics/departments/summary`**
  Department monthly breakdown counting headcount (including employees with zero logs) and attendance metrics.
  ```bash
  curl -X GET "http://localhost:8000/analytics/departments/summary?month=2026-07&department=Engineering"
  ```

* **`GET /analytics/leaderboard/late`**
  Late arrival leaderboard using `$setWindowFields` with standard competition ranking (`$rank`).
  ```bash
  curl -X GET "http://localhost:8000/analytics/leaderboard/late?month=2026-07&limit=10"
  ```

* **`GET /analytics/departments/{department}/trend`**
  Daily attendance series including gap-filled calendar dates and a 7-day moving average.
  ```bash
  curl -X GET "http://localhost:8000/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-31"
  ```

---

## 7. MongoDB Explain Endpoint (`GET /admin/explain/{endpoint}`)

The explain endpoint runs MongoDB's `explain` command with `executionStats` verbosity on the query or aggregation pipeline executed by the target endpoint.

### Supported Target Endpoints & Parameters

| Endpoint Name | Required Parameters | Optional Parameters | Description |
|---|---|---|---|
| `attendance_list` | None | `emp_code`, `date_from`, `date_to`, `status`, `page`, `page_size` | Explains the `find()` query on `attendance_logs`. |
| `employee_monthly` | `emp_code`, `month` (`YYYY-MM`) | None | Explains the monthly employee aggregation pipeline. |
| `department_summary` | `month` (`YYYY-MM`) | `department` | Explains the cross-collection department summary pipeline. |
| `late_leaderboard` | `month` (`YYYY-MM`) | `limit` (1–50), `department` | Explains the late leaderboard ranking pipeline. |
| `department_trend` | `department`, `from` (`YYYY-MM-DD`), `to` (`YYYY-MM-DD`) | None | Explains the daily trend aggregation pipeline. |

### Example Explain Requests
```bash
# Explain attendance list query
curl -X GET "http://localhost:8000/admin/explain/attendance_list?emp_code=EMP0001&date_from=2026-07-01&date_to=2026-07-31"

# Explain department summary aggregation
curl -X GET "http://localhost:8000/admin/explain/department_summary?month=2026-07&department=Engineering"

# Explain late leaderboard pipeline
curl -X GET "http://localhost:8000/admin/explain/late_leaderboard?month=2026-07&limit=10"

# Explain department trend aggregation
curl -X GET "http://localhost:8000/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-31"
```

### Inspecting Index Usage & Execution Stats
MongoDB explain plan structures vary between `find()` queries and aggregation pipelines, and are often nested:
* **`find()` Queries (`attendance_list`)**: In `queryPlanner.winningPlan`, MongoDB typically executes a `"FETCH"` stage whose inner `inputStage` contains `"stage": "IXSCAN"` referencing the index in `indexName`.
* **Aggregation Pipelines**: Index scan stages are nested inside `stages[0]["$cursor"]["queryPlanner"]["winningPlan"]` and stage execution stats inside `stages[0]["$cursor"]["executionStats"]` (or inside sub-pipeline stages such as `$lookup`).
* **Small Datasets vs 100,000-Record Dataset**: On small datasets (such as the 9-document `sample_seed.py` data), MongoDB's cost-based query optimizer may legitimately choose a collection scan (`COLLSCAN`) because in-memory sequential scanning overhead is lower than index traversal. Query index efficiency must be evaluated against the full ~100,000-record dataset.
* **Key Metrics to Inspect in `executionStats`**:
  * **`totalKeysExamined` & `totalDocsExamined`**: For index scans, `totalKeysExamined` indicates index keys traversed, while `totalDocsExamined` should match or remain close to `nReturned` documents.
  * **`collectionScans` / `COLLSCAN`**: Verify that no unindexed `COLLSCAN` stages occur against the 100,000-record dataset.
  * **`indexesUsed` / `indexName`**: Confirms that the winning plan selects the intended compound or unique B-tree index.
