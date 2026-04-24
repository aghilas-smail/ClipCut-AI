"""
ClipCut AI - Video Processor
Subtitles: word-by-word TikTok style via Pillow (cross-platform, no libass).
"""
import asyncio, json, os, re, shutil, subprocess, sys
import yt_dlp, whisper
from openai import OpenAI
from PIL import Image, ImageDraw, ImageFont

OUTPUT_DIR = os.path.join(os.path.expanduser("~"), "ClipCutAI_outputs")
CACHE_DIR  = os.path.join(os.path.expanduser("~"), "ClipCutAI_cache")
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(CACHE_DIR,  exist_ok=True)


def _extract_video_id(url: str) -> str:
    """Extract YouTube video ID from any YouTube URL format."""
    patterns = [
        r"(?:v=|youtu\.be/|embed/|shorts/)([A-Za-z0-9_-]{11})",
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    # Fallback: sanitize full url as filename
    return re.sub(r"[^A-Za-z0-9_-]", "_", url)[-50:]

IS_WINDOWS = sys.platform == "win32"

# ── Font discovery ─────────────────────────────────────────────────────────
def _find_font():
    candidates = [
        # Arial Bold via WSL (Windows fonts mount) — priorité max
        "/mnt/c/Windows/Fonts/arialbd.ttf",
        "/mnt/c/Windows/Fonts/arial.ttf",
        # Windows natif
        r"C:\Windows\Fonts\arialbd.ttf",
        r"C:\Windows\Fonts\arial.ttf",
        # Fonts custom téléchargées
        os.path.expanduser("~/.local/share/fonts/Inter-Black.otf"),
        os.path.expanduser("~/.local/share/fonts/Inter-Black.ttf"),
        os.path.expanduser("~/.local/share/fonts/Montserrat-Bold.ttf"),
        # Fonts système Linux
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None

FONT_FILE = _find_font()

# ── Subtitle PNG renderer ──────────────────────────────────────────────────

def _draw_outlined_text(draw, pos, text, font, fill, outline_color=(0,0,0,255), outline_width=4):
    x, y = pos
    for ox in range(-outline_width, outline_width + 1):
        for oy in range(-outline_width, outline_width + 1):
            if ox != 0 or oy != 0:
                draw.text((x + ox, y + oy), text, font=font, fill=outline_color)
    draw.text((x, y), text, font=font, fill=fill)


def _build_rows(word_info, gap_x, max_row_w):
    """Auto-wrap word_info list into rows fitting max_row_w."""
    rows, cur_row, cur_w = [], [], 0
    for i, wi in enumerate(word_info):
        needed = wi["tw"] + (gap_x if cur_row else 0)
        if cur_row and cur_w + needed > max_row_w:
            rows.append(cur_row)
            cur_row, cur_w = [i], wi["tw"]
        else:
            cur_row.append(i)
            cur_w += needed
    if cur_row:
        rows.append(cur_row)
    return rows


def _measure_words(words, font_normal, font_active, current_idx):
    dummy = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    info  = []
    for i, w in enumerate(words):
        f  = font_active if i == current_idx else font_normal
        bb = dummy.textbbox((0, 0), w, font=f)
        info.append({"word": w, "font": f, "tw": bb[2]-bb[0], "th": bb[3]-bb[1]})
    return info


def render_word_group_png(words_in_group, current_word_idx, out_path,
                          video_w=1080, style="elevate"):
    """
    Render a subtitle PNG. 4 styles:
      elevate  – yellow active word, white others, dark bg (CapCut classic)
      highlight – blue pill behind active word, white text everywhere
      oneword  – single large centered word, yellow on dark pill
      basic    – all white, thin dark bg, no highlight
    """
    MARGIN    = 60
    MAX_ROW_W = video_w - MARGIN * 2
    GAP_X     = 18
    GAP_Y     = 16
    PAD_X     = 30
    PAD_Y     = 18

    # Pas de majuscules — on garde la casse originale
    display_words = list(words_in_group)

    # ── Font size unique (même taille active/inactive) ─────────────────────
    fs = 110 if style == "oneword" else 80

    try:
        font_n = ImageFont.truetype(FONT_FILE, fs) if FONT_FILE else ImageFont.load_default()
        font_a = font_n   # même police, même taille — seule la couleur change
    except Exception:
        font_n = font_a = ImageFont.load_default()

    word_info = _measure_words(display_words, font_n, font_a, current_word_idx)
    rows      = _build_rows(word_info, GAP_X, MAX_ROW_W)

    row_heights = [max(word_info[i]["th"] for i in row) for row in rows]
    total_h     = sum(row_heights) + GAP_Y * (len(rows) - 1) + PAD_Y * 2 + 20

    img  = Image.new("RGBA", (video_w, total_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # ── Background ────────────────────────────────────────────────────────
    if style in ("elevate", "basic", "oneword"):
        row_widths = [sum(word_info[i]["tw"] for i in r) + GAP_X*(len(r)-1) for r in rows]
        bg_w = min(max(row_widths) + PAD_X * 2, video_w - 20)
        bg_x = (video_w - bg_w) // 2
        bg_alpha = 170 if style != "basic" else 140
        draw.rounded_rectangle(
            [bg_x, 10, bg_x + bg_w, 10 + total_h - 10],
            radius=22, fill=(0, 0, 0, bg_alpha)
        )

    # ── Draw rows ─────────────────────────────────────────────────────────
    y = 10 + PAD_Y
    for row_idx, row_indices in enumerate(rows):
        row_w = sum(word_info[i]["tw"] for i in row_indices) + GAP_X * (len(row_indices) - 1)
        x     = (video_w - row_w) // 2
        row_h = row_heights[row_idx]

        for wi_idx in row_indices:
            wi        = word_info[wi_idx]
            is_active = (wi_idx == current_word_idx)
            ty        = y + (row_h - wi["th"]) // 2

            if style == "elevate":
                # Yellow active, white others, thick outline
                color = (255, 224, 0, 255) if is_active else (255, 255, 255, 255)
                _draw_outlined_text(draw, (x, ty), wi["word"], wi["font"], color, outline_width=4)

            elif style == "highlight":
                # Blue pill behind active word, white text everywhere
                if is_active:
                    PAD = 10
                    draw.rounded_rectangle(
                        [x - PAD, ty - PAD//2, x + wi["tw"] + PAD, ty + wi["th"] + PAD//2],
                        radius=10, fill=(59, 130, 246, 230)   # blue
                    )
                _draw_outlined_text(draw, (x, ty), wi["word"], wi["font"],
                                    (255, 255, 255, 255), outline_width=2)

            elif style == "oneword":
                # Single large word, bright yellow, heavy outline
                color = (255, 224, 0, 255)
                _draw_outlined_text(draw, (x, ty), wi["word"], wi["font"], color, outline_width=6)

            elif style == "basic":
                # Simple white, no highlight
                _draw_outlined_text(draw, (x, ty), wi["word"], wi["font"],
                                    (255, 255, 255, 255), outline_width=3)

            x += wi["tw"] + GAP_X

        y += row_h + GAP_Y

    img.save(out_path, "PNG")
    return total_h


# ── VideoProcessor ─────────────────────────────────────────────────────────

class VideoProcessor:
    def __init__(self, job_id, jobs, openai_key, subtitle_style="elevate",
                 face_tracking=False, smart_zoom=False):
        self.job_id         = job_id
        self.jobs           = jobs
        self.openai_key     = openai_key
        self.subtitle_style = subtitle_style
        self.face_tracking  = face_tracking
        self.smart_zoom     = smart_zoom
        self.job_dir        = os.path.join(OUTPUT_DIR, job_id)
        os.makedirs(self.job_dir, exist_ok=True)

    def _update(self, status, progress, message):
        self.jobs[self.job_id].update({"status": status, "progress": progress, "message": message})

    # ── Pipeline ──────────────────────────────────────────────────────────

    async def process(self, youtube_url, max_clips, clip_duration, language,
                      subtitle_style="elevate", video_start=None, video_end=None,
                      face_tracking=False, smart_zoom=False):
        loop = asyncio.get_event_loop()
        try:
            self.subtitle_style = subtitle_style
            self.face_tracking  = face_tracking
            self.smart_zoom     = smart_zoom
            self._update("processing", 5,  "Downloading YouTube video...")
            video_path, title = await loop.run_in_executor(None, self._download, youtube_url)
            self.jobs[self.job_id]["title"] = title

            self._update("processing", 25, "Transcribing with Whisper AI...")
            lang = None if language == "auto" else language
            transcript = await loop.run_in_executor(None, self._transcribe, video_path, lang)

            self._update("processing", 50, "AI analysis: finding best moments...")
            clips_meta = await loop.run_in_executor(
                None, self._select_moments, transcript, max_clips, clip_duration, video_start, video_end)

            clips = []
            total = len(clips_meta)
            for i, (start, end, clip_title) in enumerate(clips_meta):
                pct = 60 + int(i * 35 / max(total, 1))
                self._update("processing", pct, f"Generating clip {i+1}/{total}: {clip_title[:40]}...")
                clip_path = await loop.run_in_executor(
                    None, self._make_tiktok_clip, video_path, transcript, start, end, i)
                clips.append({"index": i, "title": clip_title, "start": start,
                               "end": end, "duration": round(end - start, 1), "path": clip_path})

            self.jobs[self.job_id]["clips"] = clips
            self._update("completed", 100, f"{total} TikTok clips ready!")
        except Exception as exc:
            self.jobs[self.job_id]["error"] = str(exc)
            self._update("error", 0, f"Error: {exc}")

    # ── Download ──────────────────────────────────────────────────────────

    def _download(self, url):
        video_id   = _extract_video_id(url)
        cache_path = os.path.join(CACHE_DIR, f"{video_id}.mp4")

        # Use cached version if it already exists
        if os.path.exists(cache_path) and os.path.getsize(cache_path) > 100_000:
            self._update("processing", 10, "Video found in cache, skipping download...")
            # Still need the title — fetch metadata only (no download)
            try:
                with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True}) as ydl:
                    meta = ydl.extract_info(url, download=False)
                    title = meta.get("title", "Untitled")
            except Exception:
                title = video_id
            return cache_path, title

        # Download fresh — prioritise 1080p
        ydl_opts = {
            "format": (
                "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]"
                "/bestvideo[height<=1080]+bestaudio"
                "/bestvideo[ext=mp4]+bestaudio[ext=m4a]"
                "/bestvideo+bestaudio"
                "/best"
            ),
            "outtmpl": cache_path,
            "merge_output_format": "mp4",
            "quiet": True, "no_warnings": True,
        }
        title = "Untitled"
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            meta = ydl.extract_info(url, download=True)
            title = meta.get("title", "Untitled")
        return cache_path, title

    # ── Transcribe ────────────────────────────────────────────────────────

    def _transcribe(self, video_path, language):
        model = whisper.load_model("base")
        kwargs = {"word_timestamps": True}
        if language:
            kwargs["language"] = language
        return model.transcribe(video_path, **kwargs)

    # ── AI moment selection ───────────────────────────────────────────────

    def _select_moments(self, transcript, max_clips, clip_duration, video_start=None, video_end=None):
        MIN_DURATION = max(10, clip_duration // 4)
        segments_info = [
            {"start": round(s["start"], 1), "end": round(s["end"], 1), "text": s["text"].strip()}
            for s in transcript["segments"]
            if (video_start is None or s["end"] >= video_start)
            and (video_end   is None or s["start"] <= video_end)
        ]
        client = OpenAI(api_key=self.openai_key)
        prompt = f"""You are a TikTok viral content expert.
Select exactly {max_clips} clips. Min {MIN_DURATION}s, Max {clip_duration}s. Extend if too short. Natural sentence boundaries. No overlap.
Transcript: {json.dumps(segments_info, ensure_ascii=False)}
Reply ONLY with JSON: {{"clips": [{{"start": 12.5, "end": 48.3, "title": "Title"}}]}}"""
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}, temperature=0.7,
        )
        result = []
        for c in json.loads(response.choices[0].message.content).get("clips", [])[:max_clips]:
            s, e, t = c["start"], c["end"], c["title"]
            if e - s < MIN_DURATION: e = s + MIN_DURATION
            if e - s > clip_duration: e = s + clip_duration
            result.append((s, e, t))
        return result

    # ── Face detection ────────────────────────────────────────────────────

    def _detect_face_crop(self, video_path, start, end, src_w, src_h):
        """
        Sample frames in [start, end], detect the dominant face with OpenCV,
        return (crop_x, crop_y, crop_w, crop_h) for a 9:16 crop centred on face.
        Returns None if OpenCV is unavailable or no face found.
        """
        try:
            import cv2
            cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            face_cascade = cv2.CascadeClassifier(cascade_path)

            cap = cv2.VideoCapture(video_path)
            fps = cap.get(cv2.CAP_PROP_FPS) or 25
            step = max(1, int(fps * 1.0))   # sample every ~1 second

            centers = []
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(start * fps))
            frame_no = int(start * fps)
            while frame_no < int(end * fps):
                ret, frame = cap.read()
                if not ret:
                    break
                gray  = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                faces = face_cascade.detectMultiScale(gray, 1.1, 4, minSize=(40, 40))
                if len(faces):
                    x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
                    centers.append((x + w // 2, y + h // 2))
                frame_no += step
                cap.set(cv2.CAP_PROP_POS_FRAMES, frame_no)
            cap.release()

            if not centers:
                return None

            cx = int(sum(c[0] for c in centers) / len(centers))
            cy = int(sum(c[1] for c in centers) / len(centers))

            # Build 9:16 crop centred on detected face
            ratio = 9 / 16
            if src_w / src_h > ratio:
                cw = int(src_h * ratio)
                ch = src_h
                cx = max(cw // 2, min(cx, src_w - cw // 2))
                return cx - cw // 2, 0, cw, ch
            else:
                cw = src_w
                ch = int(src_w / ratio)
                cy = max(ch // 2, min(cy, src_h - ch // 2))
                return 0, cy - ch // 2, cw, ch

        except Exception:
            return None

    # ── Clip generation ───────────────────────────────────────────────────

    def _make_tiktok_clip(self, video_path, transcript, start, end, index):
        clip_path = os.path.abspath(os.path.join(self.job_dir, f"clip_{index}.mp4"))
        temp_path = os.path.abspath(os.path.join(self.job_dir, f"temp_{index}.mp4"))

        # Probe source
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", video_path],
            capture_output=True, text=True, check=True)
        src_w = src_h = None
        for s in json.loads(probe.stdout).get("streams", []):
            if s.get("codec_type") == "video":
                src_w, src_h = s["width"], s["height"]; break

        tgt_w, tgt_h = 1080, 1920

        # ── Face tracking + smart zoom ─────────────────────────────────────
        face_rect = None
        if self.face_tracking and src_w and src_h:
            self._update("processing", None, f"Detecting faces for clip {index+1}...")
            face_rect = self._detect_face_crop(video_path, start, end, src_w, src_h)

        if face_rect:
            fx, fy, fw, fh = face_rect
            if self.smart_zoom:
                # Crop tighter (85% of area) around the face for a zoom effect
                margin_x = int(fw * 0.075)
                margin_y = int(fh * 0.075)
                fx = max(0, fx + margin_x)
                fy = max(0, fy + margin_y)
                fw = min(fw - margin_x * 2, src_w - fx)
                fh = min(fh - margin_y * 2, src_h - fy)
            crop = f"crop={fw}:{fh}:{fx}:{fy},scale={tgt_w}:{tgt_h}"
        elif src_w and src_h:
            if (src_w / src_h) > (9 / 16):
                nw = int(src_h * 9 / 16)
                cx = (src_w - nw) // 2
                crop = f"crop={nw}:{src_h}:{cx}:0,scale={tgt_w}:{tgt_h}"
            else:
                nh = int(src_w * 16 / 9)
                cy = (src_h - nh) // 2
                crop = f"crop={src_w}:{nh}:0:{cy},scale={tgt_w}:{tgt_h}"
        else:
            crop = f"scale={tgt_w}:{tgt_h}:force_original_aspect_ratio=decrease,pad={tgt_w}:{tgt_h}:(ow-iw)/2:(oh-ih)/2"

        # Pass 1: cut + crop — haute qualité 1080x1920
        r1 = subprocess.run(
            ["ffmpeg", "-y", "-ss", str(start), "-to", str(end), "-i", video_path,
             "-vf", crop,
             "-c:v", "libx264", "-preset", "slow", "-crf", "18",
             "-profile:v", "high", "-level", "4.1",
             "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
             temp_path],
            capture_output=True, text=True)
        if r1.returncode != 0:
            raise RuntimeError(f"ffmpeg pass 1 failed: {r1.stderr[-800:]}")

        # Pass 2: word-by-word subtitle overlay
        words = self._extract_words(transcript, start, end)
        subs  = self._render_word_by_word_pngs(words, start, index, tgt_w)

        if subs:
            ok = self._overlay_subtitles(temp_path, clip_path, subs, tgt_h, index)
            if not ok:
                shutil.copy(temp_path, clip_path)
        else:
            shutil.copy(temp_path, clip_path)

        try: os.remove(temp_path)
        except OSError: pass
        return clip_path

    # ── Word extraction ───────────────────────────────────────────────────

    def _extract_words(self, transcript, start, end):
        words = []
        for seg in transcript.get("segments", []):
            if seg["end"] < start or seg["start"] > end:
                continue
            for w in seg.get("words", []):
                ws, we = w.get("start", 0), w.get("end", 0)
                if ws >= start and we <= end + 0.5:
                    words.append({"word": w["word"].strip(), "start": ws, "end": we})
        return words

    # ── Word-by-word PNG generation ───────────────────────────────────────

    def _render_word_by_word_pngs(self, words, clip_start, clip_index, video_w):
        """
        One PNG per WORD — karaoke effect.
        For 'oneword' style: each PNG shows only 1 large word.
        For other styles: each PNG shows the group with the active word highlighted.
        """
        if not words:
            return []

        style = self.subtitle_style
        WORDS_PER_GROUP = 1 if style == "oneword" else 4
        groups = [words[i: i + WORDS_PER_GROUP] for i in range(0, len(words), WORDS_PER_GROUP)]

        result = []
        for g_idx, group in enumerate(groups):
            group_words = [w["word"] for w in group]

            for w_idx, word in enumerate(group):
                t0 = max(0.0, word["start"] - clip_start)
                if w_idx < len(group) - 1:
                    t1 = max(t0 + 0.05, group[w_idx + 1]["start"] - clip_start)
                elif g_idx < len(groups) - 1:
                    t1 = max(t0 + 0.05, groups[g_idx + 1][0]["start"] - clip_start)
                else:
                    t1 = max(t0 + 0.05, word["end"] - clip_start)

                png_path = os.path.join(self.job_dir, f"sub_{clip_index}_g{g_idx}_w{w_idx}.png")
                try:
                    h = render_word_group_png(group_words, w_idx, png_path, video_w, style=style)
                    result.append({"path": png_path, "t0": t0, "t1": t1, "h": h})
                except Exception:
                    pass
        return result

    # ── ffmpeg overlay ────────────────────────────────────────────────────

    def _overlay_subtitles(self, temp_path, clip_path, subs, video_h, clip_index):
        inputs = ["-i", temp_path]
        for sub in subs:
            inputs += ["-i", sub["path"]]

        filter_parts = []
        prev = "0:v"
        for i, sub in enumerate(subs):
            y_pos = int(video_h * 0.68)
            out_label = "vout" if i == len(subs) - 1 else f"v{i+1}"
            t0, t1 = sub["t0"], sub["t1"]
            filter_parts.append(
                f"[{prev}][{i+1}:v]"
                f"overlay=(W-w)/2:{y_pos}:"
                f"enable='between(t,{t0:.3f},{t1:.3f})'"
                f"[{out_label}]"
            )
            prev = out_label

        filter_file = os.path.join(self.job_dir, f"filter_{clip_index}.txt")
        with open(filter_file, "w", encoding="utf-8") as fh:
            fh.write(";\n".join(filter_parts))

        r = subprocess.run(
            ["ffmpeg", "-y"] + inputs + [
                "-filter_complex_script", filter_file,
                "-map", "[vout]", "-map", "0:a",
                "-c:v", "libx264", "-preset", "slow", "-crf", "18",
                "-profile:v", "high", "-level", "4.1",
                "-c:a", "copy", "-movflags", "+faststart", clip_path,
            ],
            capture_output=True, text=True)

        for sub in subs:
            try: os.remove(sub["path"])
            except OSError: pass
        try: os.remove(filter_file)
        except OSError: pass

        if r.returncode != 0:
            entry = self.jobs.get(self.job_id, {})
            warns = entry.get("warnings", [])
            warns.append(f"[overlay error clip {clip_index}] {r.stderr[-500:]}")
            entry["warnings"] = warns
            return False
        return True
