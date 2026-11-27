@echo off
rem Legt die Verzeichnisstruktur fuer Datenbasen, Nodes, Panels, Plugins und Traces an (Windows).
rem Start: Doppelklick auf diese Datei. Vorhandene Ordner und Dateien bleiben unveraendert.

setlocal
cd /d "%~dp0.." || goto :error
set "FAILED="

call :create "Database\Demo"
call :create "Database\TRACE32"
call :create "Database\VectorIO"
call :create "Database\BLE"
call :create "Nodes\Demo"
call :create "Panel\Demo"
call :create "Plugins"
call :create "Traces\Demo"

echo.
if defined FAILED goto :error
echo Verzeichnisstruktur ist vollstaendig in: %CD%
pause
exit /b 0

:error
echo Nicht alle Ordner konnten angelegt werden.
pause
exit /b 1

:create
if exist "%~1\" (
    echo vorhanden: %~1
    exit /b 0
)
mkdir "%~1" && (
    echo angelegt:  %~1
    exit /b 0
)
set "FAILED=1"
exit /b 1
