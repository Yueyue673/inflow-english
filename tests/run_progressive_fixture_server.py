from __future__ import annotations

import os
import sys
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import server

# Browser E2E owns shard publication explicitly. Prevent the normal background
# worker from racing the fixture writer; this module is only run with isolated
# QA data and a random port.
server.start_import_thread = lambda _job_id: True

if __name__ == "__main__":
    server.acquire_server_lock()
    uvicorn.run(server.app, host="127.0.0.1", port=int(os.environ["INFLOW_ADAPTIVE_PORT"]), log_level="warning")
