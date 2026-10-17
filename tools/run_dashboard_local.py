#!/usr/bin/env python3
"""Start the dashboard on 127.0.0.1 with synthetic demo data and a throwaway PostgreSQL (pgserver).
For local development and screenshots only. Usage: python tools/run_dashboard_local.py [port]"""
import os
import sys
import tempfile

import pgserver

port = sys.argv[1] if len(sys.argv) > 1 else "8090"
srv = pgserver.get_server(tempfile.mkdtemp(prefix="saku-dash-"), cleanup_mode="delete")
os.environ.update(DATABASE_URL=srv.get_uri(), SAKU_AUTH_MODE="dev", SAKU_DEV_EMAIL="demo@saku.local", SAKU_LOAD_DEMO="1")
from saku.__main__ import serve  # noqa: E402

sys.exit(serve(["saku", "serve", "--host", "127.0.0.1", "--port", port]))
