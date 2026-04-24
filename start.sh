#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# ClipCut AI — Start script
# Usage: bash start.sh
# ─────────────────────────────────────────────────────────────────────────────

set -e
cd "$(dirname "$0")"

echo ""
echo "  ✂️  ClipCut AI"
echo "  ─────────────────────────────────────────────────────"

# 1. Check Python
if ! command -v python3 &>/dev/null; then
  echo "  ❌ Python 3 introuvable. Installez Python 3.10+ depuis https://python.org"
  exit 1
fi

# 2. Check ffmpeg
if ! command -v ffmpeg &>/dev/null; then
  echo "  ❌ ffmpeg introuvable."
  echo "     → macOS  : brew install ffmpeg"
  echo "     → Ubuntu : sudo apt install ffmpeg"
  echo "     → Windows: https://ffmpeg.org/download.html"
  exit 1
fi

# 3. Create virtual env if needed
if [ ! -d ".venv" ]; then
  echo "  📦 Création de l'environnement virtuel Python…"
  python3 -m venv .venv
fi

# 4. Activate venv
source .venv/bin/activate 2>/dev/null || source .venv/Scripts/activate

# 5. Install dependencies
echo "  📥 Installation des dépendances…"
pip install --quiet --upgrade pip
pip install --quiet -r backend/requirements.txt

# 6. Create outputs directory
mkdir -p outputs

# 7. Launch server
echo ""
echo "  🚀 Démarrage du serveur sur http://localhost:8000"
echo "  ─────────────────────────────────────────────────────"
echo "  Appuyez sur Ctrl+C pour arrêter."
echo ""

cd backend
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
