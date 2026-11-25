#!/bin/bash
# Erstellt ein Release auf GitHub:
#   1. fragt nach der Versionsnummer (z. B. 1.2) und optional nach einem Text für die Release-Notes,
#   2. setzt VERSION in Sourcen/CAN_Info.py auf diese Nummer (falls abweichend) und committet das,
#   3. pusht den Branch, erstellt den Tag "v<Version>" und das Release (der Quellcode-Download liegt automatisch dabei),
#   4. optional: wartet auf den Workflow "Build Windows exe" (startet durch den neuen Tag) und hängt CANtrix.exe an,
#   5. optional: baut die macOS-App (Batch/03) und hängt sie als CANtrix-macOS.zip an.
#      Beide Anhänge nutzt "Info > Auf Update prüfen", um die App im laufenden Betrieb zu ersetzen (je nach System).
# Das Release ist ein normales Release (kein Pre-release), nur dieses findet "Info > Auf Update prüfen" (releases/latest).
# Braucht git und die GitHub-CLI "gh" (brew install gh, danach einmal "gh auth login").
# Start: Doppelklick auf diese Datei im Finder oder im Terminal: "./Batch/06 - make_Release.command"
# Nach erfolgreichem Durchlauf schließt sich das Terminalfenster (Terminal.app); nach einem Fehler oder Abbruch bleibt es offen.

cd "$(dirname "$0")/.." || exit 1
PROJECT="$PWD"
INFO="Sourcen/CAN_Info.py"
WORKFLOW="build-windows.yml"

pause_and_exit() {
    read -r -p "Zum Beenden Enter drücken ..."
    exit "$1"
}

for tool in git gh; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "'$tool' wurde nicht gefunden."
        [ "$tool" = "gh" ] && echo "Installation: brew install gh, danach: gh auth login"
        pause_and_exit 1
    fi
done
if ! gh auth status >/dev/null 2>&1; then
    echo "Die GitHub-CLI ist nicht angemeldet (Anmeldung: gh auth login)."
    pause_and_exit 1
fi

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
CURRENT="$(sed -n "s/^VERSION = '\(.*\)'.*/\1/p" "$INFO" | head -1)"
git fetch --quiet --tags origin 2>/dev/null
LATEST="$(gh release view --json tagName -q .tagName 2>/dev/null)"

echo "Branch:                    $BRANCH"
echo "VERSION in CAN_Info.py:    ${CURRENT:-?}"
echo "Letztes Release auf GitHub: ${LATEST:-keins}"
echo

# ---------------------------------------------------------------- Eingaben

read -r -p "Neue Versionsnummer (z. B. 1.2): " VERSION
VERSION="${VERSION#v}"
if ! [[ "$VERSION" =~ ^[0-9]+(\.[0-9]+){1,2}$ ]]; then
    echo "Ungültige Versionsnummer (erwartet: 1.2 oder 1.2.3)."
    pause_and_exit 1
fi
TAG="v$VERSION"
if git rev-parse -q --verify "refs/tags/$TAG" >/dev/null || git ls-remote --exit-code --tags origin "$TAG" >/dev/null 2>&1; then
    echo "Den Tag $TAG gibt es schon."
    pause_and_exit 1
fi

NOTES=""
read -r -p "Text für die Release-Notes (Enter = automatisch aus den Commits): " NOTES
read -r -p "Windows-exe anhängen (Workflow läuft einige Minuten)? [J/n] " WITH_EXE
case "$WITH_EXE" in n|N) WITH_EXE=0 ;; *) WITH_EXE=1 ;; esac
read -r -p "macOS-App bauen und anhängen (dauert einige Minuten)? [J/n] " WITH_APP
case "$WITH_APP" in n|N) WITH_APP=0 ;; *) WITH_APP=1 ;; esac

DIRTY="$(git status --porcelain --untracked-files=no | grep -v -F " $INFO")"
echo
echo "Es wird erstellt: Release $TAG vom Branch '$BRANCH'"
[ "$CURRENT" != "$VERSION" ] && echo "  - VERSION in $INFO: $CURRENT -> $VERSION (eigener Commit)"
echo "  - Branch wird nach origin gepusht"
[ "$WITH_EXE" = 1 ] && echo "  - CANtrix.exe wird angehängt"
[ "$WITH_APP" = 1 ] && echo "  - CANtrix-macOS.zip (macOS-App) wird gebaut und angehängt"
if [ -n "$DIRTY" ]; then
    echo
    echo "Achtung: Nicht committete Änderungen sind nicht im Release enthalten:"
    echo "$DIRTY"
fi
echo
read -r -p "Fortfahren? [j/N] " ANSWER
case "$ANSWER" in j|J|y|Y) ;; *) echo "Abgebrochen."; pause_and_exit 1 ;; esac
echo

# ---------------------------------------------------------------- Version, Push, Release

if [ "$CURRENT" != "$VERSION" ]; then
    sed -i.bak "s/^VERSION = '.*'/VERSION = '$VERSION'/" "$INFO" && rm -f "$INFO.bak"
    if ! git commit -q -m "Version $VERSION" -- "$INFO"; then
        echo "Der Commit der Versionsnummer ist fehlgeschlagen."
        pause_and_exit 1
    fi
fi

echo "Branch wird gepusht ..."
if ! git push origin "$BRANCH"; then
    echo "Der Push ist fehlgeschlagen."
    pause_and_exit 1
fi

COMMIT="$(git rev-parse HEAD)"
echo "Release $TAG wird erstellt ..."
ARGS=(release create "$TAG" --target "$COMMIT" --title "Version $VERSION")
if [ -n "$NOTES" ]; then
    ARGS+=(--notes "$NOTES")
else
    ARGS+=(--generate-notes)
fi
if ! gh "${ARGS[@]}"; then
    echo "Das Release konnte nicht erstellt werden."
    pause_and_exit 1
fi

# ---------------------------------------------------------------- Windows-exe anhängen

EXE_FAILED=0
if [ "$WITH_EXE" = 1 ]; then
    TMP="$(mktemp -d)"
    trap 'rm -rf "$TMP"' EXIT
    attach_exe() {
        local run=""
        echo
        echo "Warte auf den Workflow zum Tag $TAG ..."
        for _ in $(seq 1 30); do
            run="$(gh run list --workflow "$WORKFLOW" --event push --branch "$TAG" --limit 1 --json databaseId -q '.[0].databaseId' 2>/dev/null)"
            [ -n "$run" ] && break
            sleep 2
        done
        if [ -z "$run" ]; then
            echo "Der Workflow-Lauf zum Tag wurde nicht gefunden."
            return 1
        fi
        echo "Lauf $run: Das Erstellen läuft auf GitHub (dauert einige Minuten) ..."
        if ! gh run watch "$run" --exit-status >/dev/null; then
            echo "Der Lauf ist fehlgeschlagen: $(gh run view "$run" --json url -q .url 2>/dev/null)"
            return 1
        fi
        if ! gh run download "$run" -n CANtrix-windows -D "$TMP" || [ ! -f "$TMP/CANtrix.exe" ]; then
            echo "Die exe konnte nicht geladen werden."
            return 1
        fi
        gh release upload "$TAG" "$TMP/CANtrix.exe" --clobber || return 1
        echo "CANtrix.exe wurde an das Release angehängt."
    }
    if ! attach_exe; then
        EXE_FAILED=1
        echo "Die exe wurde nicht angehängt (nachträglich: gh release upload $TAG CANtrix.exe)."
    fi
fi

# ---------------------------------------------------------------- macOS-App anhängen

APP_FAILED=0
if [ "$WITH_APP" = 1 ]; then
    attach_app() {
        local zip="$PROJECT/dist/CANtrix-macOS.zip"
        echo
        echo "Die macOS-App wird gebaut ..."
        CAN_NO_PAUSE=1 bash "$PROJECT/Batch/03 - make exe_CANtrix_MacOS.command" || return 1
        rm -f "$zip"
        ditto -c -k --keepParent "$PROJECT/dist/CANtrix.app" "$zip" || return 1
        gh release upload "$TAG" "$zip" --clobber || return 1
        echo "CANtrix-macOS.zip wurde an das Release angehängt."
    }
    if ! attach_app; then
        APP_FAILED=1
        echo "Die App wurde nicht angehängt."
    fi
fi

echo
echo "Fertig: $(gh release view "$TAG" --json url -q .url)"
if [ "$EXE_FAILED" = 1 ] || [ "$APP_FAILED" = 1 ]; then
    pause_and_exit 1
fi

# Ohne Fehler das eigene Terminalfenster schließen (nur in Terminal.app; das Fenster wird über sein tty erkannt).
if [ "$TERM_PROGRAM" = "Apple_Terminal" ]; then
    osascript - "$(tty)" >/dev/null 2>&1 <<'APPLESCRIPT' &
on run argv
    delay 1
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
