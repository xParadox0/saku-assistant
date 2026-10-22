# Dashboard Category Drill-Down Implementation Plan

**Goal:** Let a user open dashboard spending categories and see the underlying transactions for the same reporting period.

**Architecture:** Keep the feature read-only. Dashboard links carry the displayed category and half-open date interval `[start, end)` to the existing transaction page. Add a repository query with tenant, category, dates, and the same expense inclusion rules as `spending_by_category`; render the filter state and reconciled total. Invalid filters must not broaden the query.

**Tech Stack:** FastAPI, Jinja2, PostgreSQL/psycopg, pytest.

---

### Task 1: Define visible behavior in tests
- Add tests in `tests/test_web.py` that assert dashboard links include category and matching period, filtered transactions are category/period constrained, the total matches the category aggregation, invalid filters are rejected, and another tenant's rows never appear.
- Run the new tests first and verify expected failures.

### Task 2: Add a tenant-scoped repository query
- Add a read-only repository method in `saku/repo.py` that selects transactions for the validated category and `[start, end)` period, limited to negative `normal`/`fee` transactions to match spending aggregation.
- Test inclusion/exclusion and sum reconciliation with real database fixtures.

### Task 3: Wire dashboard links and filtered transaction view
- Update `saku/web/app.py` to validate the category and ISO dates, use the exact tenant id, and pass filter metadata to the template.
- Update `saku/web/templates/_overview_body.html` so every displayed category is an accessible link.
- Update `saku/web/templates/transactions.html` to show the selected category, period, total, and explicit empty state while preserving the ordinary transaction page.

### Task 4: Document and verify
- Update F21 in `docs/PRD.md` and the relevant HLD note without changing F23 scope.
- Run focused web tests, then the full test suite and `git diff --check`.
- Do not deploy or modify production services in this task.
