#!/bin/bash
# Installs the Python packages used by CANtrix (macOS).
# Start: double-click this file in the Finder or run it in the Terminal:
#        "./Batch/01 - install_python-can_MacOS.command"
#
# Installed for the current user with pip:
#   python-can              CAN interface library: live recording and sending (Trace Recorder, Generator)
#   python-can-usbtingo     python-can plugin for the USBtingo adapter (recording, Logic Analyzer)
#   lauterbach-trace32-rcl  remote API of Lauterbach TRACE32 (reading variables in the Trace Viewer)
#   pyserial                serial port access: sigrok-pico (Logic Analyzer), OBDLink / ELM327 adapters
#   bleak                   Bluetooth Low Energy library (BLE Monitor)
# Installed with Homebrew:
#   libusb                  USB access library needed by the USBtingo
# Only python-can is required; without one of the others only its function is missing.

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 was not found."
    echo "Please install Python 3 from https://www.python.org/downloads/"
    read -r -p "Press Enter to exit ..."
    exit 1
fi

echo "Python used:"
python3 --version
echo

# pip: the Python package installer; an up-to-date version avoids installation problems.
echo "Updating pip ..."
python3 -m pip install --user --upgrade pip
echo

# python-can: access to CAN hardware. Required, so a failure stops the script.
echo "Installing python-can (CAN interfaces) ..."
if ! python3 -m pip install --user --upgrade python-can; then
    echo
    echo "The installation failed."
    read -r -p "Press Enter to exit ..."
    exit 1
fi
echo

# python-can-usbtingo: driver plugin for the USBtingo USB-CAN adapter.
echo "Installing python-can-usbtingo (USBtingo adapter) ..."
if ! python3 -m pip install --user --upgrade python-can-usbtingo; then
    echo "Note: python-can-usbtingo could not be installed. Only needed for the USBtingo."
fi
echo

# lauterbach-trace32-rcl: connection to a running Lauterbach TRACE32 debugger.
echo "Installing lauterbach-trace32-rcl (TRACE32 debugger) ..."
if ! python3 -m pip install --user --upgrade lauterbach-trace32-rcl; then
    echo "Note: lauterbach-trace32-rcl could not be installed. Only needed for TRACE32."
fi
echo

# pyserial: serial communication with OBDLink / ELM327 adapters.
echo "Installing pyserial (OBDLink / ELM327 adapters) ..."
if ! python3 -m pip install --user --upgrade pyserial; then
    echo "Note: pyserial could not be installed. Only needed for OBDLink / ELM327."
fi
echo

# bleak: receiving Bluetooth Low Energy advertising packets.
echo "Installing bleak (BLE Monitor) ..."
if ! python3 -m pip install --user --upgrade bleak; then
    echo "Note: bleak could not be installed. Only needed for the BLE Monitor."
fi
echo

# libusb: system library (not a Python package) that the USBtingo needs. Installed with Homebrew,
# which is also searched in its usual folders because a double click gives a short PATH.
echo "Checking libusb (needed for the USBtingo) ..."
BREW=""
if command -v brew >/dev/null 2>&1; then
    BREW="$(command -v brew)"
elif [ -x /opt/homebrew/bin/brew ]; then
    BREW=/opt/homebrew/bin/brew
elif [ -x /usr/local/bin/brew ]; then
    BREW=/usr/local/bin/brew
fi
if [ -z "$BREW" ]; then
    echo "Note: Homebrew was not found (https://brew.sh). For the USBtingo please install libusb: brew install libusb"
elif "$BREW" list libusb >/dev/null 2>&1; then
    echo "libusb is already installed."
elif ! "$BREW" install libusb; then
    echo "Note: libusb could not be installed. Only needed for the USBtingo."
fi
echo

# Check that python-can can be imported.
echo "Check:"
python3 -c "import can; print('python-can', can.__version__, 'is installed.')"
echo
read -r -p "Press Enter to exit ..."
