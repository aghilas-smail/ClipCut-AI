#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# ClipCut AI — WSL startup script
# Run this inside Ubuntu WSL: bash start_wsl.sh
# ────────────────────────────────────────────────────────────────────��────────
set -e

# ── Kill anything holding port 8000 ──────────────────────────────────────────
_free_port() {
  # 1. Force-kill all uvicorn processes (--reload spawns 2+)
  pkill -9 -f "uvicorn" 2>/dev/null || true

  # 2. Kill by PID via ss (reliable on Ubuntu WSL, no fuser needed)
  local pids
  pids=$(ss -lptn 'sport = :8000' 2>/dev/null \
           | grep -oP '(?<=pid=)\d+' | sort -u)
  if [ -n "$pids" ]; then
    echo "$pids" | xargs -r kill -9 2>/dev/null || true
  fi

  # 3. Wait up to 4 s for port to release
  for i in 1 2 3 4; do
    ss -tlnp 2>/dev/null | grep -q ":8000 " || return 0
    sleep 1
  done
  return 1   # still busy
}

if ss -tlnp 2>/dev/null | grep -q ":8000 "; then
  echo "  ⚠️  Port 8000 busy — killing previous server..."
  if _free_port; then
    echo "  ✅  Port freed."
  else
    echo "  ❌  Port 8000 still in use after 4 s. Try: sudo kill -9 \$(ss -lptn 'sport = :8000' | grep -oP '(?<=pid=)\d+' | head -1)"
    exit 1
  fi
fi

echo ""
echo "  ✂️  ClipCut AI v3 — WSL Setup"
echo "  ──────────────────────────────────────────────────────"

# Resolve project directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Load .env ──────────────────────────────────────────────────────────────
ENV_FILE="$SCRIPT_DIR/backend/.env"
if [ -f "$ENV_FILE" ]; then
  echo "  🔑 Chargement backend/.env..."
  # Convert Windows CRLF → LF before sourcing (avoids $'\r' errors)
  _ENV_CLEAN=$(sed 's/\r//' "$ENV_FILE")
  set -a
  eval "$_ENV_CLEAN" 2>/dev/null || true
  set +a
else
  echo "  ⚠️  backend/.env absent — copie de .env.example..."
  cp "$SCRIPT_DIR/backend/.env.example" "$ENV_FILE" 2>/dev/null || true
  echo "     → Édite backend/.env et ajoute OPENAI_API_KEY=sk-... puis relance."
fi
if [ -z "$OPENAI_API_KEY" ]; then
  echo "  ⚠️  OPENAI_API_KEY non définie (les traitements échoueront jusqu'à config)"
fi

# 1. Install system dependencies
echo "  📦 Installing system dependencies..."
sudo apt-get update -qq
sudo apt-get install -y -qq \
    ffmpeg \
    python3 python3-pip python3-venv \
    nodejs \
    fonts-open-sans fonts-liberation \
    libsm6 libxext6 > /dev/null 2>&1
echo "  ✅ ffmpeg $(ffmpeg -version 2>&1 | head -1 | awk '{print $3}')"
echo "  ✅ node  $(node --version 2>/dev/null || echo 'not found')"

# ── Deno (JS runtime pour yt-dlp — résout les n challenges YouTube) ───────
export DENO_INSTALL="$HOME/.deno"
export PATH="$DENO_INSTALL/bin:$PATH"
if ! command -v deno &>/dev/null; then
  echo "  📦 Installation de deno (JS runtime pour yt-dlp)..."
  curl -fsSL https://deno.land/install.sh | sh > /dev/null 2>&1
  # S'assure que deno est dans ~/.bashrc pour les sessions futures
  grep -qxF 'export DENO_INSTALL="$HOME/.deno"' ~/.bashrc \
    || echo 'export DENO_INSTALL="$HOME/.deno"' >> ~/.bashrc
  grep -qxF 'export PATH="$DENO_INSTALL/bin:$PATH"' ~/.bashrc \
    || echo 'export PATH="$DENO_INSTALL/bin:$PATH"' >> ~/.bashrc
fi
echo "  ✅ deno  $(deno --version 2>/dev/null | head -1 || echo 'not found')"

# 2. Virtual env (in Linux home — avoids NTFS symlink issues)
VENV_DIR="$HOME/clipcut_venv"
if [ ! -d "$VENV_DIR" ]; then
    echo "  🐍 Creating venv at $VENV_DIR ..."
    python3 -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"

# 3. Python dependencies
echo "  📥 Installing Python dependencies..."
pip install --quiet --upgrade pip
pip install --quiet -r backend/requirements.txt

# 4. Output dir
mkdir -p outputs

# 5. Launch (no --reload avoids multi-process port contention)
echo ""
echo "  🚀 Server starting at http://localhost:8000"
echo "  ─────────────────────────────────────────────────────"
echo "  Open your browser at: http://localhost:8000"
echo "  Press Ctrl+C to stop."
echo ""
cd backend

# ── Silence known noisy-but-harmless warnings ────────────────────────────────
# ONNX Runtime: GPU discovery fails on CPU-only WSL (expected)
export ORT_LOGGING_LEVEL=3
# HuggingFace Hub: unauthenticated rate-limit notice (not a blocker)
export HF_HUB_DISABLE_PROGRESS_BARS=1
export HUGGINGFACE_HUB_VERBOSITY=error
export TRANSFORMERS_VERBOSITY=error

# Run uvicorn and filter the two known warning patterns from stderr
uvicorn main:app --host 0.0.0.0 --port 8000 2>&1 | grep -Ev \
  "device_discovery\.cc|DiscoverDevicesForPlatform|unauthenticated requests to the HF Hub|Please set a HF_TOKEN|higher rate limits"
