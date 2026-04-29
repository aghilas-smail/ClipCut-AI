# ✂️ ClipCut AI — YouTube → TikTok Clip Generator

Micro SaaS qui découpe automatiquement une longue vidéo YouTube en petits clips TikTok viraux avec sous-titres animés mot-par-mot.

## Comment ça marche

1. **Téléchargement** — `yt-dlp` télécharge la vidéo YouTube en 1080p
2. **Transcription** — Whisper AI (local, gratuit) transcrit la vidéo avec timestamps par mot
3. **Détection IA** — GPT-4o mini analyse la transcription et sélectionne les meilleurs passages
4. **Génération des clips** — ffmpeg recadre en 9:16, encode en H.264 et brûle les sous-titres animés
5. **Téléchargement** — Clips MP4 1080x1920 prêts à publier sur TikTok / Reels / Shorts

---

## Prérequis système

| Outil | Version min | Usage |
|-------|-------------|-------|
| Python | 3.10+ | Backend FastAPI |
| ffmpeg | 4.x+ | Encodage vidéo |
| deno | 1.x+ | JS runtime pour yt-dlp (n-challenges YouTube) |
| node.js | 16+ | Fallback JS runtime |
| Clé OpenAI | — | GPT-4o mini pour la sélection des moments |

---

## Installation — WSL (recommandé sur Windows)

> Le projet tourne sous Linux. Sur Windows, utiliser WSL (Ubuntu 22.04+).

### 1. Installer WSL si besoin
```powershell
# Dans PowerShell (administrateur)
wsl --install
# Redémarrer, puis ouvrir Ubuntu depuis le menu Démarrer
```

### 2. Cloner le projet
```bash
git clone https://github.com/ton-user/clipcut-ai.git
cd clipcut-ai
```

### 3. Lancer le script de démarrage
```bash
bash start_wsl.sh
```

Le script installe automatiquement toutes les dépendances système (ffmpeg, python3, nodejs, deno) et lance le serveur.

### 4. Configurer la clé OpenAI
```bash
# Éditer backend/.env
OPENAI_API_KEY=sk-...
```

Ouvrez **http://localhost:8000** dans votre navigateur.

---

## Installation — macOS / Linux natif

### 1. Installer ffmpeg
```bash
# macOS
brew install ffmpeg

# Ubuntu / Debian
sudo apt update && sudo apt install -y ffmpeg
```

### 2. Installer deno (JS runtime pour yt-dlp)
```bash
curl -fsSL https://deno.land/install.sh | sh

# Ajouter deno au PATH (ajouter dans ~/.bashrc ou ~/.zshrc)
export DENO_INSTALL="$HOME/.deno"
export PATH="$DENO_INSTALL/bin:$PATH"

# Vérifier
deno --version
```

> **Pourquoi deno ?** yt-dlp en a besoin pour résoudre les n-challenges YouTube. Sans deno, yt-dlp télécharge une vidéo en 360p (format 18) au lieu du 1080p.

### 3. Installer node.js (fallback si deno absent)
```bash
# Ubuntu / Debian
sudo apt install -y nodejs

# macOS
brew install node
```

### 4. Lancer le projet
```bash
bash start.sh
```

---

## Configuration

Copier et éditer le fichier d'environnement :
```bash
cp backend/.env.example backend/.env
```

| Variable | Obligatoire | Description |
|----------|-------------|-------------|
| `OPENAI_API_KEY` | ✅ | Clé API OpenAI (GPT-4o mini) |
| `CACHE_DIR` | ❌ | Répertoire cache vidéos (défaut : `~/ClipCutAI_cache`) |
| `OUTPUT_DIR` | ❌ | Répertoire clips générés (défaut : `~/ClipCutAI_outputs`) |

---

## Structure du projet

```
clipcut-ai/
├── backend/
│   ├── main.py              # Serveur FastAPI — routes et démarrage
│   ├── config.py            # Configuration globale
│   ├── state.py             # État des jobs en mémoire
│   ├── db.py                # Persistance légère
│   ├── api/
│   │   ├── process.py       # POST /api/process
│   │   ├── status.py        # GET  /api/status/{job_id}
│   │   ├── download.py      # GET  /api/download + /thumbnail + /trim
│   │   ├── upload.py        # POST /api/upload (fichier local)
│   │   ├── estimate.py      # GET  /api/estimate
│   │   └── admin.py         # GET  /api/admin
│   └── core/
│       ├── processor.py     # Orchestrateur principal du pipeline
│       ├── transcriber.py   # Whisper (faster-whisper + openai-whisper)
│       ├── gpt_client.py    # GPT-4o mini — sélection moments + captions
│       ├── ffmpeg_utils.py  # Encodage ffmpeg, crop, sous-titres, musique
│       ├── subtitles.py     # Rendu PNG des sous-titres (Pillow)
│       └── heatmap.py       # Analyse heatmap YouTube
├── frontend/
│   └── index.html           # Interface SPA (vanilla JS)
├── music/                   # Musiques de fond optionnelles (.mp3)
├── outputs/                 # Clips générés (créé automatiquement)
├── start.sh                 # Démarrage macOS / Linux
├── start_wsl.sh             # Démarrage WSL (installe les dépendances auto)
└── start_windows.bat        # Démarrage Windows natif
```

---

## API Endpoints

| Méthode | Endpoint | Description |
|---------|----------|-------------|
| `POST` | `/api/process` | Soumettre une URL YouTube ou un fichier local |
| `GET` | `/api/status/{job_id}` | Suivre la progression et récupérer les clips |
| `GET` | `/api/download/{job_id}/{clip_index}` | Télécharger un clip MP4 |
| `GET` | `/api/download-zip/{job_id}` | Télécharger tous les clips en ZIP |
| `GET` | `/api/thumbnail/{job_id}/{clip_index}` | Miniature JPEG du clip |
| `POST` | `/api/trim/{job_id}/{clip_index}` | Recouper un clip (ajuster début/fin) |
| `POST` | `/api/upload` | Uploader un fichier vidéo local |
| `GET` | `/api/estimate` | Estimer le temps de traitement |

### Exemple d'utilisation

```bash
# Soumettre une vidéo
curl -X POST http://localhost:8000/api/process \
  -H "Content-Type: application/json" \
  -d '{
    "youtube_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "openai_api_key": "sk-...",
    "max_clips": 3,
    "clip_duration": 60,
    "language": "auto"
  }'

# Suivre le statut
curl http://localhost:8000/api/status/{job_id}
```

---

## Coût estimé

Utilise **GPT-4o mini** uniquement pour la sélection des moments et la génération des captions.
- Vidéo de 10 min : ~0.01 €
- Vidéo de 60 min : ~0.03–0.05 €

Whisper tourne **en local** — aucun coût de transcription.

---

## Évolutions possibles

- [ ] Authentification utilisateur + plans d'abonnement (Stripe)
- [ ] Stockage S3 pour les clips générés
- [ ] File d'attente Redis pour jobs multiples simultanés
- [ ] Templates de sous-titres personnalisables
- [ ] Intégration TikTok / Instagram API pour publication directe
- [ ] Déploiement Docker + hébergement cloud
