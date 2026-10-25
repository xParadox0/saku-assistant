import asyncio

from saku.parsers.worker_client import ParserAdmission
from saku.web.app import ImportUploadMiddleware


def test_body_deadline_expiry_returns_408_and_releases_admission():
    async def run():
        gate = ParserAdmission(max_active=1)
        responses = []

        async def downstream(scope, receive, send):
            await asyncio.sleep(0.03)
            await receive()

        async def receive():
            return {"type": "http.request", "body": b"x", "more_body": True}

        async def send(message):
            responses.append(message)

        middleware = ImportUploadMiddleware(downstream, gate, max_body_bytes=8, body_timeout=0.01)
        scope = {"type": "http", "method": "POST", "path": "/app/imports", "headers": []}
        await middleware(scope, receive, send)
        assert responses[0]["status"] == 408
        assert gate.try_acquire()
        gate.release()

    asyncio.run(run())


def test_chunked_body_limit_returns_413_before_downstream():
    async def run():
        gate = ParserAdmission(max_active=1)
        responses = []
        completed = False
        messages = iter([
            {"type": "http.request", "body": b"1234", "more_body": True},
            {"type": "http.request", "body": b"5", "more_body": False},
        ])

        async def downstream(scope, receive, send):
            nonlocal completed
            while (await receive()).get("more_body"):
                pass
            completed = True

        async def receive():
            return next(messages)

        async def send(message):
            responses.append(message)

        middleware = ImportUploadMiddleware(downstream, gate, max_body_bytes=4, body_timeout=1)
        scope = {"type": "http", "method": "POST", "path": "/app/imports", "headers": []}
        await middleware(scope, receive, send)
        assert responses[0]["status"] == 413
        assert not completed
        assert gate.try_acquire()
        gate.release()

    asyncio.run(run())


def test_unauthorized_upload_is_rejected_before_admission_or_body_read():
    async def run():
        gate = ParserAdmission(max_active=1)
        responses = []
        body_read = False

        def authorize(scope):
            return 401, [], b"Sign-in required."

        async def downstream(scope, receive, send):
            raise AssertionError("unauthorized upload reached downstream")

        async def receive():
            nonlocal body_read
            body_read = True
            return {"type": "http.request", "body": b"secret", "more_body": False}

        async def send(message):
            responses.append(message)

        middleware = ImportUploadMiddleware(downstream, gate, max_body_bytes=8,
                                            body_timeout=1, authorize=authorize)
        scope = {"type": "http", "method": "POST", "path": "/app/imports", "headers": []}
        await middleware(scope, receive, send)

        assert responses[0]["status"] == 401
        assert not body_read
        assert gate.try_acquire()
        gate.release()

    asyncio.run(run())


def test_per_tenant_quota_rejects_before_body_and_leaves_other_tenants_a_turn():
    now = [0.0]
    gate = ParserAdmission(max_active=1, per_tenant_limit=2, per_tenant_window=60, clock=lambda: now[0])

    def authorize(scope):
        scope.setdefault("state", {})["import_tenant_id"] = scope["tenant"]
        return None

    async def run():
        body_read = False
        statuses = []

        async def downstream(scope, receive, send):
            await receive()
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        async def receive():
            nonlocal body_read
            body_read = True
            return {"type": "http.request", "body": b"x", "more_body": False}

        async def send(message):
            if message["type"] == "http.response.start":
                statuses.append(message["status"])

        middleware = ImportUploadMiddleware(downstream, gate, max_body_bytes=8,
                                            body_timeout=1, authorize=authorize)

        async def request(tenant):
            nonlocal body_read
            body_read = False
            statuses.clear()
            scope = {"type": "http", "method": "POST", "path": "/app/imports", "headers": [], "tenant": tenant}
            await middleware(scope, receive, send)
            return statuses[0], body_read

        assert await request("alice") == (204, True)
        assert await request("alice") == (204, True)
        assert await request("alice") == (429, False)
        assert await request("bob") == (204, True)
        now[0] = 61.0
        assert await request("alice") == (204, True)

    asyncio.run(run())


def test_fastapi_form_parser_preserves_chunked_upload_413():
    from fastapi import FastAPI, File, UploadFile
    from fastapi.testclient import TestClient

    app = FastAPI()

    @app.post("/app/imports")
    async def import_statement(file: UploadFile = File(...)):
        return {"filename": file.filename}

    def authorize(scope):
        scope.setdefault("state", {})["import_tenant_id"] = "alice"
        return None

    boundary = "saku-test-boundary"
    preamble = (
        f"--{boundary}" + "\r\n"
        + 'Content-Disposition: form-data; name="file"; filename="synthetic.pdf"'
        + "\r\nContent-Type: application/pdf\r\n\r\n"
    ).encode()
    suffix = ("\r\n--" + boundary + "--\r\n").encode()
    chunks = [preamble, b"x" * 16, suffix]
    middleware = ImportUploadMiddleware(
        app, ParserAdmission(max_active=1), len(preamble) + 8,
        body_timeout=1, authorize=authorize)

    with TestClient(middleware) as client:
        response = client.post(
            "/app/imports", content=iter(chunks),
            headers={"content-type": f"multipart/form-data; boundary={boundary}"})

    assert response.status_code == 413


def test_fastapi_form_parser_preserves_slow_upload_408():
    from fastapi import FastAPI, File, UploadFile
    from httpx import ASGITransport, AsyncClient

    app = FastAPI()

    @app.post("/app/imports")
    async def import_statement(file: UploadFile = File(...)):
        return {"filename": file.filename}

    def authorize(scope):
        scope.setdefault("state", {})["import_tenant_id"] = "alice"
        return None

    boundary = "saku-slow-boundary"
    preamble = (
        f"--{boundary}" + "\r\n"
        + 'Content-Disposition: form-data; name="file"; filename="synthetic.pdf"'
        + "\r\nContent-Type: application/pdf\r\n\r\n"
    ).encode()
    suffix = ("\r\n--" + boundary + "--\r\n").encode()
    middleware = ImportUploadMiddleware(
        app, ParserAdmission(max_active=1), 4096,
        body_timeout=0.1, authorize=authorize)

    async def slow_body():
        yield preamble
        await asyncio.sleep(0.5)
        yield b"synthetic"
        yield suffix

    async def run():
        async with AsyncClient(transport=ASGITransport(app=middleware), base_url="http://test") as client:
            response = await client.post(
                "/app/imports", content=slow_body(),
                headers={"content-type": f"multipart/form-data; boundary={boundary}"})
            return response.status_code

    assert asyncio.run(run()) == 408


def test_announced_oversized_upload_does_not_consume_tenant_quota():
    gate = ParserAdmission(max_active=1, per_tenant_limit=1, per_tenant_window=60)
    body_read = False
    statuses = []

    def authorize(scope):
        scope.setdefault("state", {})["import_tenant_id"] = "alice"
        return None

    async def downstream(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        nonlocal body_read
        body_read = True
        return {"type": "http.request", "body": b"x", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            statuses.append(message["status"])

    middleware = ImportUploadMiddleware(downstream, gate, max_body_bytes=8,
                                        body_timeout=1, authorize=authorize)

    async def request(content_length):
        nonlocal body_read
        body_read = False
        statuses.clear()
        scope = {"type": "http", "method": "POST", "path": "/app/imports",
                 "headers": [(b"content-length", content_length)]}
        await middleware(scope, receive, send)
        return statuses[0], body_read

    async def run():
        assert await request(b"9") == (413, False)
        assert await request(b"1") == (204, True)

    asyncio.run(run())


def test_noncanonical_account_import_path_cannot_bypass_auth_preflight():
    async def run():
        gate = ParserAdmission(max_active=1)
        responses = []
        body_read = False
        auth_checked = False

        def authorize(scope):
            nonlocal auth_checked
            auth_checked = True
            return 401, [], b"Sign-in required."

        async def downstream(scope, receive, send):
            nonlocal body_read
            await receive()
            body_read = True
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        async def receive():
            return {"type": "http.request", "body": b"oversize", "more_body": False}

        async def send(message):
            responses.append(message)

        middleware = ImportUploadMiddleware(downstream, gate, max_body_bytes=4,
                                            body_timeout=1, authorize=authorize)
        for path, raw_path, root_path in (
            ("/app/accounts/+1/import", b"/app/accounts/%2B1/import", ""),
            ("/app/accounts/1/import\n", b"/app/accounts/1/import%0A", ""),
            ("/app/imports\n", b"/app/imports%0A", ""),
            ("/prefix/app/imports", b"/prefix/app/imports", "/prefix"),
        ):
            auth_checked = False
            body_read = False
            responses.clear()
            scope = {"type": "http", "method": "POST", "path": path, "raw_path": raw_path, "headers": []}
            if root_path:
                scope["root_path"] = root_path
            await middleware(scope, receive, send)
            assert auth_checked
            assert responses[0]["status"] == 401
            assert not body_read

    asyncio.run(run())
