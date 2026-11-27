#!/bin/bash
# Legt die Verzeichnisstruktur für Datenbasen, Nodes, Panels, Plugins und Traces an (macOS).
# Start: Doppelklick auf diese Datei im Finder. Vorhandene Ordner und Dateien bleiben unverändert.

cd "$(dirname "$0")/.." || exit 1

failed=0
for folder in \
    "Database/Demo" \
    "Database/TRACE32" \
    "Database/VectorIO" \
    "Nodes/Demo" \
    "Panel/Demo" \
    "Plugins" \
    "Traces/Demo"; do
    if [ -d "$folder" ]; then
        echo "vorhanden: $folder"
    elif mkdir -p "$folder"; then
        echo "angelegt:  $folder"
    else
        failed=1
    fi
done

echo
if [ "$failed" -ne 0 ]; then
    echo "Nicht alle Ordner konnten angelegt werden."
else
    echo "Verzeichnisstruktur ist vollständig in: $(pwd)"
fi
read -r -p "Zum Beenden Enter drücken ..."
exit "$failed"
