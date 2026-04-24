"""
ClipCut AI — Micro SaaS Backend
FastAPI server for YouTube → TikTok clip generation
"""

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uuid
import os

from processor import VideoProcessor

app = FastAPI(title="ClipCut AI", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory job store (use Redis in production)
jobs: dict = {}


class ProcessRequest(BaseModel):
    youtube_url: str
    openai_api_key: str
    max_clips: int = 5
    clip_duration: int = 60
    language: str = "auto"
    subtitle_style: str = "elevate"
    video_start: float | None = None   # interval start (seconds)
    video_end:   float | None = None   # interval end   (seconds)
    face_tracking: bool = False
    smart_zoom:    bool = False


@app.post("/api/process")
async def process_video(request: ProcessRequest, background_tasks: BackgroundTasks):
    """Submit a YouTube URL for processing into TikTok clips."""
    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "status": "queued",
        "progress": 0,
        "message": "En attente de démarrage...",
        "clips": [],
        "error": None,
        "title": None,
    }
    processor = VideoProcessor(
        job_id, jobs, request.openai_api_key,
        subtitle_style=request.subtitle_style,
        face_tracking=request.face_tracking,
        smart_zoom=request.smart_zoom,
    )
    background_tasks.add_task(
        processor.process,
        request.youtube_url,
        request.max_clips,
        request.clip_duration,
        request.language,
        subtitle_style=request.subtitle_style,
        video_start=request.video_start,
        video_end=request.video_end,
        face_tracking=request.face_tracking,
        smart_zoom=request.smart_zoom,
    )
    return {"job_id": job_id}


@app.get("/api/status/{job_id}")
async def get_status(job_id: str):
    """Poll the status and progress of a processing job."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job introuvable")
    return jobs[job_id]


@app.get("/api/download/{job_id}/{clip_index}")
async def download_clip(job_id: str, clip_index: int):
    """Download a generated TikTok clip."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job introuvable")
    job = jobs[job_id]
    if clip_index >= len(job["clips"]):
        raise HTTPException(status_code=404, detail="Clip introuvable")
    clip = job["clips"][clip_index]
    path = clip.get("path", "")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Fichier introuvable")
    return FileResponse(
        path,
        media_type="video/mp4",
        filename=f"tiktok_clip_{clip_index + 1}.mp4",
    )


# ─── Serve frontend (must be last) ──────────────────────────────────────────
frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(frontend_dir):
    app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")
