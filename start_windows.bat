@echo off
REM ─────────────────────────────────────────────────────────────────────────────
REM ClipCut AI — Script de démarrage Windows
REM ─────────────────────────────────────────────────────────────────────────────

echo.
echo   ✂️  ClipCut AI
echo   ─────────────────────────────────────────────────────

REM 1. Check Python
python --version >nul 2>&1
IF ERRORLEVEL 1 (
    echo   ❌ Python introuvable. Installez Python 3.10+ depuis https://python.org
    pause & exit /b 1
)

REM 2. Create virtual env if needed
IF NOT EXIST ".venv" (
    echo   📦 Creation de l'environnement virtuel Python...
    python -m venv .venv
)

REM 3. Activate venv
call .venv\Scripts\activate.bat

REM 4. Install dependencies
echo   📥 Installation des dependances...
pip install --quiet --upgrade pip
pip install --quiet -r backend\requirements.txt

REM 5. Create outputs directory
IF NOT EXIST "outputs" mkdir outputs

REM 6. Launch server
echo.
echo   🚀 Demarrage sur http://localhost:8000
echo   Appuyez sur Ctrl+C pour arreter.
echo.

cd backend
uvicorn main:app --host 0.0.0.0 --port 8000 --reload

pause
