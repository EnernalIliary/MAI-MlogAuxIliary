@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem The packaged sidecar is a frozen onedir build (mod\MlogAuxiliary\sidecar\bin\logicd.exe).
rem The mod itself starts it on the first F3, so this window is only a manual fallback
rem (or for a dev checkout that has no exe). Prefer the exe: it needs no Python.
set "EXE=%~dp0mod\MlogAuxiliary\sidecar\bin\logicd.exe"

set "PORT=8765"
if not "%~1"=="" set "PORT=%~1"

set "BUSY="
for /f "tokens=*" %%L in ('netstat -ano ^| findstr ":%PORT%" ^| findstr "LISTENING"') do set "BUSY=1"
if defined BUSY (
    echo [warn] port %PORT% is already listening -- a sidecar may already be running.
    echo        nothing to do; the mod will find it. Close this window.
    echo.
    pause
    exit /b 1
)

if exist "%EXE%" goto run_exe

set "PY="
python -c "import sys;sys.exit(0 if sys.version_info[0]==3 else 1)" >nul 2>nul && set "PY=python"
if not defined PY (
    py -3 -c "import sys;sys.exit(0)" >nul 2>nul && set "PY=py -3"
)
if not defined PY (
    echo [fail] no sidecar\bin\logicd.exe in this package, and Python 3 not found.
    echo        reinstall the package with the install script, or install Python 3.
    echo.
    pause
    exit /b 1
)

echo [ok] starting the source sidecar with: %PY%
echo      keep this window open while you play.
echo.
%PY% "%~dp0sidecar\logicd.py" --port %PORT%
goto done

:run_exe
echo [ok] starting the bundled sidecar: mod\MlogAuxiliary\sidecar\bin\logicd.exe
echo      keep this window open while you play.
echo.
"%EXE%" --port %PORT%

:done
echo.
echo sidecar exited -- press any key to close.
pause >nul
endlocal
