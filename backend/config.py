"""
ClipCut AI — shared configuration & constants
"""
import os, re, sys

# ── Load .env if present ──────────────────────────────────────────────────────
_env_file = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_file):
    with open(_env_file) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip())

# ── OpenAI API key (server-side — never exposed to frontend) ──────────────────
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

# ── Paths ─────────────────────────────────────────────────────────────────────
OUTPUT_DIR           = os.path.join(os.path.expanduser("~"), "ClipCutAI_outputs")
CACHE_DIR            = os.path.join(os.path.expanduser("~"), "ClipCutAI_cache")
DB_PATH              = os.path.join(os.path.expanduser("~"), "ClipCutAI_jobs.db")
TRANSCRIPT_CACHE_DIR = os.path.join(CACHE_DIR, "transcripts")
UPLOAD_DIR           = os.path.join(CACHE_DIR, "uploads")

for _d in (OUTPUT_DIR, CACHE_DIR, TRANSCRIPT_CACHE_DIR, UPLOAD_DIR):
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
    """Extract a short unique ID from any supported platform URL."""
    patterns = [
        # YouTube
        r"(?:v=|youtu\.be/|embed/|shorts/)([A-Za-z0-9_-]{11})",
        # Twitch VOD
        r"twitch\.tv/videos/(\d+)",
        # Twitch clip
        r"twitch\.tv/\w+/clip/([A-Za-z0-9_-]+)",
        r"clips\.twitch\.tv/([A-Za-z0-9_-]+)",
        # Kick
        r"kick\.com/[^/]+\?clip=([A-Za-z0-9_-]+)",
        r"kick\.com/video/([A-Za-z0-9_-]+)",
        # Nimo
        r"nimo\.tv/[^/]+/(\d+)",
        # TikTok
        r"tiktok\.com/.*/video/(\d+)",
        # Instagram reel/video
        r"instagram\.com/(?:reel|p|tv)/([A-Za-z0-9_-]+)",
        # Twitter/X
        r"(?:twitter|x)\.com/\w+/status/(\d+)",
        # Facebook
        r"facebook\.com/.*/videos/(\d+)",
        # Dailymotion
        r"dailymotion\.com/video/([A-Za-z0-9]+)",
        # Vimeo
        r"vimeo\.com/(\d+)",
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    # Fallback: sanitize the URL into a safe filename
    return re.sub(r"[^A-Za-z0-9_-]", "_", url)[-50:]


def detect_platform(url: str) -> str:
    """Return a human-readable platform name for the URL."""
    u = url.lower()
    if "youtu" in u:            return "YouTube"
    if "twitch.tv" in u:        return "Twitch"
    if "kick.com" in u:         return "Kick"
    if "nimo.tv" in u:          return "Nimo"
    if "tiktok.com" in u:       return "TikTok"
    if "instagram.com" in u:    return "Instagram"
    if "twitter.com" in u or "x.com" in u: return "Twitter/X"
    if "facebook.com" in u:     return "Facebook"
    if "dailymotion.com" in u:  return "Dailymotion"
    if "vimeo.com" in u:        return "Vimeo"
    return "Vidéo"
