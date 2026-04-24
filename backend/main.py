"""
ClipCut AI v2 — Micro SaaS Backend
FastAPI server for YouTube → TikTok clip generation
New in v2: SQLite persistence, thumbnail endpoint, history, admin stats,
           trim endpoint, whisper model choice, watermark, silence removal,
           hook intro, webhook notifications.
"""

from fastapi import FastAPI, BackgroundTasks, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uuid, os, zipfile, io, sqlite3, json, subprocess
from datetime import datetime
from typing import Optional
import yt_dlp

from processor import VideoProcessor

app = FastAPI(title="ClipCut AI", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── SQLite persistence ──────────────────────────────────────────────────────
DB_PATH    = os.path.join(os.path.expanduser("~"), "ClipCutAI_jobs.db")
CACHE_DIR  = os.path.join(os.path.expanduser("~"), "ClipCutAI_cache")
OUTPUT_DIR = os.path.join(os.path.expanduser("~"), "ClipCutAI_outputs")

def _db_init():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id         TEXT PRIMARY KEY,
            data       TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")
        conn.commit()

def _db_save(job_id: str, data: dict):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO jobs(id, data, created_at) "
            "VALUES(?, ?, COALESCE((SELECT created_at FROM jobs WHERE id=?), ?))",
            (job_id, json.dumps(data, ensure_ascii=False),
             job_id, datetime.utcnow().isoformat())
        )
        conn.commit()

def _db_list(limit: int = 50):
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, data, created_at FROM jobs ORDER BY created_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
    return [{"id": r[0], **json.loads(r[1]), "created_at": r[2]} for r in rows]

def _db_count():
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()
    return row[0] if row else 0

_db_init()

# ─── In-memory job store (fast access during processing) ────────────────────
jobs: dict = {}

# Load recent completed/errored jobs from DB on startup
for _row in _db_list(200):
    _jid = _row.pop("id")
    jobs[_jid] = _row


# ─── Request model ───────────────────────────────────────────────────────────
class ProcessRequest(BaseModel):
    youtube_url:      str
    openai_api_key:   str
    max_clips:        int   = 5
    clip_duration:    int   = 60
    language:         str   = "auto"
    subtitle_style:   str   = "elevate"
    video_start:      Optional[float] = None
    video_end:        Optional[float] = None
    face_tracking:    bool  = False
    smart_zoom:       bool  = False
    subtitle_lang:    str   = "original"
    music_track:      str   = ""
    music_volume:     float = 0.15
    # New v2 fields
    whisper_model:    str   = "base"      # tiny | base | small | medium
    watermark:        str   = ""          # e.g. "@myhandle"
    silence_removal:  bool  = False
    add_hook:         bool  = False
    webhook_url:      str   = ""


# ─── Helper: persist job after it finishes ──────────────────────────────────
async def _run_and_persist(processor: VideoProcessor, *args, **kwargs):
    """Wrapper that saves the job to SQLite when processing completes."""
    await processor.process(*args, **kwargs)
    job_data = jobs.get(processor.job_id, {})
    if job_data.get("status") in ("completed", "error"):
        _db_save(processor.job_id, job_data)


# ─── Process endpoint ────────────────────────────────────────────────────────
@app.post("/api/process")
async def process_video(request: ProcessRequest,
                        background_tasks: BackgroundTasks):
    """Submit a YouTube URL for processing into TikTok clips."""
    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "status":   "queued",
        "progress": 0,
        "message":  "En attente de démarrage...",
        "clips":    [],
        "error":    None,
        "title":    None,
        "warnings": [],
        "logs":     [],
    }

    # Resolve music track
    music_path = None
    if request.music_track:
        candidate = os.path.join(os.path.dirname(__file__), "..", "music",
                                 request.music_track)
        if os.path.exists(candidate):
            music_path = os.path.abspath(candidate)

    processor = VideoProcessor(
        job_id, jobs, request.openai_api_key,
        subtitle_style  = request.subtitle_style,
        face_tracking   = request.face_tracking,
        smart_zoom      = request.smart_zoom,
        music_track     = music_path,
        music_volume    = request.music_volume,
        whisper_model   = request.whisper_model,
        watermark       = request.watermark,
        silence_removal = request.silence_removal,
        add_hook        = request.add_hook,
        webhook_url     = request.webhook_url,
    )

    background_tasks.add_task(
        _run_and_persist,
        processor,
        request.youtube_url,
        request.max_clips,
        request.clip_duration,
        request.language,
        subtitle_style  = request.subtitle_style,
        video_start     = request.video_start,
        video_end       = request.video_end,
        face_tracking   = request.face_tracking,
        smart_zoom      = request.smart_zoom,
        subtitle_lang   = request.subtitle_lang,
        music_track     = music_path,
        music_volume    = request.music_volume,
        whisper_model   = request.whisper_model,
        watermark       = request.watermark,
        silence_removal = request.silence_removal,
        add_hook        = request.add_hook,
        webhook_url     = request.webhook_url,
    )
    return {"job_id": job_id}


# ─── Batch processing ────────────────────────────────────────────────────────
class BatchRequest(BaseModel):
    youtube_urls:     list[str]
    openai_api_key:   str
    max_clips:        int   = 3
    clip_duration:    int   = 60
    language:         str   = "auto"
    subtitle_style:   str   = "elevate"
    whisper_model:    str   = "base"
    watermark:        str   = ""
    silence_removal:  bool  = False

@app.post("/api/process-batch")
async def process_batch(request: BatchRequest,
                        background_tasks: BackgroundTasks):
    """Submit multiple YouTube URLs for batch processing."""
    job_ids = []
    for url in request.youtube_urls[:10]:  # max 10 URLs at once
        url = url.strip()
        if not url:
            continue
        job_id = str(uuid.uuid4())
        jobs[job_id] = {
            "status": "queued", "progress": 0,
            "message": "En attente (batch)...",
            "clips": [], "error": None, "title": None, "warnings": [],
        }
        processor = VideoProcessor(
            job_id, jobs, request.openai_api_key,
            subtitle_style  = request.subtitle_style,
            whisper_model   = request.whisper_model,
            watermark       = request.watermark,
            silence_removal = request.silence_removal,
        )
        background_tasks.add_task(
            _run_and_persist, processor,
            url, request.max_clips, request.clip_duration, request.language,
            subtitle_style = request.subtitle_style,
            whisper_model  = request.whisper_model,
            watermark      = request.watermark,
            silence_removal= request.silence_removal,
        )
        job_ids.append({"url": url, "job_id": job_id})
    return {"jobs": job_ids}


# ─── Status endpoint ─────────────────────────────────────────────────────────
@app.get("/api/status/{job_id}")
async def get_status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job introuvable")
    return jobs[job_id]


# ─── Download single clip ────────────────────────────────────────────────────
@app.get("/api/download/{job_id}/{clip_index}")
async def download_clip(job_id: str, clip_index: int):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job introuvable")
    job  = jobs[job_id]
    if clip_index >= len(job["clips"]):
        raise HTTPException(status_code=404, detail="Clip introuvable")
    clip = job["clips"][clip_index]
    path = clip.get("path", "")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Fichier introuvable")
    return FileResponse(path, media_type="video/mp4",
                        filename=f"tiktok_clip_{clip_index + 1}.mp4")


# ─── Download all as ZIP ─────────────────────────────────────────────────────
@app.get("/api/download-zip/{job_id}")
async def download_zip(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job introuvable")
    job   = jobs[job_id]
    clips = [c for c in job.get("clips", []) if os.path.exists(c.get("path", ""))]
    if not clips:
        raise HTTPException(status_code=404, detail="Aucun clip disponible")

    def zip_generator():
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            for i, clip in enumerate(clips):
                zf.write(clip["path"], arcname=f"tiktok_clip_{i + 1}.mp4")
        buf.seek(0)
        yield buf.read()

    title_slug = (job.get("title") or "clips").replace(" ", "_")[:40]
    return StreamingResponse(
        zip_generator(), media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{title_slug}_tiktok.zip"'},
    )


# ─── Thumbnail endpoint ──────────────────────────────────────────────────────
@app.get("/api/thumbnail/{job_id}/{clip_index}")
async def get_thumbnail(job_id: str, clip_index: int):
    """Extract and serve a JPEG thumbnail from the clip."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job introuvable")
    job = jobs[job_id]
    if clip_index >= len(job.get("clips", [])):
        raise HTTPException(status_code=404, detail="Clip introuvable")
    path = job["clips"][clip_index].get("path", "")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Fichier introuvable")

    thumb_path = path.replace(".mp4", "_thumb.jpg")
    if not os.path.exists(thumb_path):
        r = subprocess.run(
            ["ffmpeg", "-y", "-ss", "00:00:01", "-i", path,
             "-vframes", "1", "-q:v", "3", "-vf", "scale=270:480", thumb_path],
            capture_output=True
        )
        if r.returncode != 0 or not os.path.exists(thumb_path):
            raise HTTPException(status_code=500, detail="Impossible de générer la miniature")
    return FileResponse(thumb_path, media_type="image/jpeg")


# ─── Trim endpoint ───────────────────────────────────────────────────────────
@app.post("/api/trim/{job_id}/{clip_index}")
async def trim_clip(job_id: str, clip_index: int,
                    background_tasks: BackgroundTasks,
                    trim_start: float = Query(0.0),
                    trim_end:   float = Query(0.0)):
    """Re-cut a clip with adjusted start/end (relative offsets in seconds)."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job introuvable")
    job = jobs[job_id]
    if clip_index >= len(job.get("clips", [])):
        raise HTTPException(status_code=404, detail="Clip introuvable")

    clip = job["clips"][clip_index]
    src  = clip.get("source_video", "")
    orig_start = clip.get("source_start", 0.0)
    orig_end   = clip.get("source_end",   0.0)

    if not src or not os.path.exists(src):
        raise HTTPException(status_code=400,
                            detail="Vidéo source introuvable (cache manquant ?)")

    # Apply user offsets
    new_start = max(0.0, orig_start + trim_start)
    new_end   = max(new_start + 5.0, orig_end + trim_end)

    # Mark clip as re-processing
    job["clips"][clip_index]["retrimming"] = True

    # Simple ffmpeg re-cut (no subtitle re-render for now)
    clip_path = clip["path"]
    tmp = clip_path.replace(".mp4", "_trim_tmp.mp4")

    def do_trim():
        r = subprocess.run(
            ["ffmpeg", "-y", "-ss", str(new_start), "-to", str(new_end),
             "-i", clip_path,
             "-c:v", "libx264", "-preset", "fast", "-crf", "20",
             "-c:a", "copy", tmp],
            capture_output=True, text=True
        )
        if r.returncode == 0 and os.path.exists(tmp):
            os.replace(tmp, clip_path)
            job["clips"][clip_index]["duration"]    = round(new_end - new_start, 1)
            job["clips"][clip_index]["source_start"] = new_start
            job["clips"][clip_index]["source_end"]   = new_end
        job["clips"][clip_index]["retrimming"] = False
        # Invalidate thumbnail
        thumb = clip_path.replace(".mp4", "_thumb.jpg")
        if os.path.exists(thumb):
            os.remove(thumb)

    import asyncio
    asyncio.get_event_loop().run_in_executor(None, do_trim)
    return {"status": "trimming", "new_start": new_start, "new_end": new_end}


# ─── Pre-generation estimate ────────────────────────────────────────────────
@app.get("/api/estimate")
async def estimate_time(
    url:           str,
    whisper_model: str = "base",
    max_clips:     int = 5,
    clip_duration: int = 60,
):
    """
    Fetch video metadata (no download) and return a time estimate.
    Fast endpoint — yt-dlp only reads the info page.
    """
    from processor import _extract_video_id, WHISPER_SPEED, _fmt_duration
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True,
                                "no_warnings": True}) as ydl:
            info     = ydl.extract_info(url, download=False)
            duration = float(info.get("duration") or 0)
            title    = info.get("title", "")
            uploader = info.get("uploader", "")
            thumbnail= info.get("thumbnail", "")
    except Exception as e:
        raise HTTPException(status_code=400,
                            detail=f"Impossible d'analyser cette URL : {e}")

    # Check cache
    video_id = _extract_video_id(url)
    cached   = os.path.exists(os.path.join(CACHE_DIR, f"{video_id}.mp4"))

    # Compute estimate (same formula as processor._estimate_eta)
    whisper_f  = WHISPER_SPEED.get(whisper_model, 0.10)
    download   = 8  if cached else 55
    transcribe = duration * whisper_f
    gpt        = 12
    ffmpeg_t   = max_clips * (clip_duration * 0.65 + 12)
    total_sec  = int(download + transcribe + gpt + ffmpeg_t)

    # Breakdown for the UI
    breakdown = {
        "download":    int(download),
        "transcribe":  int(transcribe),
        "gpt":         int(gpt),
        "ffmpeg":      int(ffmpeg_t),
    }

    return {
        "title":       title,
        "uploader":    uploader,
        "thumbnail":   thumbnail,
        "duration":    int(duration),
        "duration_fmt": _fmt_duration(duration),
        "cached":      cached,
        "eta_seconds": total_sec,
        "eta_fmt":     _fmt_duration(total_sec),
        "breakdown":   breakdown,
        "whisper_model": whisper_model,
    }


# ─── Live logs endpoint ──────────────────────────────────────────────────────
@app.get("/api/logs/{job_id}")
async def get_logs(job_id: str, since: int = 0):
    """Return log entries for a job, starting from index `since`."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job introuvable")
    logs = jobs[job_id].get("logs", [])
    return {"logs": logs[since:], "total": len(logs)}


# ─── History endpoint ────────────────────────────────────────────────────────
@app.get("/api/history")
async def get_history(limit: int = 20):
    """Return list of completed jobs from SQLite (survives restarts)."""
    rows = _db_list(limit)
    # Return a summary (no full clip paths)
    summary = []
    for row in rows:
        summary.append({
            "id":          row["id"],
            "title":       row.get("title"),
            "status":      row.get("status"),
            "clip_count":  len(row.get("clips", [])),
            "created_at":  row.get("created_at"),
        })
    return summary


# ─── Admin stats endpoint ────────────────────────────────────────────────────
@app.get("/api/admin/stats")
async def admin_stats():
    """Return system stats for the admin dashboard."""
    # Cache
    cache_files = []
    if os.path.isdir(CACHE_DIR):
        for f in os.listdir(CACHE_DIR):
            fp = os.path.join(CACHE_DIR, f)
            if os.path.isfile(fp):
                cache_files.append({
                    "name":    f,
                    "size_mb": round(os.path.getsize(fp) / 1024 / 1024, 1),
                })
    cache_total_mb = round(sum(c["size_mb"] for c in cache_files), 1)

    # Output clips
    output_mb = 0.0
    if os.path.isdir(OUTPUT_DIR):
        for root, _, files in os.walk(OUTPUT_DIR):
            for f in files:
                output_mb += os.path.getsize(os.path.join(root, f))
    output_mb = round(output_mb / 1024 / 1024, 1)

    # Job counts from in-memory store
    statuses = {}
    for j in jobs.values():
        s = j.get("status", "unknown")
        statuses[s] = statuses.get(s, 0) + 1

    return {
        "jobs_total":    _db_count(),
        "jobs_active":   len([j for j in jobs.values()
                               if j.get("status") == "processing"]),
        "jobs_by_status": statuses,
        "cache_files":   cache_files,
        "cache_total_mb": cache_total_mb,
        "output_total_mb": output_mb,
    }


@app.delete("/api/admin/cache")
async def clear_cache():
    """Clear the video download cache."""
    deleted = 0
    if os.path.isdir(CACHE_DIR):
        for f in os.listdir(CACHE_DIR):
            fp = os.path.join(CACHE_DIR, f)
            if os.path.isfile(fp):
                os.remove(fp)
                deleted += 1
    return {"deleted": deleted}


# ─── Serve frontend (must be last) ──────────────────────────────────────────
frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(frontend_dir):
    app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")
