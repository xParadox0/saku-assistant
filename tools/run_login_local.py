#!/usr/bin/env python3
"""Local server in own-sign-in mode: codes are printed to stdout instead of emailed (loopback only)."""
import os
import sys
import tempfile

import pgserver

port = sys.argv[1] if len(sys.argv) > 1 else "8091"
srv = pgserver.get_server(tempfile.mkdtemp(prefix="saku-login-"), cleanup_mode="delete")
os.environ.update(DATABASE_URL=srv.get_uri(), SAKU_AUTH_MODE="own", SAKU_MAIL="outbox", SAKU_LOAD_DEMO="1",
                  SAKU_SECRET="local-only-" + "x" * 32)
from saku.__main__ import serve  # noqa: E402

sys.exit(serve(["saku", "serve", "--host", "127.0.0.1", "--port", port]))
