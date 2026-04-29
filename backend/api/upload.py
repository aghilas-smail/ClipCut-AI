"""
ClipCut AI — /api/upload  (video file upload)
Allows creators to upload their own video file instead of providing a URL.
"""
import os, uuid, shutil
from fastapi import APIRouter, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse

from config import UPLOAD_DIR

router = APIRouter()

# Accepted video MIME types / extensions
_ALLOWED_EXT = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".flv"}
_MAX_SIZE_GB  = 4   # 4 GB limit


@router.post("/upload")
async def upload_video(file: UploadFile = File(...)):
    """
    Upload a local video file.
    Returns a local_video_path to use in POST /api/process.
    """
    # Extension check
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in _ALLOWED_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"Format non supporté : {ext}. Acceptés : {', '.join(_ALLOWED_EXT)}"
        )

    # Save to upload dir with a unique name
    uid       = str(uuid.uuid4())
    safe_name = f"{uid}{ext}"
    dest_path = os.path.join(UPLOAD_DIR, safe_name)

    try:
        size = 0
        with open(dest_path, "wb") as out:
            while chunk := await file.read(1024 * 1024):  # 1 MB chunks
                size += len(chunk)
                if size > _MAX_SIZE_GB * 1024 ** 3:
                    out.close()
                    os.remove(dest_path)
                    raise HTTPException(
                        status_code=413,
                        detail=f"Fichier trop volumineux (max {_MAX_SIZE_GB} Go)"
                    )
                out.write(chunk)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Erreur sauvegarde : {exc}")

    size_mb = round(size / 1024 ** 2, 1)
    return JSONResponse({
        "local_video_path": dest_path,
        "filename":         file.filename,
        "size_mb":          size_mb,
        "upload_id":        uid,
    })


@router.delete("/upload/{upload_id}")
async def delete_upload(upload_id: str):
    """Clean up a previously uploaded file."""
    # Security: only allow UUID-shaped IDs
    try:
        uuid.UUID(upload_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="ID invalide")

    for ext in _ALLOWED_EXT:
        path = os.path.join(UPLOAD_DIR, f"{upload_id}{ext}")
        if os.path.exists(path):
            os.remove(path)
            return {"deleted": True}
    raise HTTPException(status_code=404, detail="Fichier introuvable")
