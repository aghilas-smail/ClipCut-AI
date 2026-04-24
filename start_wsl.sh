#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# ClipCut AI — WSL startup script
# Run this inside Ubuntu WSL: bash start_wsl.sh
# ─────────────────────────────────────────────────────────────────────────────
set -e

echo ""
echo "  ✂️  ClipCut AI — WSL Setup"
echo "  ─────────────────────────────────────────────────────"

# Resolve project directory (works whether called from Windows or WSL path)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 1. Install system dependencies
echo "  📦 Installing system dependencies..."
sudo apt-get update -qq
sudo apt-get install -y -qq \
    ffmpeg \
    python3 python3-pip python3-venv \
    fonts-open-sans fonts-liberation \
    libsm6 libxext6 > /dev/null 2>&1
echo "  ✅ ffmpeg $(ffmpeg -version 2>&1 | head -1 | awk '{print $3}')"

# 2. Create virtual env in Linux home (not on Windows NTFS mount — symlink issues)
VENV_DIR="$HOME/clipcut_venv"
if [ ! -d "$VENV_DIR" ]; then
    echo "  🐍 Creating Python virtual environment in $VENV_DIR ..."
    python3 -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"

# 3. Install Python packages
echo "  📥 Installing Python dependencies..."
pip install --quiet --upgrade pip
pip install --quiet -r backend/requirements.txt

# 4. Create output directory
mkdir -p outputs

# 5. Launch
echo ""
echo "  🚀 Server starting at http://localhost:8000"
echo "  ─────────────────────────────────────────────────────"
echo "  Open your browser at: http://localhost:8000"
echo "  Press Ctrl+C to stop."
echo ""
cd backend
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
