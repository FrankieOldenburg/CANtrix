@echo off
rem Installs the Python packages used by CANtrix (Windows).
rem Start: double-click this file.
rem
rem Installed for the current user with pip:
rem   python-can              CAN interface library: live recording and sending (Trace Recorder, Generator)
rem   python-can-usbtingo     python-can plugin for the USBtingo adapter (recording, Logic Analyzer)
rem   lauterbach-trace32-rcl  remote API of Lauterbach TRACE32 (reading variables in the Trace Viewer)
rem   pyserial                serial port access: sigrok-pico (Logic Analyzer), OBDLink / ELM327 adapters
rem   bleak                   Bluetooth Low Energy library (BLE Monitor)
rem Only python-can is required; without one of the other packages only its function is missing.

setlocal

rem Find Python: first the Python launcher "py", otherwise "python".
set "PYTHON="
where py >nul 2>nul && set "PYTHON=py -3"
if not defined PYTHON (
    where python >nul 2>nul && set "PYTHON=python"
)
if not defined PYTHON (
    echo Python was not found.
    echo Please install Python 3 from https://www.python.org/downloads/
    echo and check "Add python.exe to PATH".
    goto :end
)

echo Python used:
%PYTHON% --version
echo.

rem pip: the Python package installer; an up-to-date version avoids installation problems.
echo Updating pip ...
%PYTHON% -m pip install --user --upgrade pip
echo.

rem python-can: access to CAN hardware, e.g. Vector devices. Required, so a failure stops the script.
echo Installing python-can ^(CAN interfaces^) ...
%PYTHON% -m pip install --user --upgrade python-can
if errorlevel 1 (
    echo.
    echo The installation failed.
    goto :end
)
echo.

rem python-can-usbtingo: driver plugin for the USBtingo USB-CAN adapter.
echo Installing python-can-usbtingo ^(USBtingo adapter^) ...
%PYTHON% -m pip install --user --upgrade python-can-usbtingo
if errorlevel 1 (
    echo Note: python-can-usbtingo could not be installed. Only needed for the USBtingo.
)
echo.

rem lauterbach-trace32-rcl: connection to a running Lauterbach TRACE32 debugger.
echo Installing lauterbach-trace32-rcl ^(TRACE32 debugger^) ...
%PYTHON% -m pip install --user --upgrade lauterbach-trace32-rcl
if errorlevel 1 (
    echo Note: lauterbach-trace32-rcl could not be installed. Only needed for TRACE32.
)
echo.

rem pyserial: serial communication with OBDLink / ELM327 adapters.
echo Installing pyserial ^(OBDLink / ELM327 adapters^) ...
%PYTHON% -m pip install --user --upgrade pyserial
if errorlevel 1 (
    echo Note: pyserial could not be installed. Only needed for OBDLink / ELM327.
)
echo.

rem bleak: receiving Bluetooth Low Energy advertising packets.
echo Installing bleak ^(BLE Monitor^) ...
%PYTHON% -m pip install --user --upgrade bleak
if errorlevel 1 (
    echo Note: bleak could not be installed. Only needed for the BLE Monitor.
)
echo.

rem Check that python-can can be imported.
echo Check:
%PYTHON% -c "import can; print('python-can', can.__version__, 'is installed.')"
echo.
echo Note: the Vector VN1640A also needs the Vector driver
echo (Vector XL Driver Library). The USBtingo needs no driver of its own.

:end
echo.
pause
endlocal
