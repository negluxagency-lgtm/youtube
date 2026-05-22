@echo off
echo.
echo ============================================================
echo   YUTU PIPELINE — Ensamblador Cinematografico v1.0
echo ============================================================
echo.
echo [*] Activando propulsores...
cd /d "%~dp0"
python src\ensamblar_video.py
echo.
if %ERRORLEVEL% EQU 0 (
    echo [OK] Aterrizaje exitoso. Revisa artifacts\documental_final.mp4
) else (
    echo [ERROR] Turbulencia detectada. Revisa artifacts\logs\
)
echo.
pause
