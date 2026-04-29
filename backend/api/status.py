"""
ClipCut AI -- status / logs / history routes
"""
import os
from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse, FileResponse

from config import OUTPUT_DIR
from db    import db_list
from state import jobs

router = APIRouter()


@router.get("/status/{job_id}")
async def get_status(job_id: str):
    """Return the current state of a processing job."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job introuvable")
    return jobs[job_id]


@router.get("/logs/{job_id}")
async def get_logs(job_id: str, since: int = 0):
    """Return log entries for a job starting from index `since`.
    Returns an empty list (not 404) when the job does not exist in RAM --
    this happens for stale job IDs from before a server restart.
    """
    if job_id not in jobs:
        return {"logs": [], "total": 0, "stale": True}
    log_list = jobs[job_id].get("logs", [])
    return {"logs": log_list[since:], "total": len(log_list)}


@router.get("/logs/{job_id}/file", response_class=PlainTextResponse)
async def get_log_file(job_id: str, download: bool = False):
    """Return the full log file for a job as plain text.
    Add ?download=true to get it as a file attachment.
    """
    # Security: only allow UUID-shaped job IDs (no path traversal)
    import re
    if not re.match(r'^[0-9a-f-]{36}$', job_id):
        raise HTTPException(status_code=400, detail="ID invalide")

    log_path = os.path.join(OUTPUT_DIR, job_id, "job.log")

    if not os.path.exists(log_path):
        # Fallback: build from in-memory logs if file doesn't exist yet
        if job_id in jobs:
            content = "\n".join(jobs[job_id].get("logs", [])) + "\n"
            return PlainTextResponse(content, media_type="text/plain; charset=utf-8")
        raise HTTPException(status_code=404, detail="Log introuvable pour ce job")

    if download:
        return FileResponse(
            log_path,
            filename=f"clipcut_{job_id[:8]}.log",
            media_type="text/plain"
        )

    with open(log_path, "r", encoding="utf-8") as f:
        content = f.read()
    return PlainTextResponse(content, media_type="text/plain; charset=utf-8")


@router.get("/history")
async def get_history(limit: int = 20):
    """Return a summary list of completed jobs from SQLite (survives restarts)."""
    rows = db_list(limit)
    return [
        {
            "id":         row["id"],
            "title":      row.get("title"),
            "status":     row.get("status"),
            "clip_count": len(row.get("clips", [])),
            "created_at": row.get("created_at"),
        }
        for row in rows
    ]
