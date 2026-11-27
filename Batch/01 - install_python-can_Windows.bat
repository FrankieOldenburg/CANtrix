@echo off
rem Installiert python-can fuer den CAN Trace Recorder (Windows).
rem Start: Doppelklick auf diese Datei.

setlocal

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

echo pip wird aktualisiert ...
%PYTHON% -m pip install --user --upgrade pip
echo.

echo python-can wird installiert ...
%PYTHON% -m pip install --user --upgrade python-can
if errorlevel 1 (
    echo.
    echo Die Installation ist fehlgeschlagen.
    goto :end
)
echo.

echo python-can-usbtingo (fuer den USBtingo) wird installiert ...
%PYTHON% -m pip install --user --upgrade python-can-usbtingo
if errorlevel 1 (
    echo Hinweis: python-can-usbtingo konnte nicht installiert werden. Nur fuer den USBtingo noetig.
)
echo.

echo lauterbach-trace32-rcl wird installiert ...
%PYTHON% -m pip install --user --upgrade lauterbach-trace32-rcl
if errorlevel 1 (
    echo Hinweis: lauterbach-trace32-rcl konnte nicht installiert werden. Nur fuer TRACE32 noetig.
)
echo.

echo pyserial wird installiert ...
%PYTHON% -m pip install --user --upgrade pyserial
if errorlevel 1 (
    echo Hinweis: pyserial konnte nicht installiert werden. Nur fuer OBDLink / ELM327 noetig.
)
echo.

echo bleak wird installiert ...
%PYTHON% -m pip install --user --upgrade bleak
if errorlevel 1 (
    echo Hinweis: bleak konnte nicht installiert werden. Nur fuer den BLE Monitor noetig.
)
echo.

echo Pruefung:
%PYTHON% -c "import can; print('python-can', can.__version__, 'ist installiert.')"
echo.
echo Hinweis: Fuer den Vector VN1640A wird ausserdem der Vector-Treiber
echo (Vector XL Driver Library) benoetigt. Der USBtingo braucht keinen eigenen Treiber.

:end
echo.
pause
endlocal
