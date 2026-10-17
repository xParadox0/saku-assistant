#!/usr/bin/env python3
"""Run `python -m saku demo` against a throwaway local PostgreSQL (pgserver). For machines without Docker."""
import os
import subprocess
import sys
import tempfile

import pgserver

srv = pgserver.get_server(tempfile.mkdtemp(prefix="saku-demo-"), cleanup_mode="delete")
env = dict(os.environ, DATABASE_URL=srv.get_uri())
sys.exit(subprocess.call([sys.executable, "-m", "saku", "demo"], env=env))
