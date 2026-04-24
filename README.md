# ✂️ ClipCut AI — YouTube → TikTok Clip Generator

Micro SaaS qui découpe automatiquement une longue vidéo YouTube en petits clips TikTok viraux avec sous-titres animés mot-par-mot.

## Comment ça marche

1. **Téléchargement** — `yt-dlp` télécharge la vidéo YouTube
2. **Transcription** — Whisper AI (local, gratuit) transcrit la vidéo avec timestamps par mot
3. **Détection IA** — GPT-4o mini analyse la transcription et sélectionne les meilleurs passages
4. **Génération des clips** — ffmpeg :
   - Recadre la vidéo en format 9:16 (TikTok/Reels/Shorts)
   - Brûle les sous-titres animés (mot par mot en jaune, style CapCut)
5. **Téléchargement** — Clips MP4 prêts à publier

## Prérequis

- **Python 3.10+** — [python.org](https://python.org)
- **ffmpeg** — [ffmpeg.org](https://ffmpeg.org) (doit être dans le PATH)
- **Clé API OpenAI** — [platform.openai.com](https://platform.openai.com) (GPT-4o mini, très économique)

### Installer ffmpeg

| Système | Commande |
|---------|----------|
| macOS | `brew install ffmpeg` |
| Ubuntu/Debian | `sudo apt install ffmpeg` |
| Windows | Télécharger depuis [ffmpeg.org](https://ffmpeg.org/download.html) et ajouter au PATH |

## Démarrage

### macOS / Linux
```bash
bash start.sh
```

### Windows
Double-cliquer sur `start_windows.bat`

### Manuel
```bash
# Créer et activer l'environnement virtuel
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# Installer les dépendances
pip install -r backend/requirements.txt

# Lancer le serveur
cd backend
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Ouvrez votre navigateur sur **http://localhost:8000**

## Structure du projet

```
clipcut-ai/
├── backend/
│   ├── main.py           # API FastAPI (endpoints)
│   ├── processor.py      # Pipeline de traitement vidéo
│   └── requirements.txt  # Dépendances Python
├── frontend/
│   └── index.html        # Interface utilisateur (SPA)
├── outputs/              # Clips générés (créé automatiquement)
├── start.sh              # Script de démarrage macOS/Linux
└── start_windows.bat     # Script de démarrage Windows
```

## API Endpoints

| Méthode | Endpoint | Description |
|---------|----------|-------------|
| `POST` | `/api/process` | Soumettre une URL YouTube |
| `GET` | `/api/status/{job_id}` | Suivre la progression |
| `GET` | `/api/download/{job_id}/{clip_index}` | Télécharger un clip |

### Exemple d'utilisation API

```bash
# Soumettre une vidéo
curl -X POST http://localhost:8000/api/process \
  -H "Content-Type: application/json" \
  -d '{
    "youtube_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "openai_api_key": "sk-...",
    "max_clips": 5,
    "clip_duration": 60,
    "language": "auto"
  }'

# Suivre le statut
curl http://localhost:8000/api/status/{job_id}
```

## Coût estimé

Le traitement utilise **GPT-4o mini** pour la sélection des moments clés.
Pour une vidéo de 30 minutes : ~0.01–0.03 € par vidéo.

## Évolutions possibles

- [ ] Authentification utilisateur + plans d'abonnement (Stripe)
- [ ] Stockage S3 pour les clips générés
- [ ] File d'attente Redis pour jobs multiples simultanés
- [ ] Templates de sous-titres personnalisables
- [ ] Intégration TikTok/Instagram API pour publication directe
- [ ] Déploiement Docker + hébergement cloud
