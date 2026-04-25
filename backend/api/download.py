"""
ClipCut AI — download / thumbnail / trim routes
"""
import io, os, subprocess, zipfile
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse

from state import jobs

router = APIRouter()


# ── Single clip download ──────────────────────────────────────────────────────

@router.get("/download/{job_id}/{clip_index}")
async def download_clip(job_id: str, clip_index: int):
    """Download one TikTok clip as MP4."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job introuvable")
    job = jobs[job_id]
    if clip_index >= len(job.get("clips", [])):
        raise HTTPException(status_code=404, detail="Clip introuvable")
    path = job["clips"][clip_index].get("path", "")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Fichier introuvable")
    return FileResponse(path, media_type="video/mp4",
                        filename=f"tiktok_clip_{clip_index + 1}.mp4")


# ── ZIP download (all clips) ──────────────────────────────────────────────────

@router.get("/download-zip/{job_id}")
async def download_zip(job_id: str):
    """Download all clips for a job as a single ZIP archive."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job introuvable")
    job   = jobs[job_id]
    clips = [c for c in job.get("clips", [])
             if os.path.exists(c.get("path", ""))]
    if not clips:
        raise HTTPException(status_code=404, detail="Aucun clip disponible")

    def zip_generator():
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, mode="w",
                             compression=zipfile.ZIP_DEFLATED) as zf:
            for i, clip in enumerate(clips):
                zf.write(clip["path"], arcname=f"tiktok_clip_{i + 1}.mp4")
        buf.seek(0)
        yield buf.read()

    title_slug = (job.get("title") or "clips").replace(" ", "_")[:40]
    return StreamingResponse(
        zip_generator(),
        media_type="application/zip",
        headers={
            "Content-Disposition":
                f'attachment; filename="{title_slug}_tiktok.zip"'
        },
    )


# ── Thumbnail ─────────────────────────────────────────────────────────────────

@router.get("/thumbnail/{job_id}/{clip_index}")
async def get_thumbnail(job_id: str, clip_index: int):
    """Extract and serve a JPEG thumbnail from the clip (cached on disk)."""
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
             "-vframes", "1", "-q:v", "3", "-vf", "scale=270:480",
             thumb_path],
            capture_output=True,
        )
        if r.returncode != 0 or not os.path.exists(thumb_path):
            raise HTTPException(
                status_code=500,
                detail="Impossible de générer la miniature",
            )
    return FileResponse(thumb_path, media_type="image/jpeg")


# ── Trim (re-cut) ─────────────────────────────────────────────────────────────

@router.post("/trim/{job_id}/{clip_index}")
async def trim_clip(
    job_id:      str,
    clip_index:  int,
    background_tasks: BackgroundTasks,
    trim_start:  float = Query(0.0),
    trim_end:    float = Query(0.0),
):
    """
    Re-cut a clip with adjusted start/end offsets (in seconds).
    The operation runs in the background; poll /api/status for the
    `retrimming` flag on the clip to know when it's done.
    """
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job introuvable")
    job = jobs[job_id]
    if clip_index >= len(job.get("clips", [])):
        raise HTTPException(status_code=404, detail="Clip introuvable")

    clip       = job["clips"][clip_index]
    src        = clip.get("source_video", "")
    orig_start = clip.get("source_start", 0.0)
    orig_end   = clip.get("source_end",   0.0)

    if not src or not os.path.exists(src):
        raise HTTPException(
            status_code=400,
            detail="Vidéo source introuvable (cache manquant ?)",
        )

    new_start = max(0.0, orig_start + trim_start)
    new_end   = max(new_start + 5.0, orig_end + trim_end)

    clip["retrimming"] = True

    clip_path = clip["path"]
    tmp       = clip_path.replace(".mp4", "_trim_tmp.mp4")

    def do_trim():
        r = subprocess.run(
            ["ffmpeg", "-y",
             "-ss", str(new_start), "-to", str(new_end),
             "-i", clip_path,
             "-c:v", "libx264", "-preset", "fast", "-crf", "20",
             "-c:a", "copy", tmp],
            capture_output=True, text=True,
        )
        if r.returncode == 0 and os.path.exists(tmp):
            os.replace(tmp, clip_path)
            clip["duration"]     = round(new_end - new_start, 1)
            clip["source_start"] = new_start
            clip["source_end"]   = new_end
        clip["retrimming"] = False
        # Invalidate cached thumbnail
        thumb = clip_path.replace(".mp4", "_thumb.jpg")
        if os.path.exists(thumb):
            try:
                os.remove(thumb)
            except OSError:
                pass

    background_tasks.add_task(do_trim)
    return {"status": "trimming", "new_start": new_start, "new_end": new_end}
