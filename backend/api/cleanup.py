"""
ClipCut AI — Auto-nettoyage du cache (vidéos, transcriptions, uploads, clips)
Suppression automatique des fichiers de plus de 6h toutes les 6h.
Expose aussi des routes pour déclencher manuellement et consulter les stats.
"""
import os, time, shutil, asyncio, logging
from fastapi import APIRouter
from config import OUTPUT_DIR, CACHE_DIR, TRANSCRIPT_CACHE_DIR, UPLOAD_DIR

router = APIRouter()
logger = logging.getLogger("clipcut.cleanup")

# ── Constantes ────────────────────────────────────────────────────────────────
DEFAULT_MAX_AGE = 6 * 3600   # 6 heures en secondes
CLEANUP_INTERVAL = 6 * 3600  # intervalle entre deux passes automatiques

# ── Helpers ───────────────────────────────────────────────────────────────────

def _human_size(n_bytes: int) -> str:
    """Convertit un nombre d'octets en chaîne lisible (KB, MB, GB)."""
    for unit in ("B", "KB", "MB", "GB"):
        if n_bytes < 1024:
            return f"{n_bytes:.1f} {unit}"
        n_bytes /= 1024
    return f"{n_bytes:.1f} TB"


def _dir_size(path: str) -> int:
    """Taille totale d'un dossier en octets."""
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _scan_old_files(base_dir: str, cutoff: float,
                    recursive: bool = False) -> dict:
    """
    Retourne le nombre et la taille des fichiers/dossiers plus vieux que cutoff.
    Si recursive=False, ne regarde que les entrées directes de base_dir.
    """
    count, size = 0, 0
    if not os.path.isdir(base_dir):
        return {"count": 0, "size": 0}
    for entry in os.listdir(base_dir):
        fpath = os.path.join(base_dir, entry)
        try:
            mtime = os.path.getmtime(fpath)
            if mtime < cutoff:
                if os.path.isfile(fpath):
                    count += 1
                    size  += os.path.getsize(fpath)
                elif os.path.isdir(fpath):
                    count += 1
                    size  += _dir_size(fpath)
        except OSError:
            pass
    return {"count": count, "size": size}


# ── Logique principale ────────────────────────────────────────────────────────

def run_cleanup(max_age_seconds: int = DEFAULT_MAX_AGE) -> dict:
    """
    Supprime tous les fichiers/dossiers plus vieux que max_age_seconds dans :
      - CACHE_DIR      : vidéos téléchargées (fichiers à la racine)
      - TRANSCRIPT_CACHE_DIR : fichiers de transcription
      - UPLOAD_DIR     : vidéos uploadées par l'utilisateur
      - OUTPUT_DIR     : dossiers de clips générés (un dossier par job)
    Retourne un résumé de l'opération.
    """
    cutoff        = time.time() - max_age_seconds
    deleted_files = 0
    deleted_size  = 0
    errors        = []

    # ── 1. Fichiers à la racine de CACHE_DIR (vidéos + .json métadonnées) ──
    if os.path.isdir(CACHE_DIR):
        for fname in os.listdir(CACHE_DIR):
            fpath = os.path.join(CACHE_DIR, fname)
            if not os.path.isfile(fpath):
                continue
            try:
                if os.path.getmtime(fpath) < cutoff:
                    sz = os.path.getsize(fpath)
                    os.remove(fpath)
                    deleted_files += 1
                    deleted_size  += sz
            except Exception as e:
                errors.append(f"cache/{fname}: {e}")

    # ── 2. Transcriptions ──────────────────────────────────────────────────
    if os.path.isdir(TRANSCRIPT_CACHE_DIR):
        for fname in os.listdir(TRANSCRIPT_CACHE_DIR):
            fpath = os.path.join(TRANSCRIPT_CACHE_DIR, fname)
            if not os.path.isfile(fpath):
                continue
            try:
                if os.path.getmtime(fpath) < cutoff:
                    sz = os.path.getsize(fpath)
                    os.remove(fpath)
                    deleted_files += 1
                    deleted_size  += sz
            except Exception as e:
                errors.append(f"transcripts/{fname}: {e}")

    # ── 3. Uploads temporaires ─────────────────────────────────────────────
    if os.path.isdir(UPLOAD_DIR):
        for fname in os.listdir(UPLOAD_DIR):
            fpath = os.path.join(UPLOAD_DIR, fname)
            if not os.path.isfile(fpath):
                continue
            try:
                if os.path.getmtime(fpath) < cutoff:
                    sz = os.path.getsize(fpath)
                    os.remove(fpath)
                    deleted_files += 1
                    deleted_size  += sz
            except Exception as e:
                errors.append(f"uploads/{fname}: {e}")

    # ── 4. Dossiers de jobs (OUTPUT_DIR/{job_id}/) ─────────────────────────
    if os.path.isdir(OUTPUT_DIR):
        for job_dir in os.listdir(OUTPUT_DIR):
            dpath = os.path.join(OUTPUT_DIR, job_dir)
            if not os.path.isdir(dpath):
                continue
            try:
                if os.path.getmtime(dpath) < cutoff:
                    sz = _dir_size(dpath)
                    shutil.rmtree(dpath, ignore_errors=True)
                    deleted_files += 1
                    deleted_size  += sz
            except Exception as e:
                errors.append(f"outputs/{job_dir}: {e}")

    threshold_h = max_age_seconds / 3600
    msg = (
        f"[Cleanup] {deleted_files} élément(s) supprimé(s) "
        f"({_human_size(deleted_size)}) — seuil : {threshold_h:.0f}h"
    )
    logger.info(msg)
    print(msg, flush=True)

    return {
        "deleted_items":      deleted_files,
        "deleted_size_bytes": deleted_size,
        "freed":              _human_size(deleted_size),
        "threshold_hours":    threshold_h,
        "errors":             errors,
    }


# ── Routes API ────────────────────────────────────────────────────────────────

@router.post("/cleanup")
async def trigger_cleanup(max_age_hours: float = 6.0):
    """
    Déclenche manuellement un nettoyage du cache.
    ?max_age_hours=6.0  (défaut : 6h)
    """
    loop   = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None, run_cleanup, int(max_age_hours * 3600)
    )
    return result


@router.get("/cleanup/stats")
async def cleanup_stats(max_age_hours: float = 6.0):
    """
    Retourne les stats sur ce qui SERAIT supprimé (dry-run, rien n'est effacé).
    ?max_age_hours=6.0  (défaut : 6h)
    """
    cutoff = time.time() - int(max_age_hours * 3600)

    # Fichiers racine de CACHE_DIR
    cache_root = {"count": 0, "size": 0}
    if os.path.isdir(CACHE_DIR):
        for fname in os.listdir(CACHE_DIR):
            fpath = os.path.join(CACHE_DIR, fname)
            if os.path.isfile(fpath):
                try:
                    if os.path.getmtime(fpath) < cutoff:
                        cache_root["count"] += 1
                        cache_root["size"]  += os.path.getsize(fpath)
                except OSError:
                    pass

    breakdown = {
        "videos":      cache_root,
        "transcripts": _scan_old_files(TRANSCRIPT_CACHE_DIR, cutoff),
        "uploads":     _scan_old_files(UPLOAD_DIR, cutoff),
        "outputs":     _scan_old_files(OUTPUT_DIR, cutoff),
    }

    total_size  = sum(v["size"]  for v in breakdown.values())
    total_count = sum(v["count"] for v in breakdown.values())

    # Taille actuelle totale (indépendamment de l'âge)
    current_sizes = {
        "videos_total_mb":      round(_dir_size(CACHE_DIR) / 1024 / 1024, 1),
        "transcripts_total_mb": round(_dir_size(TRANSCRIPT_CACHE_DIR) / 1024 / 1024, 1),
        "uploads_total_mb":     round(_dir_size(UPLOAD_DIR) / 1024 / 1024, 1),
        "outputs_total_mb":     round(_dir_size(OUTPUT_DIR) / 1024 / 1024, 1),
    }

    return {
        "would_delete_count": total_count,
        "would_free":         _human_size(total_size),
        "would_free_bytes":   total_size,
        "breakdown":          breakdown,
        "current_disk":       current_sizes,
        "threshold_hours":    max_age_hours,
        "next_auto_cleanup":  f"dans ~{CLEANUP_INTERVAL // 3600}h (tâche de fond)",
    }


# ── Tâche de fond asyncio ─────────────────────────────────────────────────────

async def auto_cleanup_loop():
    """
    Tâche asyncio lancée au démarrage du serveur.
    Attend 6h, nettoie, puis répète indéfiniment.
    Un premier nettoyage est effectué immédiatement au démarrage
    (supprime les résidus d'une session précédente de plus de 6h).
    """
    # Passe initiale au démarrage (supprime les vieux fichiers résiduels)
    try:
        run_cleanup(DEFAULT_MAX_AGE)
    except Exception as e:
        logger.error(f"[Cleanup] Erreur passe initiale : {e}")

    while True:
        await asyncio.sleep(CLEANUP_INTERVAL)
        try:
            run_cleanup(DEFAULT_MAX_AGE)
        except Exception as e:
            logger.error(f"[Cleanup] Erreur passe automatique : {e}")
