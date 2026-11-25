#!/bin/bash
# Erstellt alles in einem Durchlauf:
#   1. die macOS-App CANtrix.app mit PyInstaller (wie "03 - make exe_CANtrix_MacOS.command"),
#   2. die Windows-exe CANtrix.exe: Der GitHub-Workflow "Build Windows exe" (.github/workflows/build-windows.yml)
#      wird gestartet, abgewartet und das Ergebnis geladen. Braucht die GitHub-CLI "gh" (brew install gh, danach
#      einmal "gh auth login"). Gebaut wird der Stand auf GitHub, also vorher committen und pushen.
#   3. CANtrix.zip mit allen Dateien des Projekts, der App und der exe (wie "04 - make_zip_CANtrix_MacOS.command").
# Start: Doppelklick auf diese Datei im Finder oder im Terminal: "./Batch/05 - make_all_CANtrix.command"
# Ergebnis: CANtrix.app, CANtrix.exe und CANtrix.zip im Projektordner (im Zip liegt alles im Ordner "CANtrix/").
# Nach erfolgreichem Durchlauf schließt sich das Terminalfenster automatisch; nach einem Fehler (auch bei der exe) bleibt es offen.
# Die App liest Datenbasen, Traces, Panels, Plugins und Konfigurationen aus dem Ordner, in dem sie liegt.

cd "$(dirname "$0")/.." || exit 1
PROJECT="$PWD"
ZIP="$PROJECT/CANtrix.zip"

pause_and_exit() {
    read -r -p "Zum Beenden Enter drücken ..."
    exit "$1"
}

for tool in python3 git zip; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "'$tool' wurde nicht gefunden."
        [ "$tool" = "python3" ] && echo "Bitte Python 3 von https://www.python.org/downloads/ installieren."
        pause_and_exit 1
    fi
done

echo "Verwendetes Python:"
python3 --version
echo

# ---------------------------------------------------------------- 1. App

echo "=== 1/3: App erstellen ==="
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
echo

# Ein eigener Arbeitsordner für PyInstaller, den Download und die Zip-Dateilisten.
if ! TMP="$(mktemp -d)"; then
    echo "Der temporäre Arbeitsordner konnte nicht erstellt werden."
    pause_and_exit 1
fi
trap 'rm -rf "$TMP"' EXIT

echo "Die App wird erstellt (das dauert einige Minuten) ..."
if ! python3 -m PyInstaller --noconfirm --clean --windowed \
    --name CANtrix \
    --icon "$PROJECT/Bilder/CANtrix.icns" \
    --osx-bundle-identifier de.cantrix.app \
    --paths "$PROJECT/Sourcen" \
    --add-data "$PROJECT/README.md:." \
    --add-data "$PROJECT/README_en.md:." \
    --add-data "$PROJECT/Screenshots:Screenshots" \
    --add-data "$PROJECT/Bilder/CANtrix-Startbildschirm.png:Bilder" \
    --add-data "$PROJECT/Bilder/CANtrix-Icon.png:Bilder" \
    --add-data "$PROJECT/Bilder/CANtrix-Hintergrund.png:Bilder" \
    --distpath "$TMP/dist" \
    --workpath "$TMP/build" \
    --specpath "$TMP" \
    --collect-all lauterbach.trace32.rcl \
    "${EXTRA[@]}" \
    "$PROJECT/Sourcen/CANtrix.py"; then
    echo
    echo "Das Erstellen der App ist fehlgeschlagen."
    pause_and_exit 1
fi

rm -rf "$PROJECT/CANtrix.app"
if ! cp -R "$TMP/dist/CANtrix.app" "$PROJECT/CANtrix.app"; then
    echo "Die App konnte nicht in den Projektordner kopiert werden."
    pause_and_exit 1
fi
echo
echo "App fertig: $PROJECT/CANtrix.app"
echo

# ---------------------------------------------------------------- 2. Windows-exe (GitHub)

echo "=== 2/3: Windows-exe von GitHub ==="
echo

EXE_FAILED=0
WORKFLOW="build-windows.yml"

windows_exe() {
    if ! command -v gh >/dev/null 2>&1; then
        echo "Die GitHub-CLI 'gh' wurde nicht gefunden (Installation: brew install gh, danach: gh auth login)."
        return 1
    fi
    if ! gh auth status >/dev/null 2>&1; then
        echo "Die GitHub-CLI ist nicht angemeldet (Anmeldung: gh auth login)."
        return 1
    fi
    local branch
    branch="$(git rev-parse --abbrev-ref HEAD)"
    git fetch --quiet origin 2>/dev/null
    # Gebaut wird der Stand auf GitHub: Auf lokale Änderungen und nicht gepushte Commits hinweisen.
    local hints=""
    [ -n "$(git status --porcelain --untracked-files=no)" ] && hints="$hints- Es gibt nicht committete Änderungen.\n"
    [ "$(git rev-parse HEAD)" != "$(git rev-parse "origin/$branch" 2>/dev/null)" ] \
        && hints="$hints- Der lokale Stand ('$branch') ist nicht auf GitHub (origin/$branch).\n"
    if [ -n "$hints" ]; then
        printf 'Die exe wird aus dem Stand auf GitHub gebaut, nicht aus dem lokalen:\n%b' "$hints"
        local answer
        read -r -p "Trotzdem fortfahren? [j/N] " answer
        case "$answer" in j|J|y|Y) ;; *) echo "Abgebrochen."; return 1 ;; esac
    fi

    local previous run
    previous="$(gh run list --workflow "$WORKFLOW" --limit 1 --json databaseId -q '.[0].databaseId' 2>/dev/null)"
    echo "Workflow wird gestartet ($branch) ..."
    if ! gh workflow run "$WORKFLOW" --ref "$branch"; then
        echo "Der Workflow konnte nicht gestartet werden (ist $WORKFLOW auf GitHub gepusht?)."
        return 1
    fi
    # Auf den neuen Lauf warten (die Liste braucht einen Moment).
    run=""
    for _ in $(seq 1 30); do
        sleep 2
        run="$(gh run list --workflow "$WORKFLOW" --limit 1 --json databaseId -q '.[0].databaseId' 2>/dev/null)"
        [ -n "$run" ] && [ "$run" != "$previous" ] && break
        run=""
    done
    if [ -z "$run" ]; then
        echo "Der gestartete Lauf wurde nicht gefunden."
        return 1
    fi
    echo "Lauf $run: Das Erstellen läuft auf GitHub (dauert einige Minuten) ..."
    if ! gh run watch "$run" --exit-status >/dev/null; then
        echo "Der Lauf ist fehlgeschlagen: $(gh run view "$run" --json url -q .url 2>/dev/null)"
        return 1
    fi
    if ! gh run download "$run" -n CANtrix-windows -D "$TMP/windows" || [ ! -f "$TMP/windows/CANtrix.exe" ]; then
        echo "Die exe konnte nicht geladen werden."
        return 1
    fi
    if ! cp "$TMP/windows/CANtrix.exe" "$PROJECT/CANtrix.exe"; then
        echo "Die exe konnte nicht in den Projektordner kopiert werden."
        return 1
    fi
    echo
    echo "exe fertig: $PROJECT/CANtrix.exe"
}

if ! windows_exe; then
    EXE_FAILED=1
    echo
    echo "Die Windows-exe wurde nicht erstellt. Das Zip wird trotzdem erstellt"
    [ -f "$PROJECT/CANtrix.exe" ] && echo "(es enthält dann die ältere CANtrix.exe aus dem Projektordner)."
fi
echo

# ---------------------------------------------------------------- 3. Zip

echo "=== 3/3: Zip erstellen ==="
echo

# Dateien: versioniert oder neu, ohne die von der .gitignore ausgeschlossenen.
git -c core.quotepath=false ls-files --cached --others --exclude-standard > "$TMP/all.txt"
git -c core.quotepath=false ls-files --cached --ignored --exclude-standard > "$TMP/ignored.txt"

: > "$TMP/list.txt"
while IFS= read -r f; do
    [ -f "$f" ] || continue
    case "$f" in
        .gitignore|CLAUDE.md|CANtrix.zip|.DS_Store|*/.DS_Store) continue ;;
    esac
    grep -Fxq -- "$f" "$TMP/ignored.txt" && continue
    printf 'CANtrix/%s\n' "$f" >> "$TMP/list.txt"
done < "$TMP/all.txt"

# Programme, falls vorhanden (die App wurde eben erstellt).
[ -d "CANtrix.app" ] && echo "CANtrix/CANtrix.app" >> "$TMP/list.txt"
[ -f "CANtrix.exe" ] && echo "CANtrix/CANtrix.exe" >> "$TMP/list.txt"

# Das Zip bekommt den Ordner "CANtrix" als Wurzel.
ln -s "$PROJECT" "$TMP/CANtrix"
rm -f "$ZIP"
echo "Erstelle $ZIP ..."
if ! (cd "$TMP" && zip -q -r -y -@ "$ZIP" < "$TMP/list.txt"); then
    echo
    echo "Das Zip konnte nicht erstellt werden."
    pause_and_exit 1
fi

echo
echo "Fertig: $ZIP ($(du -h "$ZIP" | cut -f1))"
echo "Enthalten: CANtrix.app"
[ -f "CANtrix.exe" ] && echo "Enthalten: CANtrix.exe"
echo
if [ "$EXE_FAILED" = 1 ]; then
    echo "Achtung: Die Windows-exe wurde nicht neu erstellt (siehe oben)."
    pause_and_exit 1
fi
echo "Beim ersten Start auf einem anderen Mac: Rechtsklick auf die App > Öffnen (die App ist nicht signiert)."

# Ohne Fehler das eigene Terminalfenster schließen, sobald dieses Skript beendet ist
# (nur wenn es in Terminal.app läuft; das Fenster wird über sein tty erkannt). Nach einem Fehler bleibt es offen.
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
