@echo off
setlocal
rem Keep this launcher ASCII-compatible with Windows code pages.
pushd "%~dp0"
if errorlevel 1 (
    echo [ERROR] Cannot open the project directory.
    pause
    exit /b 1
)
if not exist ".venv-webui\Scripts\python.exe" (
    echo [ERROR] WebUI Python environment not found.
    echo Follow README.md to create .venv-webui and install requirements/webui.txt.
    goto :failed
)
if not exist "run_webui.py" (
    echo [ERROR] run_webui.py not found. Keep this launcher in the project root.
    goto :failed
)
if not exist ".webui.env" if not defined WEBUI_PASSWORD (
    echo [ERROR] WebUI password is not configured.
    echo Create .webui.env in this directory and set WEBUI_PASSWORD (see README.md).
    echo Use your own random password with 16-512 characters.
    goto :failed
)
echo Starting local WebUI. Docker will NOT be started.
echo Open http://127.0.0.1:8765 in your browser after startup.
echo If WEBUI_PORT was changed, use that port instead.
echo Keep this window open. Press Ctrl+C to stop WebUI.
echo.
".venv-webui\Scripts\python.exe" "run_webui.py"
if errorlevel 1 goto :failed
popd
exit /b 0

:failed
echo.
echo WebUI did not start or exited with an error. See the message above.
pause
popd
exit /b 1
