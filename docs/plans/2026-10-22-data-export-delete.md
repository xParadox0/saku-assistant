# Saku Data Export and Deletion Implementation Plan

**Goal:** Let an authenticated member download their tenant's non-secret data and permanently delete the tenant's stored data with explicit confirmation.

**Architecture:** Put tenant-filtered reads and transactional deletion in `Repo`. Export a JSON document covering tenant-owned product data plus the current member's beta consent record, while explicitly excluding authentication secrets and short-lived security events. Deletion requires CSRF and an exact confirmation phrase, refuses a shared tenant, removes dependent rows in FK-safe order, revokes sessions/codes, and signs the requester out. The UI states that retained backups expire according to the existing policy.

**Tech Stack:** FastAPI, Jinja2, PostgreSQL/psycopg, Python stdlib JSON, pytest.

---

### Task 1: Add failing tests
- Test export completeness and cross-tenant exclusion with seeded tenant data.
- Test invalid confirmation and CSRF do not mutate data.
- Test deletion removes all tenant-owned rows, revokes linked Telegram/access state, preserves other tenants, and refuses shared-tenant deletion.
- Run the tests first and confirm expected failures.

### Task 2: Add repository export and deletion operations
- Implement allowlisted tenant queries returning dictionaries, never secrets such as login-code HMACs or session hashes.
- Implement transactional deletion with explicit tenant scope and FK-safe ordering; block deletion unless the requesting member is the tenant's sole member.

### Task 3: Add authenticated settings routes and UI
- Add a JSON attachment endpoint and a CSRF-protected delete POST requiring `DELETE MY SAKU DATA`.
- Render export and deletion controls in Settings, describe the backup retention boundary, and clear the session cookie after deletion.

### Task 4: Verify
- Run the new data-rights tests, full suite, and `git diff --check`.
- Do not execute deletion against production data, publish a release, or change beta approvals in this task.
