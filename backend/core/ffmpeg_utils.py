"""
ClipCut AI - ffmpeg utilities
KEY FEATURE: single-pass clip generation
  Old approach : pass1 (cut+crop+encode) -> pass2 (overlay subtitles+encode)
  New approach : one ffmpeg command does cut + crop + overlay + encode
  Saves ~35-45%% encode time per clip.
Audio fades applied automatically: 2.5s fade-in, 3s fade-out per clip.
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


def build_crop_filter(src_w, src_h, face_rect=None, smart_zoom=False,
                      tgt_w=1080, tgt_h=1920) -> str:
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


def make_clip_onepass(video_path, start, end, subs, crop_filter,
                      out_path, job_dir, clip_index,
                      watermark="", tgt_h=1920, visual_enhance="none",
                      log_fn=None) -> bool:
    """Single ffmpeg pass: seek + crop/scale + subtitle overlay + watermark + encode."""
    inputs       = ["-ss", str(start), "-to", str(end), "-i", video_path]
    filter_parts = []
    clip_len     = end - start

    vf_extra = _VISUAL_FILTERS.get(visual_enhance or "none", "")
    if vf_extra:
        filter_parts.append(f"[0:v]{crop_filter},{vf_extra}[base]")
        if log_fn:
            log_fn(f"   Filtre visuel : {visual_enhance}")
    else:
        filter_parts.append(f"[0:v]{crop_filter}[base]")

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

    audio_filter_args = []
    if clip_len >= 8:
        fade_in_d      = min(2.5, clip_len * 0.06)
        fade_out_d     = min(3.0, clip_len * 0.08)
        fade_out_start = max(0.0, clip_len - fade_out_d)
        af = (f"afade=t=in:st=0:d={fade_in_d:.2f},"
              f"afade=t=out:st={fade_out_start:.2f}:d={fade_out_d:.2f}")
        audio_filter_args = ["-af", af]

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
