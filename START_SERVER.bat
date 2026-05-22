@echo off
echo.
echo ============================================================
echo   YUTU RENDER SERVER — http://0.0.0.0:8000
echo ============================================================
echo.
echo [*] Iniciando servidor... (Ctrl+C para detener)
echo.
cd /d "%~dp0"
python src\server.py
pause
