"""
ClipCut AI — Whisper transcription module
Features:
  - RAM cache: model stays loaded between jobs (no reload)
  - mmap=True: OS keeps model pages in page cache across restarts
  - Transcript disk cache: skip Whisper entirely if same video+model+segments
  - preload(): called at server startup so first user pays no wait
"""
import hashlib, json, os

from config import TRANSCRIPT_CACHE_DIR

# ── RAM model cache ────────────────────────────────────────────────────────────
_WHISPER_CACHE: dict = {}   # { model_name: WhisperModel instance }
_WHISPER_CACHE_MAX   = 1    # keep 1 model hot; raise to 2 if RAM allows


def load_model(model_name: str, log_fn=None):
    """Return a cached WhisperModel, loading it if necessary."""
    global _WHISPER_CACHE, _WHISPER_CACHE_MAX

    if model_name in _WHISPER_CACHE:
        if log_fn:
            log_fn(f"✓ Whisper «{model_name}» — cache RAM hit, skip rechargement")
        return _WHISPER_CACHE[model_name]

    from faster_whisper import WhisperModel

    # Evict oldest if cache is full
    if len(_WHISPER_CACHE) >= _WHISPER_CACHE_MAX:
        evicted = next(iter(_WHISPER_CACHE))
        del _WHISPER_CACHE[evicted]
        if log_fn:
            log_fn(f"Cache Whisper : «{evicted}» évincé pour libérer la RAM")

    if log_fn:
        log_fn(f"Chargement WhisperModel({model_name}, int8, cpu_threads=4)...")

    # cpu_threads: use multiple cores for decoding
    # mmap support depends on ctranslate2 version — try gracefully
    try:
        model = WhisperModel(model_name, device="cpu", compute_type="int8",
                             cpu_threads=4, num_workers=1)
    except TypeError:
        model = WhisperModel(model_name, device="cpu", compute_type="int8")

    _WHISPER_CACHE[model_name] = model
    if log_fn:
        log_fn(f"✓ Modèle «{model_name}» chargé et mis en cache RAM")
    return model


def preload(model_name: str):
    """
    Preload a Whisper model at server startup so the first job doesn't wait.
    Called from FastAPI @app.on_event('startup').
    """
    try:
        load_model(model_name)
        print(f"[ClipCut] Whisper «{model_name}» préchargé en RAM ✓", flush=True)
    except Exception as e:
        print(f"[ClipCut] Préchargement Whisper «{model_name}» échoué : {e}", flush=True)


# ── Transcript disk cache ──────────────────────────────────────────────────────

def transcript_cache_key(video_id: str, model: str, hot_segs) -> str:
    hot_str = json.dumps(hot_segs, sort_keys=True) if hot_segs else "full"
    suffix  = hashlib.md5(hot_str.encode()).hexdigest()[:8]
    return f"{video_id}_{model}_{suffix}"


def load_transcript_cache(cache_key: str):
    """Return cached transcript dict or None."""
    path = os.path.join(TRANSCRIPT_CACHE_DIR, f"{cache_key}.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None


def save_transcript_cache(cache_key: str, transcript: dict):
    """Save transcript dict to disk for future reuse."""
    path = os.path.join(TRANSCRIPT_CACHE_DIR, f"{cache_key}.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(transcript, f, ensure_ascii=False)
    except Exception:
        pass


def clear_transcript_cache():
    """Delete all cached transcripts. Returns count of deleted files."""
    deleted = 0
    for f in os.listdir(TRANSCRIPT_CACHE_DIR):
        if f.endswith(".json"):
            try:
                os.remove(os.path.join(TRANSCRIPT_CACHE_DIR, f))
                deleted += 1
            except OSError:
                pass
    return deleted


# ── Transcription ──────────────────────────────────────────────────────────────

def transcribe_faster(audio_path: str, language, model_name: str, log_fn=None) -> dict:
    """faster-whisper transcription → openai-whisper-compatible dict."""
    model = load_model(model_name, log_fn)
    lang  = language if language else None
    if log_fn:
        log_fn("Transcription en cours (faster-whisper, VAD activé)...")
    segments_iter, info = model.transcribe(
        audio_path, language=lang,
        word_timestamps=True, vad_filter=True,
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
    if log_fn:
        log_fn(f"faster-whisper terminé : {len(segments)} segments, langue={info.language}")
    return {"segments": segments, "language": info.language}


def transcribe_openai(audio_path: str, language, model_name: str, log_fn=None) -> dict:
    """Fallback: original openai-whisper library."""
    import whisper as ow
    if log_fn:
        log_fn(f"Chargement openai-whisper ({model_name})...")
    model  = ow.load_model(model_name)
    kwargs = {"word_timestamps": True}
    if language:
        kwargs["language"] = language
    return model.transcribe(audio_path, **kwargs)
