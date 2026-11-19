@echo off
setlocal
pushd "%~dp0" || (pause & exit /b 1)
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" start_firelinker.py
) else (
    where py.exe >nul 2>&1
    if errorlevel 1 (
        python start_firelinker.py
    ) else (
        py -3 start_firelinker.py
    )
)
set "result=%ERRORLEVEL%"
popd
if not "%result%"=="0" pause
exit /b %result%
