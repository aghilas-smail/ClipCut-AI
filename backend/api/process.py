"""
ClipCut AI — /api/process  &  /api/process-batch routes
"""
import os, uuid
from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel
from typing import Optional

from config  import DEFAULT_WHISPER_MODEL, OPENAI_API_KEY
from db      import db_save
from state   import jobs
from core.processor import VideoProcessor

router = APIRouter()


# ── Request models ────────────────────────────────────────────────────────────

class ProcessRequest(BaseModel):
    # URL ou fichier local (l'un ou l'autre)
    youtube_url:      str   = ""
    local_video_path: str   = ""   # rempli par /api/upload
    # Options principales
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
    whisper_model:    str   = DEFAULT_WHISPER_MODEL
    watermark:        str   = ""
    silence_removal:  bool  = False
    add_hook:         bool  = False
    webhook_url:      str   = ""
    visual_enhance:   str   = "none"   # none | auto | vibrant | cinematic | dramatic


class BatchRequest(BaseModel):
    youtube_urls:     list[str]
    max_clips:        int   = 3
    clip_duration:    int   = 60
    language:         str   = "auto"
    subtitle_style:   str   = "elevate"
    whisper_model:    str   = DEFAULT_WHISPER_MODEL
    watermark:        str   = ""
    silence_removal:  bool  = False


# ── Helpers ───────────────────────────────────────────────────────────────────

def _resolve_music(music_track: str) -> Optional[str]:
    """Convert a bare filename → absolute path if the file exists in music/."""
    if not music_track:
        return None
    candidate = os.path.join(os.path.dirname(__file__), "..", "..", "music",
                             music_track)
    candidate = os.path.abspath(candidate)
    return candidate if os.path.exists(candidate) else None


def _blank_job(message: str = "En attente de démarrage...") -> dict:
    from datetime import datetime
    ts = datetime.now().strftime("%H:%M:%S")
    return {
        "status":   "queued",
        "progress": 0,
        "message":  message,
        "clips":    [],
        "error":    None,
        "title":    None,
        "warnings": [],
        "logs":     [f"[{ts}] Job créé, démarrage imminent..."],
    }


async def _run_and_persist(processor: VideoProcessor, *args, **kwargs):
    """Wrapper: runs the processor then persists the final job to SQLite."""
    try:
        await processor.process(*args, **kwargs)
    except Exception as exc:
        import traceback
        from datetime import datetime
        ts  = datetime.now().strftime("%H:%M:%S")
        job = jobs.get(processor.job_id, {})
        job["status"]  = "error"
        job["error"]   = str(exc)
        job["message"] = f"Erreur critique : {exc}"
        job.setdefault("logs", []).append(
            f"[{ts}] ERREUR CRITIQUE (hors process): {exc}\n"
            + traceback.format_exc()[-800:]
        )
    finally:
        job_data = jobs.get(processor.job_id, {})
        if job_data.get("status") in ("completed", "error"):
            db_save(processor.job_id, job_data)


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/process")
async def process_video(request: ProcessRequest,
                        background_tasks: BackgroundTasks):
    """Submit a YouTube URL for processing into TikTok clips."""
    job_id = str(uuid.uuid4())
    jobs[job_id] = _blank_job()

    music_path = _resolve_music(request.music_track)

    if not OPENAI_API_KEY:
        from fastapi import HTTPException
        raise HTTPException(status_code=500,
            detail="Clé API OpenAI non configurée sur le serveur. Ajoutez OPENAI_API_KEY dans backend/.env")

    processor = VideoProcessor(
        job_id, jobs, OPENAI_API_KEY,
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
        visual_enhance  = request.visual_enhance,
    )

    url_or_path = request.local_video_path or request.youtube_url
    background_tasks.add_task(
        _run_and_persist,
        processor,
        url_or_path,
        request.max_clips,
        request.clip_duration,
        request.language,
        subtitle_style    = request.subtitle_style,
        video_start       = request.video_start,
        video_end         = request.video_end,
        face_tracking     = request.face_tracking,
        smart_zoom        = request.smart_zoom,
        subtitle_lang     = request.subtitle_lang,
        music_track       = music_path,
        music_volume      = request.music_volume,
        whisper_model     = request.whisper_model,
        watermark         = request.watermark,
        silence_removal   = request.silence_removal,
        add_hook          = request.add_hook,
        webhook_url       = request.webhook_url,
        visual_enhance    = request.visual_enhance,
        is_local_file     = bool(request.local_video_path),
    )
    return {"job_id": job_id}


@router.post("/process-batch")
async def process_batch(request: BatchRequest,
                        background_tasks: BackgroundTasks):
    """Submit multiple YouTube URLs for batch processing (max 10)."""
    job_ids = []
    for url in request.youtube_urls[:10]:
        url = url.strip()
        if not url:
            continue
        job_id = str(uuid.uuid4())
        jobs[job_id] = _blank_job("En attente (batch)...")
        processor = VideoProcessor(
            job_id, jobs, OPENAI_API_KEY,
            subtitle_style  = request.subtitle_style,
            whisper_model   = request.whisper_model,
            watermark       = request.watermark,
            silence_removal = request.silence_removal,
        )
        background_tasks.add_task(
            _run_and_persist, processor,
            url, request.max_clips, request.clip_duration, request.language,
            subtitle_style  = request.subtitle_style,
            whisper_model   = request.whisper_model,
            watermark       = request.watermark,
            silence_removal = request.silence_removal,
            visual_enhance  = "none",
        )
        job_ids.append({"url": url, "job_id": job_id})
    return {"jobs": job_ids}
