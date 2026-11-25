@echo off
rem Erstellt in einem Durchlauf (Windows):
rem   1. die exe CANtrix.exe mit PyInstaller (wie "03 - make exe_CANtrix_Windows.bat"),
rem   2. CANtrix.zip mit allen Dateien des Projekts und der neuen exe (wie "04 - make_zip_CANtrix_Windows.bat").
rem Start: Doppelklick auf diese Datei.
rem Ergebnis: CANtrix.exe und CANtrix.zip im Projektordner (im Zip liegt alles im Ordner "CANtrix").
rem Nach erfolgreichem Durchlauf schliesst sich das Fenster automatisch; nach einem Fehler bleibt es offen.
rem Die exe liest Datenbasen, Traces, Panels, Plugins und Konfigurationen aus dem Ordner, in dem sie liegt.
rem Die macOS-App wird hier nicht erstellt (das geht nur auf einem Mac, siehe "05 - make_all_CANtrix.command").
setlocal

cd /d "%~dp0.."
set "PROJECT=%CD%"
set "ZIPFILE=%PROJECT%\CANtrix.zip"
set "WORKDIR="

echo === 1/2: exe erstellen ===
echo.

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
    goto :fail
)

where git >nul 2>nul
if errorlevel 1 (
    echo Git wurde nicht gefunden. Bitte Git installieren: https://git-scm.com/downloads
    goto :fail
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
    goto :fail
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

rem Eigener Arbeitsordner fuer PyInstaller und die Zip-Dateilisten.
set "TEMPDIR=%TEMP%\CANtrix_%RANDOM%_%RANDOM%"
mkdir "%TEMPDIR%"
if errorlevel 1 (
    echo Der temporaere Arbeitsordner konnte nicht erstellt werden.
    goto :fail
)
set "WORKDIR=%TEMPDIR%"

echo Die exe wird erstellt ^(das dauert einige Minuten^) ...
%PYTHON% -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name CANtrix ^
    --icon "%PROJECT%\Bilder\CANtrix.ico" ^
    --paths "%PROJECT%\Sourcen" ^
    --add-data "%PROJECT%\README.md;." ^
    --add-data "%PROJECT%\README_en.md;." ^
    --add-data "%PROJECT%\Screenshots;Screenshots" ^
    --add-data "%PROJECT%\Bilder\CANtrix-Startbildschirm.png;Bilder" ^
    --add-data "%PROJECT%\Bilder\CANtrix-Icon.png;Bilder" ^
    --add-data "%PROJECT%\Bilder\CANtrix.ico;Bilder" ^
    --add-data "%PROJECT%\Bilder\CANtrix-Hintergrund.png;Bilder" ^
    --distpath "%WORKDIR%\dist" ^
    --workpath "%WORKDIR%\build" ^
    --specpath "%WORKDIR%" ^
    --collect-all lauterbach.trace32.rcl ^
    %EXTRA% ^
    "%PROJECT%\Sourcen\CANtrix.py"
if errorlevel 1 (
    echo.
    echo Das Erstellen der exe ist fehlgeschlagen.
    goto :fail
)

copy /y "%WORKDIR%\dist\CANtrix.exe" "%PROJECT%\CANtrix.exe" >nul
if errorlevel 1 (
    echo Die exe konnte nicht in den Projektordner kopiert werden.
    goto :fail
)
echo.
echo exe fertig: %PROJECT%\CANtrix.exe
echo.

echo === 2/2: Zip erstellen ===
echo.

git -c core.quotepath=false ls-files --cached --others --exclude-standard > "%WORKDIR%\all.txt"
git -c core.quotepath=false ls-files --cached --ignored --exclude-standard > "%WORKDIR%\ignored.txt"

echo Erstelle %ZIPFILE% ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; Add-Type -AssemblyName System.IO.Compression.FileSystem; $root=$env:PROJECT; $zip=$env:ZIPFILE; $all=@(Get-Content -LiteralPath (Join-Path $env:WORKDIR 'all.txt') -Encoding UTF8); $ign=@(Get-Content -LiteralPath (Join-Path $env:WORKDIR 'ignored.txt') -Encoding UTF8); $files=@($all | Where-Object { $_ -and ($ign -notcontains $_) -and $_ -ne '.gitignore' -and $_ -ne 'CLAUDE.md' -and $_ -ne 'CANtrix.zip' -and $_ -ne '.DS_Store' -and $_ -notlike '*/.DS_Store' } | Select-Object -Unique); if (Test-Path -LiteralPath (Join-Path $root 'CANtrix.exe') -PathType Leaf) { $files += 'CANtrix.exe'; Write-Host 'Enthalten: CANtrix.exe' }; $app=Join-Path $root 'CANtrix.app'; if (Test-Path -LiteralPath $app -PathType Container) { $files += @(Get-ChildItem -LiteralPath $app -Recurse -File -Force | ForEach-Object { $_.FullName.Substring($root.Length + 1).Replace('\','/') }); Write-Host 'Enthalten: CANtrix.app' }; if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip }; $z=[IO.Compression.ZipFile]::Open($zip,'Create'); try { foreach ($f in $files) { $p=Join-Path $root $f; if (Test-Path -LiteralPath $p -PathType Leaf) { [void][IO.Compression.ZipFileExtensions]::CreateEntryFromFile($z,$p,'CANtrix/'+$f,'Optimal') } } } finally { $z.Dispose() }; Write-Host ('' + $files.Count + ' Dateien im Zip')"
if errorlevel 1 (
    echo.
    echo Das Zip konnte nicht erstellt werden.
    goto :fail
)

rmdir /s /q "%WORKDIR%"
echo.
echo Fertig: %ZIPFILE%
endlocal
exit /b 0

:fail
echo.
if defined WORKDIR rmdir /s /q "%WORKDIR%"
pause
endlocal
exit /b 1
