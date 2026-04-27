@echo off
REM Supprime le dossier worktree peaceful-hugle-3b93d1 et nettoie le git
cd /d "%~dp0"

echo Suppression du worktree git...
git worktree remove --force peaceful-hugle-3b93d1 2>nul

echo Suppression du dossier residuel si present...
if exist "peaceful-hugle-3b93d1" (
    rmdir /s /q "peaceful-hugle-3b93d1"
    echo Dossier supprime.
) else (
    echo Dossier deja supprime.
)

echo Suppression de la branche locale claude/peaceful-hugle-3b93d1...
git branch -D claude/peaceful-hugle-3b93d1 2>nul

echo Nettoyage git...
git worktree prune

echo.
echo Termine. Le projet n'a plus de sous-dossier parasite.
pause
