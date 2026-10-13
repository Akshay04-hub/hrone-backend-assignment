# DECISIONS.md

1. **Indexes:** On `employees`, created `emp_code` (unique) for O(1) lookups, `(department, emp_code)` for department listings, and `joined_on` / `(department, joined_on)` for headcount filters. On `attendance_logs`, created `(emp_code, date)` unique (both ASC) for integrity and race prevention, `(date DESC, emp_code ASC)` for sorting, and `(emp_code ASC, date DESC)` for history queries. Rejected indexing `(department, date)` on logs to prevent denormalization.

2. **Punch-in race:** Both requests validate the employee and compute the date. During concurrent inserts, MongoDB's unique `(emp_code, date)` index guarantees atomic serialization: exactly one insert succeeds (201 Created), while the other throws `DuplicateKeyError`, caught and returned as 409 Conflict.

3. **Ties:** Aggregation uses `$setWindowFields` with `$rank` (1, 2, 2, 4), tie-broken by `emp_code` ASC. Because `rank <= limit` applies after ranking, all tied employees at the cutoff are returned, preserving fairness over truncation.

4. **Headcount:** Aggregation starts from `employees` filtered by `joined_on <= month_end`, then `$lookup` joins `attendance_logs` with `preserveNullAndEmptyArrays: true`. Zero-log employees remain present; grouping uses `$addToSet` for `emp_code` and counts headcount via `$size`, preventing duplicate inflation from unwound logs.

5. **100x data:** Asynchronously pre-aggregate monthly summaries via Change Streams into dedicated collections, eliminating runtime cross-collection `$lookup` joins and scanning large collections.
