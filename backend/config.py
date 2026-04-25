"""
ClipCut AI — shared configuration & constants
"""
import os, re, sys

# ── Paths ─────────────────────────────────────────────────────────────────────
OUTPUT_DIR           = os.path.join(os.path.expanduser("~"), "ClipCutAI_outputs")
CACHE_DIR            = os.path.join(os.path.expanduser("~"), "ClipCutAI_cache")
DB_PATH              = os.path.join(os.path.expanduser("~"), "ClipCutAI_jobs.db")
TRANSCRIPT_CACHE_DIR = os.path.join(CACHE_DIR, "transcripts")

for _d in (OUTPUT_DIR, CACHE_DIR, TRANSCRIPT_CACHE_DIR):
    os.makedirs(_d, exist_ok=True)

# ── Whisper speed factors (real-time ratio, faster-whisper CPU int8) ──────────
WHISPER_SPEED = {
    "tiny":   0.04,
    "base":   0.10,
    "small":  0.22,
    "medium": 0.50,
    "large":  1.00,
}

# Default model preloaded at server startup (override via env var)
DEFAULT_WHISPER_MODEL = os.environ.get("CLIPCUT_WHISPER_MODEL", "base")

IS_WINDOWS = sys.platform == "win32"

# ── Font discovery ─────────────────────────────────────────────────────────────
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

# ── Helpers ────────────────────────────────────────────────────────────────────
def fmt_duration(seconds: float) -> str:
    """Format seconds → '3min 42s' or '42s'."""
    s = int(max(0, seconds))
    m = s // 60
    return f"{m}min {s % 60}s" if m else f"{s}s"

def extract_video_id(url: str) -> str:
    for p in [r"(?:v=|youtu\.be/|embed/|shorts/)([A-Za-z0-9_-]{11})"]:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return re.sub(r"[^A-Za-z0-9_-]", "_", url)[-50:]
