@echo off
rem Erstellt aus dem CANtrix ein eigenstaendiges Programm (CANtrix.exe) fuer Windows mit PyInstaller.
rem Start: Doppelklick auf diese Datei.
rem Ergebnis: dist\CANtrix.exe, eine Kopie liegt im Projektordner (neben Database, Traces, Plugins ...).
rem Die exe liest Datenbasen, Traces, Panels, Plugins und Konfigurationen aus dem Ordner, in dem sie liegt.

setlocal
cd /d "%~dp0.."
set "PROJECT=%CD%"

rem Python suchen: zuerst den Python-Launcher "py", sonst "python".
set "PYTHON="
where py >nul 2>nul && set "PYTHON=py -3"
if not defined PYTHON (
    where python >nul 2>nul && set "PYTHON=python"
)
if not defined PYTHON (
    echo Python wurde nicht gefunden.
    echo Bitte Python 3 von https://www.python.org/downloads/ installieren
    echo und dabei "Add python.exe to PATH" anhaken.
    goto :end
)

echo Verwendetes Python:
%PYTHON% --version
echo.

rem In einer virtuellen Umgebung geht --user nicht.
set "USERFLAG=--user"
%PYTHON% -c "import sys; sys.exit(0 if sys.prefix != sys.base_prefix else 1)" && set "USERFLAG="

echo PyInstaller, TRACE32-RCL und pyserial werden installiert bzw. aktualisiert ...
%PYTHON% -m pip install %USERFLAG% --upgrade pyinstaller lauterbach-trace32-rcl pyserial
if errorlevel 1 (
    echo.
    echo Die Installation der Build-Abhaengigkeiten ist fehlgeschlagen.
    goto :end
)
echo.

rem python-can (fuer den Trace Recorder) nur mitnehmen, wenn es installiert ist.
set "EXTRA="
%PYTHON% -c "import can" >nul 2>nul
if errorlevel 1 (
    echo python-can ist nicht installiert: Der Trace Recorder funktioniert in der exe nicht
    echo ^(Installation: 01 - install_python-can_Windows.bat, danach dieses Skript erneut starten^).
) else (
    echo python-can ist installiert und wird mit eingebunden.
    set "EXTRA=--collect-all can"
)
echo.

echo Die exe wird erstellt ^(das dauert einige Minuten^) ...
if exist "%PROJECT%\build\pyinstaller" rmdir /s /q "%PROJECT%\build\pyinstaller"
%PYTHON% -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name CANtrix ^
    --icon "%PROJECT%\Bilder\CANtrix.ico" ^
    --paths "%PROJECT%\Sourcen" ^
    --add-data "%PROJECT%\README.md;." ^
    --add-data "%PROJECT%\README_en.md;." ^
    --add-data "%PROJECT%\Screenshots;Screenshots" ^
    --add-data "%PROJECT%\Bilder\CANtrix Automotive Analyse Splashscreen.png;Bilder" ^
    --add-data "%PROJECT%\Bilder\CANtrix-Icon.png;Bilder" ^
    --add-data "%PROJECT%\Bilder\CANtrix.ico;Bilder" ^
    --add-data "%PROJECT%\Bilder\CANtrix-Hintergrund.png;Bilder" ^
    --distpath "%PROJECT%\dist" ^
    --workpath "%PROJECT%\build\pyinstaller" ^
    --specpath "%PROJECT%\build" ^
    --collect-all lauterbach.trace32.rcl ^
    %EXTRA% ^
    "%PROJECT%\Sourcen\CANtrix.py"
if errorlevel 1 (
    echo.
    echo Das Erstellen ist fehlgeschlagen.
    goto :end
)

copy /y "%PROJECT%\dist\CANtrix.exe" "%PROJECT%\CANtrix.exe" >nul
echo.
echo Fertig: %PROJECT%\CANtrix.exe

:end
echo.
pause
endlocal
