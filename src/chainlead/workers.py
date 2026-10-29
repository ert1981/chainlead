"""Background job runner for campaign runs (thread pool; one run per campaign at a time)."""
from __future__ import annotations

import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .models import iso, new_id, utcnow


class JobRunner:
    def __init__(self, engine: Any, max_workers: int = 2) -> None:
        self.engine = engine
        self.pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="chainlead-job")
        self.jobs: dict[str, dict] = {}
        self._running: set[tuple[str, str]] = set()
        self._lock = threading.Lock()

    def submit(self, tenant: str, campaign_id: str) -> dict:
        self.engine.get_campaign(tenant, campaign_id)          # 404 early
        with self._lock:
            if (tenant, campaign_id) in self._running:
                existing = next(j for j in self.jobs.values()
                                if j["tenant"] == tenant and j["campaign_id"] == campaign_id and j["status"] == "running")
                return {k: v for k, v in existing.items() if k != "tenant"}
            job = {"job_id": new_id("job"), "tenant": tenant, "campaign_id": campaign_id, "status": "running",
                   "started_at": iso(utcnow()), "finished_at": None, "result": None, "error": None}
            self.jobs[job["job_id"]] = job
            self._running.add((tenant, campaign_id))
        self.pool.submit(self._run, job)
        return {k: v for k, v in job.items() if k != "tenant"}

    def _run(self, job: dict) -> None:
        try:
            job["result"] = self.engine.run_campaign(job["tenant"], job["campaign_id"])
            job["status"] = "completed"
        except Exception as e:  # noqa: BLE001
            job["status"] = "failed"
            job["error"] = f"{type(e).__name__}: {e}"
            job["trace"] = traceback.format_exc()[-2000:]
        finally:
            job["finished_at"] = iso(utcnow())
            with self._lock:
                self._running.discard((job["tenant"], job["campaign_id"]))

    def get(self, tenant: str, job_id: str) -> dict | None:
        j = self.jobs.get(job_id)
        if not j or j["tenant"] != tenant:
            return None
        return {k: v for k, v in j.items() if k not in ("tenant", "trace")}

    def wait(self, job_id: str, timeout: float = 60) -> None:
        import time
        end = time.time() + timeout
        while time.time() < end and self.jobs[job_id]["status"] == "running":
            time.sleep(0.02)
