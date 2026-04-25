"""
ClipCut AI -- /api/estimate route
Fast pre-generation time estimate (no download, only yt-dlp metadata).
"""
import os
from fastapi import APIRouter, HTTPException
import yt_dlp

from config import CACHE_DIR, WHISPER_SPEED, fmt_duration, extract_video_id

router = APIRouter()


@router.get("/estimate")
async def estimate_time(
    url:           str,
    whisper_model: str = "base",
    max_clips:     int = 5,
    clip_duration: int = 60,
):
    """
    Fetch video metadata (no download) and return a processing time estimate.
    Also reports YouTube heatmap hot-zones when available.
    """
    try:
        with yt_dlp.YoutubeDL({
            "quiet":         True,
            "skip_download": True,
            "no_warnings":   True,
            "extractor_args": {
                "youtube": {"player_client": ["web", "android"]}
            },
        }) as ydl:
            info      = ydl.extract_info(url, download=False)
            duration  = float(info.get("duration") or 0)
            title     = info.get("title",    "")
            uploader  = info.get("uploader", "")
            thumbnail = info.get("thumbnail", "")
            heatmap   = info.get("heatmap")  or []
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Impossible d'analyser cette URL : {exc}",
        )

    # Is the video already cached?
    video_id = extract_video_id(url)
    cached   = os.path.exists(os.path.join(CACHE_DIR, f"{video_id}.mp4"))

    # Parse heatmap for hot-zone duration
    hot_duration  = 0
    hot_seg_count = 0
    heatmap_available = bool(heatmap)

    if heatmap and duration > 0:
        values  = [float(h.get("value", 0)) for h in heatmap]
        max_val = max(values) if values else 0.0
        if max_val > 0:
            sorted_vals = sorted(values, reverse=True)
            top_n       = max(1, int(len(sorted_vals) * 0.35))
            threshold   = sorted_vals[top_n - 1]
            hot = [
                (float(h.get("start_time", 0)), float(h.get("end_time", 0)))
                for h in heatmap if float(h.get("value", 0)) >= threshold
            ]
            hot.sort()
            if hot:
                merged = [list(hot[0])]
                for s, e in hot[1:]:
                    if s - merged[-1][1] < 30:
                        merged[-1][1] = max(merged[-1][1], e)
                    else:
                        merged.append([s, e])
                hot_duration  = int(sum(e - s for s, e in merged))
                hot_seg_count = len(merged)

    # Estimate breakdown (seconds)
    whisper_f      = WHISPER_SPEED.get(whisper_model, 0.10)
    download       = 8 if cached else 55
    gpt            = 12
    parallelism    = min(max_clips, 3)
    ffmpeg_t       = (max_clips * (clip_duration * 0.65 + 12) / parallelism) * 0.55
    transcribe_dur = hot_duration if hot_duration > 0 else duration
    transcribe     = transcribe_dur * whisper_f
    total_sec      = int(download + transcribe + gpt + ffmpeg_t)

    return {
        "title":             title,
        "uploader":          uploader,
        "thumbnail":         thumbnail,
        "duration":          int(duration),
        "duration_fmt":      fmt_duration(duration),
        "cached":            cached,
        "eta_seconds":       total_sec,
        "eta_fmt":           fmt_duration(total_sec),
        "breakdown": {
            "download":   int(download),
            "transcribe": int(transcribe),
            "gpt":        int(gpt),
            "ffmpeg":     int(ffmpeg_t),
        },
        "whisper_model":     whisper_model,
        "heatmap_available": heatmap_available,
        "hot_duration":      hot_duration,
        "hot_duration_fmt":  fmt_duration(hot_duration) if hot_duration > 0 else None,
        "hot_seg_count":     hot_seg_count,
    }
