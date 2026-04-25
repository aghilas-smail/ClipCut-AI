"""
ClipCut AI — YouTube heatmap parser & hot-segment audio extractor
"""
import os, subprocess


def parse_heatmap(heatmap: list, duration: float, log_fn=None):
    """
    Parse yt-dlp heatmap (list of {start_time, end_time, value}).
    Keeps top 35% by value, merges adjacent segments (<30s gap).
    Returns list of (start, end) tuples or None.
    """
    if not heatmap:
        return None
    values  = [float(h.get("value", 0)) for h in heatmap]
    max_val = max(values) if values else 0.0
    if max_val <= 0:
        return None

    sorted_vals = sorted(values, reverse=True)
    top_n       = max(1, int(len(sorted_vals) * 0.35))
    threshold   = sorted_vals[top_n - 1]

    hot = [
        (float(h.get("start_time", 0)), float(h.get("end_time", 0)))
        for h in heatmap if float(h.get("value", 0)) >= threshold
    ]
    if not hot:
        return None

    hot.sort()
    merged = [list(hot[0])]
    for s, e in hot[1:]:
        if s - merged[-1][1] < 30:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    merged = [(float(s), float(e)) for s, e in merged]

    if log_fn:
        total_hot = sum(e - s for s, e in merged)
        pct       = int(total_hot / duration * 100) if duration > 0 else 0
        log_fn(f"🔥 Heatmap YouTube : {len(merged)} zone(s) populaire(s) détectée(s)")
        log_fn(f"   Durée totale : {total_hot:.0f}s / {duration:.0f}s ({pct}% de la vidéo)")
        for i, (s, e) in enumerate(merged):
            log_fn(f"   Zone {i+1} : {int(s)//60}:{int(s)%60:02d} → "
                   f"{int(e)//60}:{int(e)%60:02d} ({e-s:.0f}s)")
    return merged


def extract_hot_audio(video_path: str, segments: list, job_dir: str, log_fn=None):
    """
    Extract audio from each hot segment, then concatenate.
    Returns (audio_path, offsets) where offsets = [(concat_start, video_start), ...]
    or (None, None) on failure.
    """
    parts, offsets, concat_pos = [], [], 0.0

    for i, (s, e) in enumerate(segments):
        part = os.path.join(job_dir, f"hot_part_{i}.m4a")
        r    = subprocess.run(
            ["ffmpeg", "-y", "-ss", str(s), "-to", str(e),
             "-i", video_path, "-vn", "-c:a", "copy", part],
            capture_output=True
        )
        if r.returncode == 0 and os.path.exists(part):
            parts.append(part)
            offsets.append((concat_pos, s))
            concat_pos += (e - s)
        elif log_fn:
            log_fn(f"   ⚠️ Extraction zone {i+1} échouée")

    if not parts:
        return None, None
    if len(parts) == 1:
        return parts[0], offsets

    concat_out = os.path.join(job_dir, "hot_audio.aac")
    inputs     = []
    for p in parts:
        inputs += ["-i", p]
    flt = "".join(f"[{i}:a]" for i in range(len(parts)))
    flt += f"concat=n={len(parts)}:v=0:a=1[aout]"
    r = subprocess.run(
        ["ffmpeg", "-y"] + inputs + [
            "-filter_complex", flt, "-map", "[aout]",
            "-c:a", "aac", "-b:a", "128k", concat_out
        ],
        capture_output=True
    )
    for p in parts:
        try: os.remove(p)
        except OSError: pass
    if r.returncode == 0 and os.path.exists(concat_out):
        return concat_out, offsets
    return None, None


def remap_hot_timestamps(result: dict, offsets: list) -> dict:
    """Convert timestamps from concatenated audio space → original video time."""
    def remap(t: float) -> float:
        for i, (cs, vs) in enumerate(offsets):
            next_cs = offsets[i + 1][0] if i + 1 < len(offsets) else float("inf")
            if cs <= t < next_cs:
                return t - cs + vs
        if offsets:
            cs, vs = offsets[-1]
            return t - cs + vs
        return t

    for seg in result.get("segments", []):
        seg["start"] = remap(seg["start"])
        seg["end"]   = remap(seg["end"])
        for w in seg.get("words", []):
            w["start"] = remap(w["start"])
            w["end"]   = remap(w["end"])
    return result
