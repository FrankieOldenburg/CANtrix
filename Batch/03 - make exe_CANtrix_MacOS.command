#!/bin/bash
# Erstellt aus dem CANtrix ein eigenständiges Programm (CANtrix.app) für macOS mit PyInstaller.
# Start: Doppelklick auf diese Datei im Finder oder im Terminal: "./Batch/03 - make exe_CANtrix_MacOS.command"
# Ergebnis: dist/CANtrix.app, eine Kopie liegt im Projektordner (neben Database, Traces, Plugins ...).
# Die App liest Datenbasen, Traces, Panels, Plugins und Konfigurationen aus dem Ordner, in dem sie liegt.

cd "$(dirname "$0")/.." || exit 1
PROJECT="$PWD"

pause_and_exit() {
    [ -n "$CAN_NO_PAUSE" ] && exit "$1"  # vom Release-Skript gesetzt: kein Warten auf Enter
    read -r -p "Zum Beenden Enter drücken ..."
    exit "$1"
}

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 wurde nicht gefunden."
    echo "Bitte Python 3 von https://www.python.org/downloads/ installieren."
    pause_and_exit 1
fi

echo "Verwendetes Python:"
python3 --version
echo

# In einer virtuellen Umgebung geht --user nicht.
USER_FLAG="--user"
if python3 -c "import sys; sys.exit(0 if sys.prefix != sys.base_prefix else 1)"; then
    USER_FLAG=""
fi

echo "PyInstaller, TRACE32-RCL und pyserial werden installiert bzw. aktualisiert ..."
if ! python3 -m pip install $USER_FLAG --upgrade pyinstaller lauterbach-trace32-rcl pyserial; then
    echo
    echo "Die Installation der Build-Abhängigkeiten ist fehlgeschlagen."
    pause_and_exit 1
fi
echo

# python-can (für den Trace Recorder) nur mitnehmen, wenn es installiert ist.
EXTRA=()
if python3 -c "import can" >/dev/null 2>&1; then
    echo "python-can ist installiert und wird mit eingebunden."
    EXTRA=(--collect-all can)
else
    echo "python-can ist nicht installiert: Der Trace Recorder funktioniert in der App nicht"
    echo "(Installation: 01 - install_python-can_MacOS.command, danach dieses Skript erneut starten)."
fi
# bleak (für den BLE Monitor) ebenfalls nur, wenn es installiert ist.
if python3 -c "import bleak" >/dev/null 2>&1; then
    EXTRA+=(--collect-all bleak)
else
    echo "bleak ist nicht installiert: Der BLE Monitor funktioniert in der App nicht."
fi
echo

echo "Die App wird erstellt (das dauert einige Minuten) ..."
rm -rf "$PROJECT/build/pyinstaller" "$PROJECT/dist/CANtrix.app"
if ! python3 -m PyInstaller --noconfirm --clean --windowed \
    --name CANtrix \
    --icon "$PROJECT/Bilder/CANtrix.icns" \
    --osx-bundle-identifier de.cantrix.app \
    --paths "$PROJECT/Sourcen" \
    --add-data "$PROJECT/README.md:." \
    --add-data "$PROJECT/README_en.md:." \
    --add-data "$PROJECT/Screenshots:Screenshots" \
    --add-data "$PROJECT/Bilder/CANtrix Automotive Analyse Splashscreen.png:Bilder" \
    --add-data "$PROJECT/Bilder/CANtrix-Icon.png:Bilder" \
    --add-data "$PROJECT/Bilder/CANtrix-Hintergrund.png:Bilder" \
    --distpath "$PROJECT/dist" \
    --workpath "$PROJECT/build/pyinstaller" \
    --specpath "$PROJECT/build" \
    --collect-all lauterbach.trace32.rcl \
    "${EXTRA[@]}" \
    "$PROJECT/Sourcen/CANtrix.py"; then
    echo
    echo "Das Erstellen ist fehlgeschlagen."
    pause_and_exit 1
fi

# Ohne diese Beschreibung beendet macOS die App beim ersten BLE-Scan. Danach muss neu signiert werden.
if ! /usr/libexec/PlistBuddy -c "Add :NSBluetoothAlwaysUsageDescription string 'CANtrix empfängt Bluetooth-LE-Advertising-Pakete im BLE Monitor.'" \
    "$PROJECT/dist/CANtrix.app/Contents/Info.plist" || ! codesign --force --deep --sign - "$PROJECT/dist/CANtrix.app"; then
    echo "Die Bluetooth-Berechtigung konnte nicht in die App eingetragen werden."
    pause_and_exit 1
fi

rm -rf "$PROJECT/CANtrix.app"
cp -R "$PROJECT/dist/CANtrix.app" "$PROJECT/CANtrix.app"

echo
echo "Fertig: $PROJECT/CANtrix.app"
echo "Beim ersten Start auf einem anderen Mac: Rechtsklick auf die App > Öffnen (die App ist nicht signiert)."
pause_and_exit 0
