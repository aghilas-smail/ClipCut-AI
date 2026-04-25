"""
ClipCut AI v2 - Video Processor
Subtitles: word-by-word TikTok style via Pillow (cross-platform, no libass).
New in v2: viral score, AI captions, silence removal (jump cuts),
           hook intro overlay, watermark, Whisper model choice, webhook.
"""
import asyncio, json, os, re, shutil, subprocess, sys, time, urllib.request
import yt_dlp
from openai import OpenAI
from PIL import Image, ImageDraw, ImageFont

# faster-whisper real-time factor on CPU (int8 quantized)
# ~4-8x faster than openai-whisper
WHISPER_SPEED = {
    "tiny":   0.04,
    "base":   0.10,
    "small":  0.22,
    "medium": 0.50,
    "large":  1.00,
}

# ── Whisper model cache ────────────────────────────────────────────────────
# Models are heavy (base=~150MB, medium=~1.5GB). Keeping one in RAM avoids
# a ~5-10s reload on every job. We keep at most 1 model at a time to cap RAM.
_WHISPER_CACHE: dict = {}   # { model_name: WhisperModel_instance }
_WHISPER_CACHE_MAX = 1      # increase to 2 if you want base+medium both hot

def _fmt_duration(seconds: float) -> str:
    """Format seconds as 'Xmin Ys' or 'Xs'."""
    s = int(max(0, seconds))
    m = s // 60
    return f"{m}min {s % 60}s" if m else f"{s}s"

OUTPUT_DIR = os.path.join(os.path.expanduser("~"), "ClipCutAI_outputs")
CACHE_DIR  = os.path.join(os.path.expanduser("~"), "ClipCutAI_cache")
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(CACHE_DIR,  exist_ok=True)


def _extract_video_id(url: str) -> str:
    patterns = [r"(?:v=|youtu\.be/|embed/|shorts/)([A-Za-z0-9_-]{11})"]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return re.sub(r"[^A-Za-z0-9_-]", "_", url)[-50:]


IS_WINDOWS = sys.platform == "win32"

# ── Font discovery ─────────────────────────────────────────────────────────
def _find_font():
    candidates = [
        "/mnt/c/Windows/Fonts/arialbd.ttf",
        "/mnt/c/Windows/Fonts/arial.ttf",
        r"C:\Windows\Fonts\arialbd.ttf",
        r"C:\Windows\Fonts\arial.ttf",
        os.path.expanduser("~/.local/share/fonts/Inter-Black.otf"),
        os.path.expanduser("~/.local/share/fonts/Inter-Black.ttf"),
        os.path.expanduser("~/.local/share/fonts/Montserrat-Bold.ttf"),
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
    try:
        asc, desc = font_normal.getmetrics()
    except Exception:
        asc, desc = 60, 10
    line_h = asc + abs(desc)
    info = []
    for i, w in enumerate(words):
        f  = font_active if i == current_idx else font_normal
        bb = dummy.textbbox((0, 0), w, font=f)
        info.append({
            "word": w, "font": f,
            "tw": bb[2] - bb[0], "th": line_h,
            "bb1": bb[1], "bb3": bb[3],
        })
    return info


def render_word_group_png(words_in_group, current_word_idx, out_path,
                          video_w=1080, style="elevate"):
    MARGIN    = 60
    MAX_ROW_W = video_w - MARGIN * 2
    GAP_X     = 18
    GAP_Y     = 16
    PAD_X     = 30
    PAD_Y     = 18

    display_words = list(words_in_group)
    fs = 110 if style == "oneword" else 80

    try:
        font_n = ImageFont.truetype(FONT_FILE, fs) if FONT_FILE else ImageFont.load_default()
        font_a = font_n
    except Exception:
        font_n = font_a = ImageFont.load_default()

    word_info = _measure_words(display_words, font_n, font_a, current_word_idx)
    rows      = _build_rows(word_info, GAP_X, MAX_ROW_W)

    row_heights = [max(word_info[i]["th"] for i in row) for row in rows]
    total_h     = sum(row_heights) + GAP_Y * (len(rows) - 1) + PAD_Y * 2 + 20

    img  = Image.new("RGBA", (video_w, total_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # No dark background — rely on text outline (real TikTok style)
    if style == "highlight":
        pass

    y = 10 + PAD_Y
    for row_idx, row_indices in enumerate(rows):
        row_w = sum(word_info[i]["tw"] for i in row_indices) + GAP_X * (len(row_indices) - 1)
        x     = (video_w - row_w) // 2
        row_h = row_heights[row_idx]

        baseline_y = y + row_h - abs(word_info[row_indices[0]]["bb3"])

        for wi_idx in row_indices:
            wi        = word_info[wi_idx]
            is_active = (wi_idx == current_word_idx)

            glyph_top    = baseline_y + wi["bb1"]
            glyph_bottom = baseline_y + wi["bb3"]

            if style == "elevate":
                color = (255, 224, 0, 255) if is_active else (255, 255, 255, 255)
                _draw_outlined_text(draw, (x, baseline_y), wi["word"], wi["font"], color, outline_width=4)

            elif style == "highlight":
                if is_active:
                    HPAD_X, HPAD_Y = 14, 8
                    draw.rounded_rectangle(
                        [x - HPAD_X, glyph_top - HPAD_Y,
                         x + wi["tw"] + HPAD_X, glyph_bottom + HPAD_Y],
                        radius=10, fill=(59, 130, 246, 230)
                    )
                _draw_outlined_text(draw, (x, baseline_y), wi["word"], wi["font"],
                                    (255, 255, 255, 255), outline_width=2)

            elif style == "oneword":
                color = (255, 224, 0, 255)
                _draw_outlined_text(draw, (x, baseline_y), wi["word"], wi["font"], color, outline_width=6)

            elif style == "basic":
                _draw_outlined_text(draw, (x, baseline_y), wi["word"], wi["font"],
                                    (255, 255, 255, 255), outline_width=3)

            x += wi["tw"] + GAP_X

        y += row_h + GAP_Y

    img.save(out_path, "PNG")
    return total_h


def render_hook_png(text, out_path, video_w=1080):
    """Render a large hook intro text PNG (shown for first 2s of clip)."""
    PAD_X, PAD_Y = 40, 24
    fs = 78
    try:
        font = ImageFont.truetype(FONT_FILE, fs) if FONT_FILE else ImageFont.load_default()
    except Exception:
        font = ImageFont.load_default()

    dummy = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    # Word wrap
    words = text.split()
    lines, cur = [], []
    for w in words:
        test = " ".join(cur + [w])
        bb = dummy.textbbox((0, 0), test, font=font)
        if bb[2] - bb[0] > video_w - 80 and cur:
            lines.append(" ".join(cur))
            cur = [w]
        else:
            cur.append(w)
    if cur:
        lines.append(" ".join(cur))

    try:
        asc, desc = font.getmetrics()
    except Exception:
        asc, desc = 60, 10
    line_h = asc + abs(desc)
    total_h = line_h * len(lines) + 16 * (len(lines) - 1) + PAD_Y * 2 + 20

    img  = Image.new("RGBA", (video_w, total_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Dark pill background
    row_widths = [dummy.textbbox((0, 0), l, font=font)[2] for l in lines]
    bg_w = min(max(row_widths) + PAD_X * 2, video_w - 20)
    bg_x = (video_w - bg_w) // 2
    draw.rounded_rectangle([bg_x, 10, bg_x + bg_w, total_h - 10],
                            radius=24, fill=(0, 0, 0, 185))

    y = 10 + PAD_Y
    for line in lines:
        bb = dummy.textbbox((0, 0), line, font=font)
        x  = (video_w - (bb[2] - bb[0])) // 2
        baseline_y = y + line_h - abs(font.getmetrics()[1])
        _draw_outlined_text(draw, (x, baseline_y), line, font,
                            (255, 224, 0, 255), outline_width=5)
        y += line_h + 16

    img.save(out_path, "PNG")
    return total_h


# ── VideoProcessor ─────────────────────────────────────────────────────────

class VideoProcessor:
    def __init__(self, job_id, jobs, openai_key,
                 subtitle_style="elevate",
                 face_tracking=False, smart_zoom=False,
                 music_track=None, music_volume=0.15,
                 whisper_model="base",
                 watermark="",
                 silence_removal=False,
                 add_hook=False,
                 webhook_url=""):
        self.job_id          = job_id
        self.jobs            = jobs
        self.openai_key      = openai_key
        self.subtitle_style  = subtitle_style
        self.face_tracking   = face_tracking
        self.smart_zoom      = smart_zoom
        self.music_track     = music_track
        self.music_volume    = music_volume
        self.whisper_model   = whisper_model
        self.watermark       = watermark
        self.silence_removal = silence_removal
        self.add_hook        = add_hook
        self.webhook_url     = webhook_url
        self.job_dir         = os.path.join(OUTPUT_DIR, job_id)
        os.makedirs(self.job_dir, exist_ok=True)

    def _log(self, message: str):
        """Append a timestamped entry to the job's live log."""
        from datetime import datetime
        job  = self.jobs[self.job_id]
        logs = job.setdefault("logs", [])
        ts   = datetime.now().strftime("%H:%M:%S")
        logs.append(f"[{ts}] {message}")
        if len(logs) > 300:
            job["logs"] = logs[-300:]

    def _update(self, status, progress, message):
        job = self.jobs[self.job_id]
        job["status"]   = status
        job["message"]  = message
        if progress is not None:
            job["progress"] = progress
        self._log(message)

    # ── Pipeline ──────────────────────────────────────────────────────────

    async def process(self, youtube_url, max_clips, clip_duration, language,
                      subtitle_style="elevate", video_start=None, video_end=None,
                      face_tracking=False, smart_zoom=False, subtitle_lang=None,
                      music_track=None, music_volume=0.15,
                      whisper_model="base", watermark="",
                      silence_removal=False, add_hook=False, webhook_url=""):
        loop = asyncio.get_event_loop()
        try:
            self.subtitle_style  = subtitle_style
            self.face_tracking   = face_tracking
            self.smart_zoom      = smart_zoom
            self.music_track     = music_track
            self.music_volume    = music_volume
            self.whisper_model   = whisper_model
            self.watermark       = watermark
            self.silence_removal = silence_removal
            self.add_hook        = add_hook
            self.webhook_url     = webhook_url

            self._update("processing", 5, "Téléchargement de la vidéo YouTube...")
            self._log("yt-dlp : connexion à YouTube...")
            video_path, title = await loop.run_in_executor(None, self._download, youtube_url)
            self.jobs[self.job_id]["title"] = title
            self._log(f"Vidéo téléchargée : «{title}»")

            # Probe duration → compute initial ETA
            dur    = self._get_video_duration(video_path)
            cached = "(cache)" in self.jobs[self.job_id].get("message", "")
            dur_label = ""
            if dur > 0:
                m, s = int(dur // 60), int(dur % 60)
                dur_label = f" — vidéo de {m}min {s}s"
                eta_sec = self._estimate_eta(dur, max_clips, clip_duration, cached)
                self.jobs[self.job_id]["eta_seconds"] = eta_sec
                self.jobs[self.job_id]["started_at"]  = time.time()
                self._log(
                    f"Durée vidéo : {m}min {s}s | "
                    f"Modèle : Whisper {whisper_model} | "
                    f"ETA initial : ~{_fmt_duration(eta_sec)}"
                )

            # ── Parse YouTube heatmap (Most Replayed) ─────────────────────
            raw_heatmap = getattr(self, "_raw_heatmap", [])
            hot_segs    = None
            if raw_heatmap and dur > 0:
                self._log(f"Analyse heatmap YouTube ({len(raw_heatmap)} points de données)...")
                hot_segs = self._parse_heatmap(raw_heatmap, dur)
            else:
                self._log("Heatmap YouTube non disponible pour cette vidéo")

            self._hot_segments = hot_segs
            self.jobs[self.job_id]["heatmap_active"]  = bool(hot_segs)
            self.jobs[self.job_id]["hot_segment_count"] = len(hot_segs) if hot_segs else 0

            # Recalculate ETA using hot duration if heatmap available
            if hot_segs and dur > 0:
                hot_dur = sum(e - s for s, e in hot_segs)
                self.jobs[self.job_id]["hot_duration"] = int(hot_dur)
                eta_sec = self._estimate_eta_hot(dur, hot_dur, max_clips, clip_duration, cached)
                self.jobs[self.job_id]["eta_seconds"]  = eta_sec
                self._log(
                    f"⚡ Heatmap activée — transcription réduite : "
                    f"{dur:.0f}s → {hot_dur:.0f}s ({int(hot_dur/dur*100)}%) | "
                    f"Nouveau ETA : ~{_fmt_duration(eta_sec)}"
                )

            # Store manual interval (video_start/video_end) — lower priority than heatmap
            self._partial_start = video_start if not hot_segs else None
            self._partial_end   = video_end   if not hot_segs else None
            if video_start is not None and video_end is not None and not hot_segs:
                self._log(f"Intervalle manuel défini : transcription limitée à {video_end - video_start:.0f}s")

            self._update("processing", 20,
                         f"Transcription faster-whisper ({whisper_model}){dur_label}...")
            self._log(f"Chargement du modèle Whisper «{whisper_model}» (faster-whisper, int8)...")
            lang = None if language == "auto" else language
            transcript = await loop.run_in_executor(None, self._transcribe, video_path, lang)
            seg_count = len(transcript.get("segments", []))
            self._log(f"Transcription terminée : {seg_count} segments détectés")

            if subtitle_lang and subtitle_lang != "original":
                self._update("processing", 35, f"Traduction des sous-titres en {subtitle_lang}...")
                self._log(f"GPT-4o mini : traduction vers {subtitle_lang}...")
                transcript = await loop.run_in_executor(
                    None, self._translate_transcript, transcript, subtitle_lang)
                self._log("Traduction terminée")

            self._update("processing", 45, "Analyse IA : détection des meilleurs moments...")
            self._log(f"GPT-4o mini : sélection de {max_clips} moment(s) viraux (max {clip_duration}s)...")
            clips_meta = await loop.run_in_executor(
                None, self._select_moments, transcript, max_clips, clip_duration,
                video_start, video_end, self._hot_segments)
            self._log(f"{len(clips_meta)} moment(s) sélectionné(s) par l'IA")
            for i, (s, e, t, sc) in enumerate(clips_meta):
                self._log(f"  Clip {i+1}: «{t[:50]}» [{s:.1f}s → {e:.1f}s] score={sc}/10")

            self._update("processing", 55, "Génération des captions TikTok...")
            self._log("GPT-4o mini : génération des captions + hashtags...")
            captions = await loop.run_in_executor(
                None, self._generate_captions, clips_meta)
            self._log(f"{len(captions)} caption(s) générée(s)")

            clips  = []
            total  = len(clips_meta)
            for i, (start, end, clip_title, score) in enumerate(clips_meta):
                pct = 60 + int(i * 35 / max(total, 1))
                self._update("processing", pct,
                             f"Génération du clip {i+1}/{total} : {clip_title[:40]}...")
                clip_path = await loop.run_in_executor(
                    None, self._make_tiktok_clip,
                    video_path, transcript, start, end, i)
                clips.append({
                    "index":    i,
                    "title":    clip_title,
                    "start":    start,
                    "end":      end,
                    "duration": round(end - start, 1),
                    "path":     clip_path,
                    "score":    score,
                    "caption":  captions[i] if i < len(captions) else "",
                    # stored for trim endpoint
                    "source_video": video_path,
                    "source_start": start,
                    "source_end":   end,
                })

            self.jobs[self.job_id]["clips"] = clips
            self._update("completed", 100, f"{total} clips TikTok prêts !")

            # Fire webhook if set
            if self.webhook_url:
                self._fire_webhook(self.webhook_url, self.job_id, total)

        except Exception as exc:
            import traceback
            self.jobs[self.job_id]["error"] = str(exc)
            self._update("error", 0, f"Erreur : {exc}")

    # ── ETA estimation ────────────────────────────────────────────────────

    def _estimate_eta(self, video_duration: float, num_clips: int,
                      clip_duration: int, cached: bool) -> int:
        """
        Estimate total processing time in seconds.
        Components:
          - Download      : ~10s if cached, ~60s otherwise
          - Transcription : video_duration × whisper_factor
          - GPT calls     : ~12s (selection + captions)
          - ffmpeg / clip : clip_duration × 0.65 + 12s per clip
        """
        download   = 8  if cached else 55
        whisper_f  = WHISPER_SPEED.get(self.whisper_model, 0.10)
        transcribe = video_duration * whisper_f
        gpt        = 12
        ffmpeg     = num_clips * (clip_duration * 0.65 + 12)
        return int(download + transcribe + gpt + ffmpeg)

    def _estimate_eta_hot(self, video_duration: float, hot_duration: float,
                          num_clips: int, clip_duration: int, cached: bool) -> int:
        """Same as _estimate_eta but transcription uses only hot_duration."""
        download   = 8  if cached else 55
        whisper_f  = WHISPER_SPEED.get(self.whisper_model, 0.10)
        transcribe = hot_duration * whisper_f
        gpt        = 12
        ffmpeg     = num_clips * (clip_duration * 0.65 + 12)
        return int(download + transcribe + gpt + ffmpeg)

    # ── Heatmap parsing & hot-segment extraction ──────────────────────────

    def _parse_heatmap(self, heatmap: list, duration: float):
        """
        Parse yt-dlp heatmap (list of {start_time, end_time, value}).
        Keeps the top 35% most-viewed segments, merges adjacent ones (<30s gap).
        Returns list of (start, end) tuples, or None if unavailable/useless.
        """
        if not heatmap:
            return None

        values = [float(h.get("value", 0)) for h in heatmap]
        max_val = max(values) if values else 0.0
        if max_val <= 0:
            return None

        # Threshold = top 35% by value
        sorted_vals = sorted(values, reverse=True)
        top_n = max(1, int(len(sorted_vals) * 0.35))
        threshold = sorted_vals[top_n - 1]

        hot = []
        for h in heatmap:
            if float(h.get("value", 0)) >= threshold:
                s = float(h.get("start_time", 0))
                e = float(h.get("end_time", s + 10))
                hot.append((s, e))

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

        total_hot = sum(e - s for s, e in merged)
        pct = int(total_hot / duration * 100) if duration > 0 else 0
        self._log(f"🔥 Heatmap YouTube : {len(merged)} zone(s) populaire(s) détectée(s)")
        self._log(f"   Durée totale : {total_hot:.0f}s / {duration:.0f}s ({pct}% de la vidéo)")
        for i, (s, e) in enumerate(merged):
            self._log(f"   Zone {i+1} : {int(s)//60}:{int(s)%60:02d} → {int(e)//60}:{int(e)%60:02d} ({e-s:.0f}s)")
        return merged

    def _extract_hot_audio(self, video_path: str, segments: list):
        """
        Extract audio from each hot segment and concatenate them.
        Returns (audio_path, offsets) where offsets = [(concat_start, video_start), ...]
        Returns (None, None) on failure.
        """
        parts   = []
        offsets = []
        concat_pos = 0.0

        for i, (s, e) in enumerate(segments):
            part = os.path.join(self.job_dir, f"hot_part_{i}.m4a")
            r = subprocess.run(
                ["ffmpeg", "-y",
                 "-ss", str(s), "-to", str(e),
                 "-i", video_path,
                 "-vn", "-c:a", "copy", part],
                capture_output=True
            )
            if r.returncode == 0 and os.path.exists(part):
                parts.append(part)
                offsets.append((concat_pos, s))
                concat_pos += (e - s)
            else:
                self._log(f"   ⚠️ Extraction zone {i+1} échouée, ignorée")

        if not parts:
            return None, None

        if len(parts) == 1:
            return parts[0], offsets

        # Concatenate all audio parts into one file
        concat_out = os.path.join(self.job_dir, "hot_audio.aac")
        inputs     = []
        for p in parts:
            inputs += ["-i", p]
        filter_s = "".join(f"[{i}:a]" for i in range(len(parts)))
        filter_s += f"concat=n={len(parts)}:v=0:a=1[aout]"
        r = subprocess.run(
            ["ffmpeg", "-y"] + inputs + [
                "-filter_complex", filter_s,
                "-map", "[aout]",
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

    def _remap_hot_timestamps(self, result: dict, offsets: list) -> dict:
        """
        Convert timestamps from concatenated audio space → original video time.
        offsets: [(concat_start, video_start), ...] sorted by concat_start ascending.
        """
        def remap(t: float) -> float:
            for i, (cs, vs) in enumerate(offsets):
                next_cs = offsets[i + 1][0] if i + 1 < len(offsets) else float("inf")
                if cs <= t < next_cs:
                    return t - cs + vs
            # Fallback: use last segment
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

    # ── Video duration probe ──────────────────────────────────────────────

    def _get_video_duration(self, video_path: str) -> float:
        try:
            r = subprocess.run(
                ["ffprobe", "-v", "quiet", "-print_format", "json",
                 "-show_format", video_path],
                capture_output=True, text=True, timeout=15
            )
            data = json.loads(r.stdout)
            return float(data.get("format", {}).get("duration", 0))
        except Exception:
            return 0.0

    # ── Download ──────────────────────────────────────────────────────────

    def _download(self, url):
        video_id   = _extract_video_id(url)
        cache_path = os.path.join(CACHE_DIR, f"{video_id}.mp4")

        if os.path.exists(cache_path) and os.path.getsize(cache_path) > 100_000:
            self._update("processing", 10, "Vidéo trouvée dans le cache, skip téléchargement...")
            try:
                with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True}) as ydl:
                    meta  = ydl.extract_info(url, download=False)
                    title = meta.get("title", "Untitled")
                    self._raw_heatmap = meta.get("heatmap") or []
            except Exception:
                title = video_id
                self._raw_heatmap = []
            return cache_path, title

        ydl_opts = {
            "format": (
                "bestvideo[height<=1080][vcodec^=avc][ext=mp4]+bestaudio[ext=m4a]"
                "/bestvideo[height<=1080][vcodec^=avc]+bestaudio"
                "/bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]"
                "/bestvideo[height<=1080]+bestaudio"
                "/bestvideo[vcodec^=avc]+bestaudio"
                "/best[ext=mp4]/best"
            ),
            "outtmpl": cache_path,
            "merge_output_format": "mp4",
            "quiet": True, "no_warnings": True,
        }
        title = "Untitled"
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            meta  = ydl.extract_info(url, download=True)
            title = meta.get("title", "Untitled")
            self._raw_heatmap = meta.get("heatmap") or []
        return cache_path, title

    # ── Transcribe ────────────────────────────────────────────────────────

    def _transcribe(self, video_path, language):
        """
        Transcribe using faster-whisper (4-8x faster on CPU, int8 quantized).
        Falls back to openai-whisper if faster-whisper is not installed.

        Priority order for audio extraction:
          1. Hot segments from YouTube heatmap (smartest — only most-viewed parts)
          2. Manual interval (video_start / video_end)
          3. Full video (fallback)
        """
        audio_path  = video_path
        tmp_audio   = None
        hot_offsets = None  # list of (concat_start, video_start) for timestamp remapping

        # ── Priority 1: YouTube heatmap hot segments ──────────────────────
        hot_segs = getattr(self, "_hot_segments", None)
        if hot_segs:
            total_hot = sum(e - s for s, e in hot_segs)
            self._log(
                f"⚡ Transcription intelligente : extraction de {len(hot_segs)} zone(s) "
                f"populaire(s) ({total_hot:.0f}s au lieu de la vidéo entière)"
            )
            extracted, offsets = self._extract_hot_audio(video_path, hot_segs)
            if extracted and offsets:
                audio_path  = extracted
                tmp_audio   = extracted
                hot_offsets = offsets
                self._log(f"   Audio hot extrait → {os.path.basename(extracted)}")
            else:
                self._log("   ⚠️ Extraction hot échouée — transcription complète en fallback")

        # ── Priority 2: manual interval (video_start/video_end) ───────────
        elif (getattr(self, "_partial_start", None) is not None and
              getattr(self, "_partial_end",   None) is not None):
            v_start, v_end = self._partial_start, self._partial_end
            if v_end > v_start:
                tmp_audio = os.path.join(self.job_dir, "partial_audio.m4a")
                self._log(
                    f"Extraction audio partielle [{v_start:.0f}s → {v_end:.0f}s] "
                    f"({v_end - v_start:.0f}s au lieu de la vidéo entière)"
                )
                r = subprocess.run(
                    ["ffmpeg", "-y",
                     "-ss", str(v_start), "-to", str(v_end),
                     "-i", video_path, "-vn", "-c:a", "copy", tmp_audio],
                    capture_output=True
                )
                if r.returncode == 0 and os.path.exists(tmp_audio):
                    audio_path  = tmp_audio
                    hot_offsets = [(0.0, v_start)]
                else:
                    self._log("⚠️ Extraction partielle échouée — transcription complète")
                    tmp_audio = None

        # ── Transcribe ────────────────────────────────────────────────────
        try:
            result = self._transcribe_faster(audio_path, language)
            self._log("Moteur : faster-whisper (optimisé CPU)")
        except Exception as e:
            self._log(f"faster-whisper indisponible ({e}) — fallback openai-whisper")
            result = self._transcribe_openai(audio_path, language)

        # ── Remap timestamps back to original video time ──────────────────
        if hot_offsets:
            result = self._remap_hot_timestamps(result, hot_offsets)

        if tmp_audio:
            try: os.remove(tmp_audio)
            except OSError: pass

        return result

    def _transcribe_faster(self, audio_path: str, language):
        """
        faster-whisper transcription — returns openai-whisper-compatible dict.
        The WhisperModel instance is kept in a module-level cache so it is NOT
        reloaded between jobs (saves 5-10s per run after the first load).
        """
        from faster_whisper import WhisperModel
        global _WHISPER_CACHE, _WHISPER_CACHE_MAX

        model_key = self.whisper_model
        if model_key in _WHISPER_CACHE:
            self._log(f"✓ Whisper «{model_key}» déjà en cache RAM — skip rechargement")
            model = _WHISPER_CACHE[model_key]
        else:
            # Evict oldest entry if cache is full
            if len(_WHISPER_CACHE) >= _WHISPER_CACHE_MAX:
                evicted = next(iter(_WHISPER_CACHE))
                del _WHISPER_CACHE[evicted]
                self._log(f"Cache Whisper : «{evicted}» désactivé pour libérer la RAM")
            self._log(f"Chargement WhisperModel({model_key}, int8) — mise en cache RAM...")
            model = WhisperModel(model_key, device="cpu", compute_type="int8")
            _WHISPER_CACHE[model_key] = model
            self._log(f"✓ Modèle «{model_key}» chargé et mis en cache (réutilisable)")

        lang = language if language else None
        self._log("Transcription en cours (faster-whisper)...")
        segments_iter, info = model.transcribe(
            audio_path, language=lang,
            word_timestamps=True, vad_filter=True,  # VAD = skip silent parts
        )
        segments = []
        for seg in segments_iter:
            words = [
                {"word": w.word.strip(), "start": w.start, "end": w.end}
                for w in (seg.words or [])
            ]
            segments.append({
                "id": seg.id, "start": seg.start, "end": seg.end,
                "text": seg.text, "words": words,
            })
        return {"segments": segments, "language": info.language}

    def _transcribe_openai(self, audio_path: str, language):
        """Fallback: original openai-whisper."""
        import whisper as ow
        model  = ow.load_model(self.whisper_model)
        kwargs = {"word_timestamps": True}
        if language:
            kwargs["language"] = language
        return model.transcribe(audio_path, **kwargs)

    # ── Translate subtitles ───────────────────────────────────────────────

    def _translate_transcript(self, transcript, target_lang):
        client = OpenAI(api_key=self.openai_key)
        translated_segments = []
        for seg in transcript.get("segments", []):
            orig_text = seg["text"].strip()
            if not orig_text:
                continue
            try:
                resp = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content":
                        f"Translate this text to {target_lang}. "
                        f"Return ONLY the translation, nothing else:\n{orig_text}"}],
                    temperature=0.2, max_tokens=300,
                )
                translated_text = resp.choices[0].message.content.strip()
            except Exception:
                translated_text = orig_text
            trans_words = translated_text.split()
            seg_start, seg_end = seg["start"], seg["end"]
            duration = seg_end - seg_start
            n = max(len(trans_words), 1)
            new_words = [
                {"word":  w,
                 "start": seg_start + (i / n) * duration,
                 "end":   seg_start + ((i + 1) / n) * duration}
                for i, w in enumerate(trans_words)
            ]
            translated_segments.append({**seg, "text": translated_text, "words": new_words})
        return {**transcript, "segments": translated_segments}

    # ── AI moment selection (with viral score) ────────────────────────────

    def _select_moments(self, transcript, max_clips, clip_duration,
                        video_start=None, video_end=None, hot_segments=None):
        MIN_DURATION = max(10, clip_duration // 4)
        
        segments_info = []
        for s in transcript["segments"]:    
            s_start = round(s["start"], 1)
            s_end = round(s["end"], 1)
            
        # We add only the segemnt how are in the interval (Onlly)
        if video_start is not None and s_start < video_start:
            continue
        
        if video_end is not None and s_end > video_end:
            continue
        
        segments_info.append({
            "start": s_start,
            "end": s_end,
            "text": s["text"].strip()
        })
        
        # segments_info = [
        #     {"start": round(s["start"], 1), "end": round(s["end"], 1), "text": s["text"].strip()}
        #     for s in transcript["segments"]
        #     if (video_start is None or s["end"] >= video_start)
        #     and (video_end   is None or s["start"] <= video_end)
        # ]
        client = OpenAI(api_key=self.openai_key)

        hot_hint = ""
        if hot_segments:
            ranges = ", ".join(
                f"{int(s)//60}:{int(s)%60:02d}-{int(e)//60}:{int(e)%60:02d}"
                for s, e in hot_segments
            )
            hot_hint = (
                f"\nIMPORTANT: The following time ranges are the MOST REPLAYED parts "
                f"of this video according to YouTube analytics (heatmap): {ranges}. "
                f"Strongly prefer clips that fall within or overlap these ranges, "
                f"as they are proven to be the most engaging moments."
            )

        prompt = f"""You are a TikTok viral content expert.
Select exactly {max_clips} clips. Min {MIN_DURATION}s, Max {clip_duration}s. Extend if too short.
STRICT CONSTRAINT: You must ONLY use the provided transcript timestamps.
Do NOT go outside the range {video_start if video_start else 0}s to {video_end if video_end else 'end'}s.
Use natural sentence boundaries. No overlap.
For each clip also give a viral_score (1-10) based on entertainment, emotion, and shareability.{hot_hint}
Transcript: {json.dumps(segments_info, ensure_ascii=False)}
Reply ONLY with JSON: {{"clips": [{{"start": 12.5, "end": 48.3, "title": "Title", "viral_score": 8}}]}}"""
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}, temperature=0.7,
        )
        result = []
        for c in json.loads(response.choices[0].message.content).get("clips", [])[:max_clips]:
            s, e, t = c["start"], c["end"], c["title"]
            score = int(c.get("viral_score", 7))
            if e - s < MIN_DURATION: e = s + MIN_DURATION
            if e - s > clip_duration: e = s + clip_duration
            result.append((s, e, t, score))
        return result

    # ── AI caption + hashtag generation ──────────────────────────────────

    def _generate_captions(self, clips_meta):
        """Generate TikTok caption + hashtags for each clip via GPT-4o mini."""
        client = OpenAI(api_key=self.openai_key)
        captions = []
        titles = [t for _, _, t, _ in clips_meta]
        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content":
                    f"For each of these {len(titles)} TikTok clip titles, write a short punchy "
                    f"caption (max 100 chars) followed by 5 trending hashtags on a new line.\n"
                    f"Format each as: CAPTION\\n#tag1 #tag2 #tag3 #tag4 #tag5\n"
                    f"Separate clips with ---\n\n"
                    + "\n".join(f"{i+1}. {t}" for i, t in enumerate(titles))}],
                temperature=0.8, max_tokens=800,
            )
            raw = resp.choices[0].message.content.strip()
            parts = re.split(r"\n---\n|---", raw)
            for p in parts:
                p = p.strip()
                if p:
                    lines = p.strip().split("\n")
                    # Remove leading "1. " numbering if present
                    caption_line = re.sub(r"^\d+\.\s*", "", lines[0]).strip()
                    hashtags = lines[1].strip() if len(lines) > 1 else "#viral #fyp #trending"
                    captions.append(f"{caption_line}\n\n{hashtags}")
        except Exception:
            captions = []
        # Fallback per-clip if GPT failed or returned fewer than expected
        while len(captions) < len(clips_meta):
            i = len(captions)
            t = clips_meta[i][2] if i < len(clips_meta) else "Clip"
            captions.append(f"{t}\n\n#viral #fyp #trending #tiktok #foryou")
        return captions

    # ── Hook text generation ──────────────────────────────────────────────

    def _generate_hook_text(self, clip_title: str) -> str:
        """GPT generates a short 5-10 word hook sentence for the clip intro."""
        try:
            client = OpenAI(api_key=self.openai_key)
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content":
                    f"Write ONE very short hook sentence (5-8 words max) to open a TikTok clip "
                    f"about: '{clip_title}'. Make it intriguing or create curiosity. "
                    f"Return ONLY the sentence, no quotes, no punctuation at end."}],
                temperature=0.9, max_tokens=30,
            )
            return resp.choices[0].message.content.strip().strip('"\'.')
        except Exception:
            return clip_title[:60]

    # ── Silence detection & removal (jump cuts) ───────────────────────────

    def _apply_silence_removal(self, input_path: str, output_path: str) -> bool:
        """
        Detect silences in the clip and remove them (jump cuts).
        Returns True if silences were found and removed, False otherwise.
        """
        # Step 1: detect silences
        det = subprocess.run(
            ["ffmpeg", "-i", input_path,
             "-af", "silencedetect=noise=-35dB:d=0.35", "-f", "null", "-"],
            capture_output=True, text=True
        )
        text = det.stderr + det.stdout

        starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", text)]
        ends   = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", text)]

        # Get clip duration
        dur_m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", text)
        if not dur_m or not starts:
            return False
        duration = (int(dur_m.group(1)) * 3600 +
                    int(dur_m.group(2)) * 60 +
                    float(dur_m.group(3)))

        # Build keep intervals (non-silent segments)
        silences = list(zip(starts, ends if len(ends) >= len(starts)
                            else starts + [duration]))
        keep = []
        prev = 0.0
        for s, e in silences:
            if s > prev + 0.15:
                keep.append((prev, s))
            prev = e
        if prev < duration - 0.15:
            keep.append((prev, duration))

        if len(keep) < 2:
            return False

        # Build concat inputs + filter
        inputs = []
        filter_parts = []
        for i, (ks, ke) in enumerate(keep):
            inputs.extend(["-ss", f"{ks:.3f}", "-to", f"{ke:.3f}", "-i", input_path])
            filter_parts.append(f"[{i}:v][{i}:a]")
        concat_filter = "".join(filter_parts) + f"concat=n={len(keep)}:v=1:a=1[vout][aout]"

        r = subprocess.run(
            ["ffmpeg", "-y"] + inputs + [
                "-filter_complex", concat_filter,
                "-map", "[vout]", "-map", "[aout]",
                "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                "-c:a", "aac", "-b:a", "192k", output_path
            ],
            capture_output=True, text=True
        )
        return r.returncode == 0

    # ── Face detection ────────────────────────────────────────────────────

    def _detect_face_crop(self, video_path, start, end, src_w, src_h):
        try:
            import cv2
            cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            face_cascade = cv2.CascadeClassifier(cascade_path)

            cap = cv2.VideoCapture(video_path)
            fps = cap.get(cv2.CAP_PROP_FPS) or 25
            step = max(1, int(fps * 1.0))

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
        
        # Récupération les limites depuis l'instance
        v_limit_start = getattr(self, "_partial_start", None)
        v_limit_end = getattr(self, "_partial_end", None)
        
        # Force le clip a rester dans les bornes définies par l'utilisateur 
        if v_limit_start is not None:
            start = max(start, v_limit_start)
        if v_limit_end is not None:
            end = min(end, v_limit_end)
            
        clip_path = os.path.abspath(os.path.join(self.job_dir, f"clip_{index}.mp4"))
        temp_path = os.path.abspath(os.path.join(self.job_dir, f"temp_{index}.mp4"))

        # Probe source dimensions
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", video_path],
            capture_output=True, text=True, check=True)
        src_w = src_h = None
        for s in json.loads(probe.stdout).get("streams", []):
            if s.get("codec_type") == "video":
                src_w, src_h = s["width"], s["height"]; break

        tgt_w, tgt_h = 1080, 1920
        
        # Validate start and end times
        total_duration = self._get_video_duration(video_path)
        if start < 0 or end > total_duration:
            raise ValueError("Invalid start/end times for clip generation")

        # Face tracking + smart zoom
        face_rect = None
        if self.face_tracking and src_w and src_h:
            self._update("processing", None, f"Détection de visage (clip {index+1})...")
            face_rect = self._detect_face_crop(video_path, start, end, src_w, src_h)

        if face_rect:
            fx, fy, fw, fh = face_rect
            if self.smart_zoom:
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
            crop = (f"scale={tgt_w}:{tgt_h}:force_original_aspect_ratio=decrease,"
                    f"pad={tgt_w}:{tgt_h}:(ow-iw)/2:(oh-ih)/2")

        # Pass 1: cut + crop
        clip_dur = round(end - start, 1)
        self._log(f"  Clip {index+1} — ffmpeg pass 1 : découpe {start:.1f}s→{end:.1f}s ({clip_dur}s), crop 9:16 + encode H.264...")
        r1 = subprocess.run(
            ["ffmpeg", "-y", "-ss", str(start), "-to", str(end), "-i", video_path,
             "-vf", crop,
             "-c:v", "libx264", "-preset", "medium", "-crf", "20",
             "-profile:v", "high", "-level", "4.1",
             "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
             temp_path],
            capture_output=True, text=True)
        if r1.returncode != 0:
            raise RuntimeError(f"ffmpeg pass 1 failed: {r1.stderr[-800:]}")
        self._log(f"  Clip {index+1} — pass 1 terminé")

        # Pass 1b: silence removal / jump cuts (optional)
        if self.silence_removal:
            self._update("processing", None, f"Suppression des silences (clip {index+1})...")
            self._log(f"  Clip {index+1} — détection des silences (ffmpeg silencedetect)...")
            jc_path = os.path.join(self.job_dir, f"jc_{index}.mp4")
            removed = self._apply_silence_removal(temp_path, jc_path)
            if removed and os.path.exists(jc_path):
                try: os.remove(temp_path)
                except OSError: pass
                temp_path = jc_path
                self._log(f"  Clip {index+1} — silences supprimés (jump cuts appliqués)")
            else:
                self._log(f"  Clip {index+1} — aucun silence détecté, clip inchangé")

        # Collect subtitle PNGs
        self._log(f"  Clip {index+1} — rendu des sous-titres mot par mot (style: {self.subtitle_style})...")
        words = self._extract_words(transcript, start, end)
        subs  = self._render_word_by_word_pngs(words, start, index, tgt_w)
        self._log(f"  Clip {index+1} — {len(subs)} frame(s) de sous-titres générées")

        # Hook intro overlay (first 2 seconds)
        if self.add_hook:
            clip_title = ""
            job = self.jobs.get(self.job_id, {})
            clips = job.get("clips", [])
            # Try to find title from clips already processed
            # Use a temporary hook based on index
            hook_text = self._generate_hook_text(
                clips[index]["title"] if index < len(clips) else f"Clip {index+1}"
            ) if index < len(clips) else f"Clip {index+1}"

            hook_png = os.path.join(self.job_dir, f"hook_{index}.png")
            try:
                render_hook_png(hook_text, hook_png, tgt_w)
                # Show hook for first 2 seconds
                subs = [{"path": hook_png, "t0": 0.0, "t1": 2.0,
                         "h": 200, "is_hook": True}] + subs
            except Exception:
                pass

        # Determine output path for this pass
        sub_out = clip_path if not self.music_track else \
                  os.path.join(self.job_dir, f"nosub_{index}.mp4")

        if subs:
            self._log(f"  Clip {index+1} — ffmpeg pass 2 : incrustation sous-titres{' + watermark' if self.watermark else ''}...")
            ok = self._overlay_subtitles(temp_path, sub_out, subs, tgt_h, index)
            if not ok:
                self._log(f"  Clip {index+1} — ⚠️ overlay échoué, fallback sans sous-titres")
                shutil.copy(temp_path, sub_out)
            else:
                self._log(f"  Clip {index+1} — pass 2 terminé")
        else:
            self._log(f"  Clip {index+1} — aucun mot détecté, pas de sous-titres")
            shutil.copy(temp_path, sub_out)

        try: os.remove(temp_path)
        except OSError: pass

        # Pass 3: music mix
        if self.music_track and os.path.exists(self.music_track):
            self._log(f"  Clip {index+1} — ffmpeg pass 3 : mixage musique de fond...")
            self._mix_music(sub_out, clip_path, end - start)
            try: os.remove(sub_out)
            except OSError: pass
            self._log(f"  Clip {index+1} — musique mixée")
        elif self.music_track:
            self._log(f"  Clip {index+1} — ⚠️ fichier musique introuvable, ignoré")
            if sub_out != clip_path:
                shutil.copy(sub_out, clip_path)
                try: os.remove(sub_out)
                except OSError: pass

        self._log(f"  Clip {index+1} — ✓ PRÊT : {os.path.basename(clip_path)}")
        return clip_path

    # ── Music mixer ───────────────────────────────────────────────────────

    def _mix_music(self, input_path, output_path, clip_duration):
        vol = max(0.05, min(1.0, self.music_volume))
        r = subprocess.run(
            ["ffmpeg", "-y",
             "-i", input_path,
             "-stream_loop", "-1", "-i", self.music_track,
             "-filter_complex",
             f"[1:a]volume={vol:.2f},afade=t=out:st={max(0, clip_duration-1.5):.2f}:d=1.5[music];"
             f"[0:a][music]amix=inputs=2:duration=first:dropout_transition=1[aout]",
             "-map", "0:v", "-map", "[aout]",
             "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
             "-movflags", "+faststart", output_path],
            capture_output=True, text=True)
        if r.returncode != 0:
            shutil.copy(input_path, output_path)

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

                png_path = os.path.join(self.job_dir,
                                        f"sub_{clip_index}_g{g_idx}_w{w_idx}.png")
                try:
                    h = render_word_group_png(group_words, w_idx, png_path,
                                              video_w, style=style)
                    result.append({"path": png_path, "t0": t0, "t1": t1, "h": h})
                except Exception:
                    pass
        return result

    # ── ffmpeg subtitle overlay (+ optional watermark) ────────────────────

    def _overlay_subtitles(self, temp_path, clip_path, subs, video_h, clip_index):
        inputs = ["-i", temp_path]
        for sub in subs:
            inputs += ["-i", sub["path"]]

        filter_parts = []
        prev = "0:v"
        for i, sub in enumerate(subs):
            y_pos = int(video_h * 0.68)
            is_last = (i == len(subs) - 1) and not self.watermark
            out_label = "vout" if is_last else f"v{i+1}"
            t0, t1 = sub["t0"], sub["t1"]
            filter_parts.append(
                f"[{prev}][{i+1}:v]"
                f"overlay=(W-w)/2:{y_pos}:"
                f"enable='between(t,{t0:.3f},{t1:.3f})'"
                f"[{out_label}]"
            )
            prev = out_label

        # Append watermark drawtext filter at the end of the chain
        if self.watermark:
            wm = re.sub(r"[':()\\]", "", self.watermark)[:40]
            fs = 42
            filter_parts.append(
                f"[{prev}]drawtext="
                f"text='{wm}':fontsize={fs}:fontcolor=white@0.80:"
                f"x=w-tw-32:y=h-th-32:"
                f"shadowcolor=black@0.60:shadowx=2:shadowy=2"
                f"[vout]"
            )

        filter_file = os.path.join(self.job_dir, f"filter_{clip_index}.txt")
        with open(filter_file, "w", encoding="utf-8") as fh:
            fh.write(";\n".join(filter_parts))

        r = subprocess.run(
            ["ffmpeg", "-y"] + inputs + [
                "-filter_complex_script", filter_file,
                "-map", "[vout]", "-map", "0:a",
                "-c:v", "libx264", "-preset", "medium", "-crf", "20",
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

    # \u2500\u2500 Webhook notification \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

    def _fire_webhook(self, webhook_url: str, job_id: str, clip_count: int):
        """POST a JSON payload to the webhook URL on completion."""
        try:
            payload = json.dumps({
                "event":      "clips_ready",
                "job_id":     job_id,
                "clip_count": clip_count,
            }).encode()
            req = urllib.request.Request(
                webhook_url, data=payload,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass  # Webhook failure is non-fatal
