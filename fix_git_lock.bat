@echo off
cd /d "C:\Users\aghil\Documents\Claude\Projects\Projet_1"

echo Suppression du fichier de verrou git...
if exist ".git\index.lock" (
    del /f /q ".git\index.lock"
    echo index.lock supprime.
) else (
    echo index.lock absent, rien a faire.
)

if exist ".git\HEAD.lock" (
    del /f /q ".git\HEAD.lock"
    echo HEAD.lock supprime.
)

echo Verification...
git status
echo.
echo Termine. Tu peux maintenant commiter et pusher normalement.
pause
