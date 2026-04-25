"""
ClipCut AI -- status / logs / history routes
"""
from fastapi import APIRouter, HTTPException

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
