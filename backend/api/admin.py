"""
ClipCut AI — admin routes
  GET  /api/admin/stats
  DELETE /api/admin/cache
  GET  /api/admin/whisper-cache
  DELETE /api/admin/whisper-cache
  GET  /api/admin/transcript-cache
  DELETE /api/admin/transcript-cache
"""
import os
from fastapi import APIRouter

from config import CACHE_DIR, OUTPUT_DIR
from db     import db_count
from state  import jobs
import core.transcriber as trans_mod

router = APIRouter(prefix="/admin")


@router.get("/stats")
async def admin_stats():
    """System stats for the admin dashboard."""
    # Video cache files
    cache_files = []
    if os.path.isdir(CACHE_DIR):
        for f in os.listdir(CACHE_DIR):
            fp = os.path.join(CACHE_DIR, f)
            if os.path.isfile(fp):
                cache_files.append({
                    "name":    f,
                    "size_mb": round(os.path.getsize(fp) / 1_048_576, 1),
                })
    cache_total_mb = round(sum(c["size_mb"] for c in cache_files), 1)

    # Output clips
    output_bytes = 0.0
    if os.path.isdir(OUTPUT_DIR):
        for root, _, files in os.walk(OUTPUT_DIR):
            for f in files:
                output_bytes += os.path.getsize(os.path.join(root, f))
    output_mb = round(output_bytes / 1_048_576, 1)

    # Job status counts
    statuses: dict = {}
    for j in jobs.values():
        s = j.get("status", "unknown")
        statuses[s] = statuses.get(s, 0) + 1

    return {
        "jobs_total":      db_count(),
        "jobs_active":     sum(1 for j in jobs.values()
                               if j.get("status") == "processing"),
        "jobs_by_status":  statuses,
        "cache_files":     cache_files,
        "cache_total_mb":  cache_total_mb,
        "output_total_mb": output_mb,
    }


@router.delete("/cache")
async def clear_video_cache():
    """Delete all downloaded video files from the cache."""
    deleted = 0
    if os.path.isdir(CACHE_DIR):
        for f in os.listdir(CACHE_DIR):
            fp = os.path.join(CACHE_DIR, f)
            if os.path.isfile(fp):
                try:
                    os.remove(fp)
                    deleted += 1
                except OSError:
                    pass
    return {"deleted": deleted}


@router.get("/whisper-cache")
async def get_whisper_cache():
    """Return the list of Whisper models currently loaded in RAM."""
    return {
        "loaded_models": list(trans_mod._WHISPER_CACHE.keys()),
        "count":         len(trans_mod._WHISPER_CACHE),
    }


@router.delete("/whisper-cache")
async def clear_whisper_cache():
    """Evict all Whisper models from RAM (frees memory immediately)."""
    models = list(trans_mod._WHISPER_CACHE.keys())
    trans_mod._WHISPER_CACHE.clear()
    return {"evicted": models}


@router.get("/transcript-cache")
async def get_transcript_cache():
    """Return info about the on-disk transcript cache."""
    from config import TRANSCRIPT_CACHE_DIR
    files = []
    if os.path.isdir(TRANSCRIPT_CACHE_DIR):
        for f in os.listdir(TRANSCRIPT_CACHE_DIR):
            if f.endswith(".json"):
                fp = os.path.join(TRANSCRIPT_CACHE_DIR, f)
                files.append({
                    "key":     f[:-5],
                    "size_kb": round(os.path.getsize(fp) / 1024, 1),
                })
    return {
        "count":    len(files),
        "entries":  files,
        "total_kb": round(sum(e["size_kb"] for e in files), 1),
    }


@router.delete("/transcript-cache")
async def clear_transcript_cache():
    """Delete all cached transcript JSON files."""
    deleted = trans_mod.clear_transcript_cache()
    return {"deleted": deleted}
