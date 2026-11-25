#!/bin/bash
# Installiert python-can für den CAN Trace Recorder (macOS).
# Start: Doppelklick auf diese Datei im Finder oder im Terminal: ./install_python-can_macos.command

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 wurde nicht gefunden."
    echo "Bitte Python 3 von https://www.python.org/downloads/ installieren."
    read -r -p "Zum Beenden Enter drücken ..."
    exit 1
fi

echo "Verwendetes Python:"
python3 --version
echo

echo "pip wird aktualisiert ..."
python3 -m pip install --user --upgrade pip
echo

echo "python-can wird installiert ..."
if ! python3 -m pip install --user --upgrade python-can; then
    echo
    echo "Die Installation ist fehlgeschlagen."
    read -r -p "Zum Beenden Enter drücken ..."
    exit 1
fi
echo

echo "python-can-usbtingo (für den USBtingo) wird installiert ..."
if ! python3 -m pip install --user --upgrade python-can-usbtingo; then
    echo "Hinweis: python-can-usbtingo konnte nicht installiert werden. Nur für den USBtingo nötig."
fi
echo

echo "lauterbach-trace32-rcl wird installiert ..."
if ! python3 -m pip install --user --upgrade lauterbach-trace32-rcl; then
    echo "Hinweis: lauterbach-trace32-rcl konnte nicht installiert werden. Nur für TRACE32 nötig."
fi
echo

echo "pyserial wird installiert ..."
if ! python3 -m pip install --user --upgrade pyserial; then
    echo "Hinweis: pyserial konnte nicht installiert werden. Nur für OBDLink / ELM327 nötig."
fi
echo

echo "libusb (für den USBtingo) wird geprüft ..."
BREW=""
if command -v brew >/dev/null 2>&1; then
    BREW="$(command -v brew)"
elif [ -x /opt/homebrew/bin/brew ]; then
    BREW=/opt/homebrew/bin/brew
elif [ -x /usr/local/bin/brew ]; then
    BREW=/usr/local/bin/brew
fi
if [ -z "$BREW" ]; then
    echo "Hinweis: Homebrew wurde nicht gefunden (https://brew.sh). Für den USBtingo bitte libusb installieren: brew install libusb"
elif "$BREW" list libusb >/dev/null 2>&1; then
    echo "libusb ist bereits installiert."
elif ! "$BREW" install libusb; then
    echo "Hinweis: libusb konnte nicht installiert werden. Nur für den USBtingo nötig."
fi
echo

echo "Prüfung:"
python3 -c "import can; print('python-can', can.__version__, 'ist installiert.')"
echo
read -r -p "Zum Beenden Enter drücken ..."
