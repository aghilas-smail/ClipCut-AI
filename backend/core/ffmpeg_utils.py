"""
ClipCut AI - ffmpeg utilities
KEY FEATURE: single-pass clip generation
  Old approach : pass1 (cut+crop+encode) -> pass2 (overlay subtitles+encode)
  New approach : one ffmpeg command does cut + crop + overlay + encode
  Saves ~35-45%% encode time per clip.
Audio fades applied automatically: 2.5s fade-in, 3s fade-out per clip.

feat/hostfix1 additions:
  - speed_factor (1/2/3x) : setpts + atempo dans make_clip_onepass
  - detect_face_trajectory : sampling multi-frames pour suivi dynamique
  - build_dynamic_crop_filter : expression crop if() ffmpeg qui suit la trajectoire
"""
import json, os, re, shutil, subprocess


def get_video_duration(video_path: str) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", video_path],
            capture_output=True, text=True, timeout=15
        )
        return float(json.loads(r.stdout).get("format", {}).get("duration", 0))
    except Exception:
        return 0.0


def probe_video_dimensions(video_path: str):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", video_path],
            capture_output=True, text=True, check=True
        )
        for s in json.loads(r.stdout).get("streams", []):
            if s.get("codec_type") == "video":
                return s["width"], s["height"]
    except Exception:
        pass
    return None, None


# ── Legacy static face detection (kept for non-smart_zoom mode) ───────────────

def detect_face_crop(video_path, start, end, src_w, src_h):
    """Read frames from the video (no encode) to find the average face position."""
    try:
        import cv2
        face_cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
        cap  = cv2.VideoCapture(video_path)
        fps  = cap.get(cv2.CAP_PROP_FPS) or 25
        step = max(1, int(fps * 0.5))
        centers = []
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(start * fps))
        frame_no = int(start * fps)
        while frame_no < int(end * fps):
            ret, frame = cap.read()
            if not ret:
                break
            gray  = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = face_cascade.detectMultiScale(
                gray, scaleFactor=1.05, minNeighbors=5, minSize=(60, 60)
            )
            if len(faces):
                x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
                centers.append((x + w // 2, y + h // 2))
            frame_no += step
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_no)
        cap.release()
        if not centers:
            return None
        centers_sorted_x = sorted(c[0] for c in centers)
        centers_sorted_y = sorted(c[1] for c in centers)
        mid = len(centers) // 2
        cx  = centers_sorted_x[mid]
        cy  = centers_sorted_y[mid]
        if src_w / src_h > 9 / 16:
            cw = int(src_h * 9 / 16)
            cx = max(cw // 2, min(cx, src_w - cw // 2))
            return cx - cw // 2, 0, cw, src_h
        else:
            ch = int(src_w * 16 / 9)
            cy = max(ch // 2, min(cy, src_h - ch // 2))
            return 0, cy - ch // 2, src_w, ch
    except Exception:
        return None


# ── Feature 3 : détection de trajectoire multi-frames ─────────────────────────

def detect_face_trajectory(video_path, start, end, src_w, src_h,
                            sample_interval=0.75):
    """
    Sample face positions every `sample_interval` seconds throughout the clip.
    Returns a list of {"t": float, "cx": int, "cy": int} dicts (t = offset from start),
    or None if opencv is unavailable / no faces detected.

    Compared to detect_face_crop (which returns a single median rect), this
    function preserves the temporal evolution so build_dynamic_crop_filter can
    generate a crop expression that follows the subject.
    """
    try:
        import cv2
        face_cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
        cap = cv2.VideoCapture(video_path)
        fps = cap.get(cv2.CAP_PROP_FPS) or 25

        trajectory = []
        t = start
        while t <= end:
            frame_no = int(t * fps)
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_no)
            ret, frame = cap.read()
            if not ret:
                break
            gray  = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = face_cascade.detectMultiScale(
                gray, scaleFactor=1.05, minNeighbors=4, minSize=(50, 50)
            )
            if len(faces):
                x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
                trajectory.append({
                    "t":  round(t - start, 3),
                    "cx": int(x + w // 2),
                    "cy": int(y + h // 2),
                })
            t += sample_interval

        cap.release()
        return trajectory if trajectory else None
    except Exception:
        return None


def _smooth_trajectory(trajectory, window=3):
    """Apply a simple moving average to cx/cy to reduce jitter."""
    if len(trajectory) <= 2:
        return trajectory
    smoothed = []
    for i, pt in enumerate(trajectory):
        lo  = max(0, i - window // 2)
        hi  = min(len(trajectory), i + window // 2 + 1)
        seg = trajectory[lo:hi]
        smoothed.append({
            "t":  pt["t"],
            "cx": int(sum(p["cx"] for p in seg) / len(seg)),
            "cy": int(sum(p["cy"] for p in seg) / len(seg)),
        })
    return smoothed


def _build_if_expr(keyframes, value_key, clamp_lo, clamp_hi):
    """
    Build a nested ffmpeg if(lt(t,T),V,...) expression from a list of keyframes.
    The last keyframe value is the default (innermost else).
    """
    # Sort by time ascending
    kf = sorted(keyframes, key=lambda x: x["t"])
    val = max(clamp_lo, min(clamp_hi, kf[-1][value_key]))
    expr = str(val)
    for pt in reversed(kf[:-1]):
        v = max(clamp_lo, min(clamp_hi, pt[value_key]))
        expr = f"if(lt(t,{pt['t']:.3f}),{v},{expr})"
    return expr


# ── Feature 3 : filtre crop dynamique ─────────────────────────────────────────

def build_dynamic_crop_filter(src_w, src_h, face_trajectory=None,
                               smart_zoom=False,
                               tgt_w=1080, tgt_h=1920) -> str:
    """
    Build a ffmpeg crop+scale filter.

    - Without face_trajectory: centres statiques (comportement original).
    - Avec face_trajectory (liste de dicts {"t","cx","cy"}) et smart_zoom=True:
      génère une expression crop with if(lt(t,...)) pour suivre le visage.
      Un léger zoom-in (1.15x) est aussi appliqué pour renforcer l'effet.
    """
    if not src_w or not src_h:
        return (f"scale={tgt_w}:{tgt_h}:force_original_aspect_ratio=decrease,"
                f"pad={tgt_w}:{tgt_h}:(ow-iw)/2:(oh-ih)/2")

    is_landscape = src_w / src_h > 9 / 16

    # ── Zoom-in factor pour smart_zoom ───────────────────────────────────
    # On réduit la fenêtre de crop de 15% → plus près du sujet
    zoom_factor = 1.15 if smart_zoom and face_trajectory else 1.0

    if is_landscape:
        # Crop horizontal : fenêtre de hauteur = src_h, largeur = src_h*9/16
        crop_w = int(src_h * 9 / 16 / zoom_factor)
        crop_h = int(src_h / zoom_factor)
        crop_y = (src_h - crop_h) // 2  # centre vertical fixe

        if face_trajectory and smart_zoom and len(face_trajectory) >= 2:
            # Lissage trajectoire pour réduire les saccades
            traj = _smooth_trajectory(face_trajectory)
            # Construire les keyframes cx pour l'expression crop x
            kf_x = [{"t": p["t"],
                      "cx": max(crop_w // 2,
                                min(p["cx"] - crop_w // 2, src_w - crop_w))}
                    for p in traj]
            x_expr = _build_if_expr(kf_x, "cx", 0, src_w - crop_w)
            return (f"crop={crop_w}:{crop_h}:{x_expr}:{crop_y},"
                    f"scale={tgt_w}:{tgt_h}:flags=lanczos")
        elif face_trajectory:
            # Un seul point (statique avec zoom-in)
            cx = face_trajectory[0]["cx"]
            cx = max(crop_w // 2, min(cx - crop_w // 2, src_w - crop_w))
            return f"crop={crop_w}:{crop_h}:{cx}:{crop_y},scale={tgt_w}:{tgt_h}:flags=lanczos"
        else:
            # Centre de la vidéo — comportement original
            cx = (src_w - crop_w) // 2
            return f"crop={crop_w}:{crop_h}:{cx}:{crop_y},scale={tgt_w}:{tgt_h}:flags=lanczos"
    else:
        # Vidéo portrait ou carrée : crop vertical
        crop_w = int(src_w / zoom_factor)
        crop_h = int(src_w * 16 / 9 / zoom_factor)
        crop_x = (src_w - crop_w) // 2

        if face_trajectory and smart_zoom and len(face_trajectory) >= 2:
            traj = _smooth_trajectory(face_trajectory)
            kf_y = [{"t": p["t"],
                      "cy": max(crop_h // 2,
                                min(p["cy"] - crop_h // 2, src_h - crop_h))}
                    for p in traj]
            y_expr = _build_if_expr(kf_y, "cy", 0, src_h - crop_h)
            return (f"crop={crop_w}:{crop_h}:{crop_x}:{y_expr},"
                    f"scale={tgt_w}:{tgt_h}:flags=lanczos")
        elif face_trajectory:
            cy = face_trajectory[0]["cy"]
            cy = max(crop_h // 2, min(cy - crop_h // 2, src_h - crop_h))
            return f"crop={crop_w}:{crop_h}:{crop_x}:{cy},scale={tgt_w}:{tgt_h}:flags=lanczos"
        else:
            cy = (src_h - crop_h) // 2
            return f"crop={crop_w}:{crop_h}:{crop_x}:{cy},scale={tgt_w}:{tgt_h}:flags=lanczos"


# ── Legacy wrapper (utilisé quand smart_zoom=False) ───────────────────────────

def build_crop_filter(src_w, src_h, face_rect=None, smart_zoom=False,
                      tgt_w=1080, tgt_h=1920) -> str:
    """Kept for backward compat. Prefer build_dynamic_crop_filter."""
    if face_rect:
        fx, fy, fw, fh = face_rect
        if smart_zoom:
            mx, my = int(fw * 0.075), int(fh * 0.075)
            fx = max(0, fx + mx);  fy = max(0, fy + my)
            fw = min(fw - mx * 2, src_w - fx)
            fh = min(fh - my * 2, src_h - fy)
        return f"crop={fw}:{fh}:{fx}:{fy},scale={tgt_w}:{tgt_h}:flags=lanczos"
    if src_w and src_h:
        if src_w / src_h > 9 / 16:
            nw = int(src_h * 9 / 16)
            cx = (src_w - nw) // 2
            return f"crop={nw}:{src_h}:{cx}:0,scale={tgt_w}:{tgt_h}:flags=lanczos"
        else:
            nh = int(src_w * 16 / 9)
            cy = (src_h - nh) // 2
            return f"crop={src_w}:{nh}:0:{cy},scale={tgt_w}:{tgt_h}:flags=lanczos"
    return (f"scale={tgt_w}:{tgt_h}:force_original_aspect_ratio=decrease,"
            f"pad={tgt_w}:{tgt_h}:(ow-iw)/2:(oh-ih)/2")


_VISUAL_FILTERS = {
    "none":      "",
    "auto":      "eq=brightness=0.04:contrast=1.12:saturation=1.25,unsharp=3:3:0.6:3:3:0",
    "vibrant":   "eq=contrast=1.30:saturation=1.60:brightness=0.05,unsharp=5:5:0.5:5:5:0",
    "cinematic": "eq=gamma=0.92:contrast=1.18:saturation=0.82:brightness=-0.02,"
                 "colorbalance=rs=0.05:gs=-0.02:bs=-0.04:rm=0.03:gm=0:bm=-0.03",
    "dramatic":  "eq=contrast=1.45:saturation=0.70:brightness=-0.04,unsharp=5:5:1.0:5:5:0",
}


def _build_atempo(speed: float) -> str:
    """
    Build a chained atempo filter string for the given speed factor.
    atempo range per filter: [0.5, 100.0] in modern ffmpeg; we chain to be safe
    with older builds that cap at 2.0.
    """
    if speed <= 2.0:
        return f"atempo={speed:.4f}"
    # 3.0 → atempo=2.0,atempo=1.5
    filters = []
    remaining = speed
    while remaining > 2.0:
        filters.append("atempo=2.0")
        remaining /= 2.0
    if remaining > 1.0001:
        filters.append(f"atempo={remaining:.4f}")
    return ",".join(filters)


# ── Feature 2 : make_clip_onepass avec speed_factor ───────────────────────────

def make_clip_onepass(video_path, start, end, subs, crop_filter,
                      out_path, job_dir, clip_index,
                      watermark="", tgt_h=1920, visual_enhance="none",
                      speed_factor=1.0, log_fn=None) -> bool:
    """Single ffmpeg pass: seek + speed + crop/scale + subtitle overlay + watermark + encode.

    speed_factor (float): 1.0 = normal, 2.0 = x2, 3.0 = x3.
      - Video: setpts=PTS/speed (inserted before crop).
      - Audio: atempo=speed (chained for >2x, timestamps subtitle ajustés).
      - Subtitle timings are divided by speed_factor so they stay in sync.
    """
    speed   = max(1.0, float(speed_factor))
    inputs  = ["-ss", str(start), "-to", str(end), "-i", video_path]
    filter_parts = []
    clip_len = (end - start) / speed  # durée effective après accélération

    # ── Vidéo : setpts pour accélération ─────────────────────────────────
    speed_vf = f"setpts=PTS/{speed:.4f}," if speed > 1.0 else ""

    vf_extra = _VISUAL_FILTERS.get(visual_enhance or "none", "")
    if vf_extra:
        filter_parts.append(f"[0:v]{speed_vf}{crop_filter},{vf_extra}[base]")
        if log_fn:
            log_fn(f"   Filtre visuel : {visual_enhance}")
    else:
        filter_parts.append(f"[0:v]{speed_vf}{crop_filter}[base]")

    # Ajuster les timestamps des sous-titres en fonction de la vitesse
    # (enable='between(t,t0,t1)' référence le temps de SORTIE après setpts)
    if speed > 1.0 and subs:
        subs = [
            {**s, "t0": s["t0"] / speed, "t1": s["t1"] / speed}
            for s in subs
        ]

    if subs:
        for sub in subs:
            inputs += ["-i", sub["path"]]
        prev  = "base"
        y_pos = int(tgt_h * 0.68)
        for i, sub in enumerate(subs):
            is_last   = (i == len(subs) - 1) and not watermark
            out_label = "vout" if is_last else f"v{i + 1}"
            t0 = sub['t0']
            t1 = sub['t1']
            filter_parts.append(
                f"[{prev}][{i + 1}:v]"
                f"overlay=(W-w)/2:{y_pos}:"
                f"enable='between(t,{t0:.3f},{t1:.3f})'"
                f"[{out_label}]"
            )
            prev = out_label
        if watermark:
            wm = re.sub(r"[':()\\\\]", "", watermark)[:40]
            filter_parts.append(
                f"[{prev}]drawtext="
                f"text='{wm}':fontsize=42:fontcolor=white@0.80:"
                f"x=w-tw-32:y=h-th-32:"
                f"shadowcolor=black@0.60:shadowx=2:shadowy=2"
                f"[vout]"
            )
    else:
        if watermark:
            wm = re.sub(r"[':()\\\\]", "", watermark)[:40]
            filter_parts.append(
                f"[base]drawtext="
                f"text='{wm}':fontsize=42:fontcolor=white@0.80:"
                f"x=w-tw-32:y=h-th-32:"
                f"shadowcolor=black@0.60:shadowx=2:shadowy=2"
                f"[vout]"
            )
        else:
            filter_parts.append("[base]copy[vout]")

    filter_file = os.path.join(job_dir, f"filter_{clip_index}.txt")
    with open(filter_file, "w", encoding="utf-8") as fh:
        fh.write(";\n".join(filter_parts))

    # ── Audio : fades + atempo ────────────────────────────────────────────
    audio_filters = []
    if clip_len >= 8:
        fade_in_d      = min(2.5, clip_len * 0.06)
        fade_out_d     = min(3.0, clip_len * 0.08)
        fade_out_start = max(0.0, clip_len - fade_out_d)
        audio_filters.append(
            f"afade=t=in:st=0:d={fade_in_d:.2f},"
            f"afade=t=out:st={fade_out_start:.2f}:d={fade_out_d:.2f}"
        )
    if speed > 1.0:
        audio_filters.append(_build_atempo(speed))

    audio_filter_args = ["-af", ",".join(audio_filters)] if audio_filters else []

    try:
        r = subprocess.run(
            ["ffmpeg", "-y"] + inputs + [
                "-filter_complex_script", filter_file,
                "-map", "[vout]", "-map", "0:a",
                "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                "-maxrate", "8M", "-bufsize", "16M",
                "-profile:v", "high", "-level", "4.1",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
            ] + audio_filter_args + [
                "-movflags", "+faststart", out_path,
            ],
            capture_output=True, text=True,
            timeout=300,
        )
        failed = r.returncode != 0
        if failed and log_fn:
            log_fn(f"   ffmpeg error: {r.stderr[-400:]}")
    except subprocess.TimeoutExpired:
        if log_fn:
            log_fn("   ffmpeg timeout (300s) -- encode abandonne")
        failed = True

    for sub in subs:
        try:
            os.remove(sub["path"])
        except OSError:
            pass
    try:
        os.remove(filter_file)
    except OSError:
        pass

    return not failed


def apply_silence_removal(input_path: str, output_path: str) -> bool:
    det = subprocess.run(
        ["ffmpeg", "-i", input_path,
         "-af", "silencedetect=noise=-35dB:d=0.35", "-f", "null", "-"],
        capture_output=True, text=True
    )
    text   = det.stderr + det.stdout
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", text)]
    ends   = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", text)]
    dur_m  = re.search(r"Duration: (\d+):(\d+):([\d.]+)", text)
    if not dur_m or not starts:
        return False
    duration = (int(dur_m.group(1)) * 3600 + int(dur_m.group(2)) * 60
                + float(dur_m.group(3)))
    silences = list(zip(starts,
                        ends if len(ends) >= len(starts) else starts + [duration]))
    keep, prev = [], 0.0
    for s, e in silences:
        if s > prev + 0.15:
            keep.append((prev, s))
        prev = e
    if prev < duration - 0.15:
        keep.append((prev, duration))
    if len(keep) < 2:
        return False

    inputs, fparts = [], []
    for i, (ks, ke) in enumerate(keep):
        inputs.extend(["-ss", f"{ks:.3f}", "-to", f"{ke:.3f}", "-i", input_path])
        fparts.append(f"[{i}:v][{i}:a]")
    concat = "".join(fparts) + f"concat=n={len(keep)}:v=1:a=1[vout][aout]"
    r = subprocess.run(
        ["ffmpeg", "-y"] + inputs + [
            "-filter_complex", concat,
            "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k", output_path
        ],
        capture_output=True, text=True
    )
    return r.returncode == 0


def mix_music(input_path, output_path, clip_duration, music_track, music_volume):
    vol = max(0.05, min(1.0, music_volume))
    r   = subprocess.run(
        ["ffmpeg", "-y",
         "-i", input_path,
         "-stream_loop", "-1", "-i", music_track,
         "-filter_complex",
         f"[1:a]volume={vol:.2f},"
         f"afade=t=out:st={max(0, clip_duration - 1.5):.2f}:d=1.5[music];"
         f"[0:a][music]amix=inputs=2:duration=first:dropout_transition=1[aout]",
         "-map", "0:v", "-map", "[aout]",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
         "-movflags", "+faststart", output_path],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        shutil.copy(input_path, output_path)
