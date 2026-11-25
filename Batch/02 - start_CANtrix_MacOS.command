#!/bin/bash
# Startet den CANtrix (macOS).
# Start: Doppelklick auf diese Datei im Finder oder im Terminal: "./Batch/02 - start_CANtrix_MacOS.command" [konfig.canconfig]
# Nach dem Beenden schließt sich das Terminalfenster automatisch; nach einem Fehler bleibt es offen.

cd "$(dirname "$0")/.." || exit 1

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 wurde nicht gefunden."
    echo "Bitte Python 3 von https://www.python.org/downloads/ installieren."
    read -r -p "Zum Beenden Enter drücken ..."
    exit 1
fi

if ! python3 Sourcen/CANtrix.py "$@"; then
    echo
    echo "Der CANtrix wurde mit einem Fehler beendet."
    read -r -p "Zum Beenden Enter drücken ..."
    exit 1
fi

# Das eigene Terminalfenster schließen, sobald dieses Skript beendet ist
# (nur wenn es in Terminal.app läuft; das Fenster wird über sein tty erkannt).
if [ "$TERM_PROGRAM" = "Apple_Terminal" ]; then
    osascript - "$(tty)" >/dev/null 2>&1 <<'APPLESCRIPT' &
on run argv
    delay 0.5
    tell application "Terminal"
        repeat with w in windows
            if tty of selected tab of w is item 1 of argv then
                close w
                exit repeat
            end if
        end repeat
    end tell
end run
APPLESCRIPT
fi
exit 0
