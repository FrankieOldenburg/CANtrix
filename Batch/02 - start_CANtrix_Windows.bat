@echo off
rem Startet den CANtrix (Windows).
rem Start: Doppelklick auf diese Datei. Optional: Batch\02 - start_CANtrix_Windows.bat konfig.canconfig

setlocal
cd /d "%~dp0.."

rem Ohne Konsolenfenster starten: zuerst "pyw" (Python-Launcher), sonst "pythonw".
where pyw >nul 2>nul && (
    start "" pyw -3 "%~dp0..\Sourcen\CANtrix.py" %*
    goto :end
)
where pythonw >nul 2>nul && (
    start "" pythonw "%~dp0..\Sourcen\CANtrix.py" %*
    goto :end
)

rem Fallback mit Konsole, damit Fehlermeldungen sichtbar bleiben.
where py >nul 2>nul && (
    py -3 "%~dp0..\Sourcen\CANtrix.py" %*
    goto :check
)
where python >nul 2>nul && (
    python "%~dp0..\Sourcen\CANtrix.py" %*
    goto :check
)

echo Python wurde nicht gefunden.
echo Bitte Python 3 von https://www.python.org/downloads/ installieren
echo und dabei "Add python.exe to PATH" anhaken.
pause
goto :end

:check
if errorlevel 1 pause

:end
endlocal
