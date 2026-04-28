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
            cy