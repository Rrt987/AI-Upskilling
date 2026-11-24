"""Start embedded Postgres (pgserver) and mcp-state-store (streamable HTTP on :8081).

Usage: python scripts/dev_stack.py        (leave running; Ctrl-C stops the state server)
The app is then started with: .venv/bin/uvicorn app.main:app --port 8000
"""
import os
import signal
import subprocess
import sys
from pathlib import Path

import pgserver

ROOT = Path(__file__).resolve().parent.parent
DATA = os.environ.get("PGDATA_DIR", str(ROOT / ".devdb"))

srv = pgserver.get_server(DATA)
env = {**os.environ, "PYTHONPATH": str(ROOT), "DATABASE_URL": srv.get_uri(), "STATE_PORT": os.environ.get("STATE_PORT", "8081")}
print("postgres:", srv.get_uri(), flush=True)
proc = subprocess.Popen([sys.executable, str(ROOT / "mcp_servers" / "state_server.py")], env=env, cwd=ROOT)
try:
    proc.wait()
except KeyboardInterrupt:
    proc.send_signal(signal.SIGINT)
    proc.wait()
