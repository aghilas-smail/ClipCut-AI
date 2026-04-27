"""
ClipCut AI — video processing orchestrator
New features vs v2 monolith:
  1. Parallel clip generation  — asyncio.gather + ThreadPoolExecutor (max 3 simultaneous)
  2. Single-pass ffmpeg        — crop + subtitles in one encode (via ffmpeg_utils)
  3. Transcript disk cache     — skip Whisper on repeated requests for same video
  4. Whisper RAM cache + mmap  — via transcriber module (loaded once, reused)
  5. Server startup preload    — via transcriber.preload() in main.py
"""
import asyncio, os, shutil, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import yt_dlp

from config import CACHE_DIR, OUTPUT_DIR, WHISPER_SPEED, fmt_duration, extract_video_id
from core import subtitles   as sub_mod
from core import transcriber as trans_mod
from core import heatmap     as heat_mod
from core import gpt_client  as gpt_mod
from core import ffmpeg_utils as ff_mod

# Shared executor for parallel clip generation (max 3 clips at once)
# 2 workers max — 3 simultaneous ffmpeg each opening 150+ PNGs = ~450 file descriptors
# which hits Linux's default limit of 1024 and triggers EAGAIN / "Resource unavailable"
_CLIP_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="clipcut_clip")


class VideoProcessor:
    def __init__(self, job_id, jobs, openai_key,
                 subtitle_style="elevate",
                 face_tracking=False, smart_zoom=False,
                 music_track=None, music_volume=0.15,
                 whisper_model="base", watermark="",
                 silence_removal=False, add_hook=False, webhook_url="",
                 visual_enhance="none"):
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
        self.visual_enhance  = visual_enhance
        self.job_dir         = os.path.join(OUTPUT_DIR, job_id)
        os.makedirs(self.job_dir, exist_ok=True)

    # ── Logging ───────────────────────────────────────────────────────────────

    def _log(self, message: str):
        job  = self.jobs[self.job_id]
        logs = job.setdefault("logs", [])
        ts   = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {message}"
        logs.append(line)
        if len(logs) > 300:
            job["logs"] = logs[-300:]
        # ── Persist to log file (always readable after job ends) ──────────
        try:
            log_path = os.path.join(self.job_dir, "job.log")
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def _update(self, status, progress, message):
        job = self.jobs[self.job_id]
        job["status"]  = status
        job["message"] = message
        if progress is not None:
            job["progress"] = progress
        self._log(message)

    # ── ETA (accounts for parallelism + single-pass savings) ─────────────────

    def _estimate_eta(self, video_dur, hot_dur, num_clips, clip_duration, cached):
        download   = 8 if cached else 55
        whisper_f  = WHISPER_SPEED.get(self.whisper_model, 0.10)
        transcribe = (hot_dur if hot_dur > 0 else video_dur) * whisper_f
        gpt        = 12
        # Parallel (÷3) + single-pass (×0.55 vs two-pass)
        parallelism = min(num_clips, 3)
        ffmpeg = (num_clips * (clip_duration * 0.65 + 12) / parallelism) * 0.55
        return int(download + transcribe + gpt + ffmpeg)

    # ── Main pipeline ─────────────────────────────────────────────────────────

    async def process(self, url_or_path, max_clips, clip_duration, language,
                      subtitle_style="elevate", video_start=None, video_end=None,
                      face_tracking=False, smart_zoom=False, subtitle_lang=None,
                      music_track=None, music_volume=0.15,
                      whisper_model="base", watermark="",
                      silence_removal=False, add_hook=False, webhook_url="",
                      visual_enhance="none", is_local_file=False):
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
            self.visual_enhance  = visual_enhance
            # Offset used to remap original-video timestamps to trimmed-file timestamps
            self.video_offset    = float(video_start) if video_start else 0.0

            # ── 1. Download / Local file ──────────────────────────────────
            if is_local_file:
                self._update("processing", 10, "Fichier local chargé, analyse en cours...")
                video_path = url_or_path
                title      = os.path.basename(url_or_path)
                raw_heatmap = []
                self._log(f"Fichier local : {title}")
            else:
                from config import detect_platform
                platform = detect_platform(url_or_path)
                self._update("processing", 5, f"Téléchargement {platform}...")
                self._log(f"yt-dlp : connexion à {platform}...")
                video_path, title, raw_heatmap = await loop.run_in_executor(
                    None, self._download, url_or_path, video_start, video_end)
            self.jobs[self.job_id]["title"] = title
            self._log(f"Vidéo : «{title}»")
            # Log résolution réelle du fichier téléchargé
            _dw, _dh = ff_mod.probe_video_dimensions(video_path)
            if _dw and _dh:
                self._log(f"ℹ️  Résolution source : {_dw}x{_dh} — crop vers 9:16 puis scale 1080x1920")
            else:
                self._log("⚠️  Résolution source inconnue (ffprobe échoué)")

            # ── 2. Duration + initial ETA ─────────────────────────────────
            dur    = ff_mod.get_video_duration(video_path)
            cached = "(cache)" in self.jobs[self.job_id].get("message", "")
            dur_label = ""
            if dur > 0:
                m, s = int(dur // 60), int(dur % 60)
                dur_label = f" — vidéo de {m}min {s}s"
                self.jobs[self.job_id]["started_at"] = time.time()

            # ── 3. Heatmap ────────────────────────────────────────────────
            hot_segs = None
            if raw_heatmap and dur > 0:
                self._log(f"Analyse heatmap YouTube ({len(raw_heatmap)} points)...")
                hot_segs = heat_mod.parse_heatmap(raw_heatmap, dur, log_fn=self._log)
            else:
                self._log("Heatmap YouTube non disponible")

            hot_dur = sum(e - s for s, e in hot_segs) if hot_segs else 0
            self.jobs[self.job_id].update({
                "heatmap_active":    bool(hot_segs),
                "hot_segment_count": len(hot_segs) if hot_segs else 0,
            })
            if hot_segs:
                self.jobs[self.job_id]["hot_duration"] = int(hot_dur)

            eta_sec = self._estimate_eta(dur, hot_dur, max_clips, clip_duration, cached)
            self.jobs[self.job_id]["eta_seconds"] = eta_sec
            self._log(
                f"Durée : {dur_label.strip(' —')} | Whisper: {whisper_model} | "
                f"ETA: ~{fmt_duration(eta_sec)}"
                + (f" (heatmap: {int(hot_dur)}s/{int(dur)}s)" if hot_segs else "")
                + " | ⚡ clips parallèles + single-pass ffmpeg"
            )

            # ── 4. Transcription (disk cache → skip Whisper if hit) ───────
            video_id   = extract_video_id(url_or_path) if not is_local_file else self.job_id
            cache_key  = trans_mod.transcript_cache_key(video_id, whisper_model, hot_segs)
            transcript = trans_mod.load_transcript_cache(cache_key)

            if transcript:
                seg_count = len(transcript.get("segments", []))
                self._log(
                    f"✓ Transcription depuis cache disque ({seg_count} segments) "
                    f"— Whisper ignoré !"
                )
                self._update("processing", 35, "Transcription chargée depuis le cache...")
            else:
                self._update("processing", 20,
                             f"Transcription faster-whisper ({whisper_model}){dur_label}...")
                audio_path, hot_offsets = self._prepare_audio(
                    video_path, hot_segs, video_start, video_end)
                try:
                    transcript = trans_mod.transcribe_faster(
                        audio_path, None if language == "auto" else language,
                        whisper_model, log_fn=self._log
                    )
                except Exception as e:
                    self._log(f"faster-whisper indisponible ({e}) — fallback openai-whisper")
                    transcript = trans_mod.transcribe_openai(
                        audio_path, None if language == "auto" else language,
                        whisper_model, log_fn=self._log
                    )

                if hot_offsets:
                    transcript = heat_mod.remap_hot_timestamps(transcript, hot_offsets)
                if audio_path != video_path:
                    try: os.remove(audio_path)
                    except OSError: pass

                seg_count = len(transcript.get("segments", []))
                self._log(f"Transcription terminée : {seg_count} segments")
                trans_mod.save_transcript_cache(cache_key, transcript)
                self._log(f"✓ Transcript sauvegardé en cache disque")

            # ── 5. Subtitle translation ───────────────────────────────────
            if subtitle_lang and subtitle_lang != "original":
                self._update("processing", 37, f"Traduction sous-titres → {subtitle_lang}...")
                transcript = await loop.run_in_executor(
                    None, gpt_mod.translate_transcript,
                    self.openai_key, transcript, subtitle_lang, self._log
                )

            # ── 6. AI moment selection ────────────────────────────────────
            self._update("processing", 45, "Analyse IA : détection des meilleurs moments...")
            self._log(f"GPT-4o mini : sélection de {max_clips} moment(s) viraux...")
            clips_meta = await loop.run_in_executor(
                None, gpt_mod.select_moments,
                self.openai_key, transcript, max_clips, clip_duration,
                video_start, video_end, hot_segs, self._log
            )
            for i, (s, e, t, sc) in enumerate(clips_meta):
                self._log(f"  Clip {i+1}: «{t[:50]}» [{s:.1f}s→{e:.1f}s] score={sc}/10")

            # ── 7. Captions ───────────────────────────────────────────────
            self._update("processing", 55, "Génération des captions TikTok...")
            captions = await loop.run_in_executor(
                None, gpt_mod.generate_captions,
                self.openai_key, clips_meta, self._log
            )

            # ── 8. PARALLEL clip generation ───────────────────────────────
            total = len(clips_meta)
            self._update("processing", 60,
                         f"⚡ Génération parallèle de {total} clip(s) (max 3 simultanés)...")
            self._log(f"⚡ Single-pass ffmpeg + {min(total, 3)} clips en parallèle")

            completed_count = [0]

            def make_tracked(args):
                i, start, end, clip_title, score = args
                clip_path = self._make_tiktok_clip(
                    video_path, transcript, start, end, i, clip_title)
                completed_count[0] += 1
                pct = 60 + int(completed_count[0] * 35 / max(total, 1))
                self._update("processing", pct,
                             f"Clip {completed_count[0]}/{total} prêt : {clip_title[:35]}...")
                return i, clip_path

            tasks = [
                loop.run_in_executor(_CLIP_EXECUTOR, make_tracked,
                                     (i, s, e, t, sc))
                for i, (s, e, t, sc) in enumerate(clips_meta)
            ]
            results = await asyncio.gather(*tasks)
            results = sorted(results, key=lambda x: x[0])

            clips = []
            for i, clip_path in results:
                s, e, t, sc = clips_meta[i]
                clips.append({
                    "index":        i,
                    "title":        t,
                    "start":        s,
                    "end":          e,
                    "duration":     round(e - s, 1),
                    "path":         clip_path,
                    "score":        sc,
                    "caption":      captions[i] if i < len(captions) else "",
                    "source_video": video_path,
                    "source_start": s,
                    "source_end":   e,
                })

            self.jobs[self.job_id]["clips"] = clips
            self._update("completed", 100, f"{total} clips TikTok prêts !")
            self._log(f"✅ Terminé — {total} clips générés")

            if self.webhook_url:
                self._fire_webhook(self.webhook_url, self.job_id, total)

        except Exception as exc:
            import traceback
            self.jobs[self.job_id]["error"] = str(exc)
            self._update("error", 0, f"Erreur : {exc}")
            self._log(f"TRACEBACK:\n{traceback.format_exc()[-600:]}")

    # ── Audio preparation ──────────────────────────────────────────────────────

    def _prepare_audio(self, video_path, hot_segs, video_start, video_end):
        """Returns (audio_path, hot_offsets). audio_path == video_path means full video."""
        import subprocess
        if hot_segs:
            total_hot = sum(e - s for s, e in hot_segs)
            self._log(f"⚡ Extraction audio intelligente : {len(hot_segs)} zone(s), {total_hot:.0f}s")
            extracted, offsets = heat_mod.extract_hot_audio(
                video_path, hot_segs, self.job_dir, log_fn=self._log)
            if extracted and offsets:
                self._log(f"   Audio hot extrait → {os.path.basename(extracted)}")
                return extracted, offsets
            self._log("   ⚠️ Extraction hot échouée — transcription complète")

        elif video_start is not None and video_end is not None and video_end > video_start:
            tmp = os.path.join(self.job_dir, "partial_audio.m4a")
            self._log(f"Extraction partielle [{video_start:.0f}s → {video_end:.0f}s]")
            r = subprocess.run(
                ["ffmpeg", "-y", "-ss", str(video_start), "-to", str(video_end),
                 "-i", video_path, "-vn", "-c:a", "copy", tmp],
                capture_output=True
            )
            if r.returncode == 0 and os.path.exists(tmp):
                return tmp, [(0.0, video_start)]
            self._log("⚠️ Extraction partielle échouée — transcription complète")

        return video_path, None

    # ── Clip generation (single-pass) ─────────────────────────────────────────

    def _make_tiktok_clip(self, video_path, transcript, start, end, index, clip_title=""):
        clip_path = os.path.abspath(os.path.join(self.job_dir, f"clip_{index}.mp4"))
        tgt_w, tgt_h = 1080, 1920

        # When the video was downloaded as a trimmed section (download_sections),
        # its file timestamps start at 0.  Subtract the original start offset so
        # ffmpeg seeks to the right position inside the trimmed file.
        offset      = getattr(self, "video_offset", 0.0)
        seek_start  = max(0.0, start - offset)
        seek_end    = max(seek_start + 0.1, end - offset)

        src_w, src_h = ff_mod.probe_video_dimensions(video_path)

        face_rect = None
        if self.face_tracking and src_w and src_h:
            self._log(f"  Clip {index+1} — détection de visage...")
            face_rect = ff_mod.detect_face_crop(video_path, seek_start, seek_end, src_w, src_h)

        crop_filter = ff_mod.build_crop_filter(src_w, src_h, face_rect, self.smart_zoom)

        # Render subtitle PNGs (fast — Pillow only, no video decode)
        words = self._extract_words(transcript, start, end)
        subs  = self._render_subtitle_pngs(words, start, index, tgt_w)
        self._log(f"  Clip {index+1} — {len(subs)} sous-titres, single-pass encode...")

        # Optional hook intro overlay
        if self.add_hook:
            hook_png  = os.path.join(self.job_dir, f"hook_{index}.png")
            hook_text = gpt_mod.generate_hook_text(self.openai_key, clip_title or f"Clip {index+1}")
            try:
                sub_mod.render_hook_png(hook_text, hook_png, tgt_w)
                subs = [{"path": hook_png, "t0": 0.0, "t1": 2.0, "h": 200}] + subs
            except Exception:
                pass

        sub_out = clip_path if not self.music_track else \
                  os.path.join(self.job_dir, f"nosub_{index}.mp4")

        ok = ff_mod.make_clip_onepass(
            video_path, seek_start, seek_end, subs, crop_filter,
            sub_out, self.job_dir, index,
            watermark=self.watermark, tgt_h=tgt_h,
            visual_enhance=self.visual_enhance, log_fn=self._log
        )
        if not ok:
            self._log(f"  Clip {index+1} — ⚠️ encode échoué")
            # Create empty marker so job doesn't crash
            open(sub_out, "wb").close()

        # Silence removal (post-process)
        if self.silence_removal and os.path.exists(sub_out) and os.path.getsize(sub_out) > 1000:
            jc_path = os.path.join(self.job_dir, f"jc_{index}.mp4")
            removed = ff_mod.apply_silence_removal(sub_out, jc_path)
            if removed and os.path.exists(jc_path):
                try: os.remove(sub_out)
                except OSError: pass
                sub_out = jc_path
                self._log(f"  Clip {index+1} — silences supprimés")

        # Music mix
        if (self.music_track and os.path.exists(self.music_track)
                and os.path.exists(sub_out) and os.path.getsize(sub_out) > 1000):
            ff_mod.mix_music(sub_out, clip_path, end - start,
                             self.music_track, self.music_volume)
            try: os.remove(sub_out)
            except OSError: pass
        elif sub_out != clip_path:
            try: shutil.copy(sub_out, clip_path)
            except Exception: pass
            try: os.remove(sub_out)
            except OSError: pass

        self._log(f"  Clip {index+1} — ✓ PRÊT")
        return clip_path

    # ── Word extraction & subtitle PNG generation ──────────────────────────────

    def _extract_words(self, transcript, start, end):
        words = []
        for seg in transcript.get("segments", []):
            if seg["end"] < start or seg["start"] > end:
                continue
            seg_words = seg.get("words", [])
            if seg_words:
                for w in seg_words:
                    ws, we = w.get("start", 0), w.get("end", 0)
                    if ws >= start - 0.5 and we <= end + 1.0:
                        words.append({"word": w["word"].strip(), "start": ws, "end": we})
            else:
                # Fallback: distribute segment text evenly across segment duration
                text_words = seg.get("text", "").strip().split()
                if not text_words:
                    continue
                seg_s = max(start, seg["start"])
                seg_e = min(end, seg["end"])
                dur   = seg_e - seg_s
                if dur <= 0:
                    continue
                for wi, ww in enumerate(text_words):
                    ws = seg_s + wi * dur / len(text_words)
                    we = seg_s + (wi + 1) * dur / len(text_words)
                    words.append({"word": ww, "start": ws, "end": we})
        return words

    def _render_subtitle_pngs(self, words, clip_start, clip_index, video_w):
        if not words:
            return []

        style           = self.subtitle_style
        WORDS_PER_GROUP = 1 if style in ("oneword", "pop", "neon") else 4

        # Timing V4 — version éprouvée, sans SILENCE_GAP ni MAX_WORDS.
        # Chaque mot reste visible jusqu'au début du mot suivant : pas de disparition
        # intempestive pendant les pauses naturelles de la parole.
        groups = [words[i:i + WORDS_PER_GROUP]
                  for i in range(0, len(words), WORDS_PER_GROUP)]
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
                    h = sub_mod.render_word_group_png(
                        group_words, w_idx, png_path, video_w, style=style)
                    result.append({"path": png_path, "t0": t0, "t1": t1, "h": h})
                except Exception:
                    pass
        return result

    # ── Download ───────────────────────────────────────────────────────────────

    def _download(self, url, video_start=None, video_end=None):
        import json

        video_id = extract_video_id(url)

        # Separate cache entry when a time interval is requested
        interval_suffix = ""
        if video_end is not None and video_start and video_start > 0:
            interval_suffix = f"_{int(video_start)}-{int(video_end)}"
        elif video_end is not None and not video_start:
            interval_suffix = f"_0-{int(video_end)}"

        cache_path = os.path.join(CACHE_DIR, f"{video_id}{interval_suffix}.mp4")
        meta_path  = os.path.join(CACHE_DIR, f"{video_id}.json")

        # ── Cache hit ─────────────────────────────────────────────────────
        if os.path.exists(cache_path) and os.path.getsize(cache_path) > 100_000:
            self._update("processing", 10, "Vidéo trouvée dans le cache, skip téléchargement...")
            if os.path.exists(meta_path):
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        saved = json.load(f)
                    self._log(f"Métadonnées depuis cache : «{saved.get('title', video_id)[:40]}»")
                    return cache_path, saved.get("title", video_id), saved.get("heatmap") or []
                except Exception:
                    pass
            self._log("Cache vidéo trouvé (métadonnées absentes, heatmap ignoré)")
            return cache_path, video_id, []

        # ── Runtime detection (JS fallback seulement — player_client=android est prioritaire) ──
        deno_bin = shutil.which("deno") or ""
        node_bin = shutil.which("node") or shutil.which("nodejs") or ""
        if deno_bin:
            js_runtimes = [f"deno:{deno_bin}"]
        elif node_bin:
            js_runtimes = [f"node:{node_bin}"]
        else:
            js_runtimes = []

        # ffmpeg_location : utiliser uniquement ce que le PATH fournit
        # BUG FIX : ne pas forcer "/usr/bin/ffmpeg" (chemin Linux invalide sur Windows)
        ffmpeg_bin = shutil.which("ffmpeg")  # None si absent du PATH

        # ── yt-dlp logger (surfaces relevant lines into the job log) ──────
        class _YtdlLogger:
            def __init__(self, log_fn):
                self._fn = log_fn
            def debug(self, msg):
                low = msg.lower()
                if any(k in low for k in ("format", "merge", "download", "ffmpeg", "error")):
                    self._fn(f"[yt-dlp] {msg[:250]}")
            def info(self, msg):
                self._fn(f"[yt-dlp] {msg[:250]}")
            def warning(self, msg):
                self._fn(f"[yt-dlp] ⚠️ {msg[:250]}")
            def error(self, msg):
                self._fn(f"[yt-dlp] ❌ {msg[:250]}")

        # ── yt-dlp options ────────────────────────────────────────────────
        # player_client=android : évite les n-challenges YouTube sans JS runtime
        # js_runtimes en fallback seulement si android client échoue
        # socket_timeout=30 : évite les blocages réseau indéfinis (BUG FIX)
        extractor_args: dict = {"player_client": ["web", "android"]}
        if js_runtimes:
            extractor_args["js_runtimes"] = js_runtimes

        ydl_opts = {
            "extractor_args":      {"youtube": extractor_args},
            "format": (
                "bestvideo[height>=1080]+bestaudio"
                "/bestvideo[height>=720]+bestaudio"
                "/bestvideo+bestaudio"
                "/best"
            ),
            "format_sort":         ["res:1080"],
            "merge_output_format": "mp4",
            "outtmpl":             cache_path,
            "quiet":               True,
            "no_warnings":         False,
            "socket_timeout":      30,   # BUG FIX : timeout réseau pour éviter blocage
            "logger":              _YtdlLogger(self._log),
        }
        if ffmpeg_bin:
            ydl_opts["ffmpeg_location"] = ffmpeg_bin  # BUG FIX : ne définir que si trouvé

        # Partial download — only fetch the requested time range
        if video_start is not None and video_end is not None:
            def _sec_to_hhmmss(s):
                h = int(s // 3600); m = int((s % 3600) // 60); sec = int(s % 60)
                return f"{h:02d}:{m:02d}:{sec:02d}"
            section = f"*{_sec_to_hhmmss(video_start)}-{_sec_to_hhmmss(video_end)}"
            ydl_opts["download_sections"]      = section
            ydl_opts["force_keyframes_at_cuts"] = True
            self._log(f"⚡ Téléchargement partiel : {section}")

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            meta = ydl.extract_info(url, download=True)

        title   = meta.get("title", "Untitled")
        heatmap = meta.get("heatmap") or []
        self._log(
            f"yt-dlp format selectionne : {meta.get('format_id','?')} | "
            f"{meta.get('width','?')}x{meta.get('height','?')} | "
            f"vcodec={meta.get('vcodec','?')}"
        )

        # Persist metadata for cache hits
        try:
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump({"title": title, "heatmap": heatmap}, f, ensure_ascii=False)
        except Exception:
            pass

        return cache_path, title, heatmap

    # ── Webhook ────────────────────────────────────────────────────────────────

    def _fire_webhook(self, webhook_url, job_id, clip_count):
        import urllib.request, json
        try:
            payload = json.dumps({"event": "clips_ready", "job_id": job_id,
                                  "clip_count": clip_count}).encode()
            req = urllib.request.Request(
                webhook_url, data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass
