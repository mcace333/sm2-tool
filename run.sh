#!/bin/bash
# SM2 Tool Launcher

export DISPLAY=:0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# If not running inside a terminal, relaunch in konsole
if [ ! -t 1 ]; then
    konsole --noclose -e bash "$(realpath "$0")" "$@"
    exit
fi

cd "$SCRIPT_DIR"

if [ ! -d ".venv" ]; then
    echo "Erstelle virtuelles Environment..."
    python3 -m venv --system-site-packages .venv
    echo "Installiere Abhängigkeiten..."
    .venv/bin/pip install -r requirements.txt
fi

# Selbstheilung: falls requirements.txt erweitert wurde (z. B. numpy/pytesseract
# für die OCR-Auswertung), bei fehlenden Importen nachinstallieren.
if ! .venv/bin/python3 -c "import numpy, pytesseract, PIL" 2>/dev/null; then
    echo "Installiere/aktualisiere Abhängigkeiten..."
    .venv/bin/pip install -r requirements.txt
fi

# tesseract-Binary ist ein System-Paket (kein pip) – nur für den OCR-Autofill nötig.
if ! command -v tesseract >/dev/null 2>&1; then
    echo "[WARN] tesseract nicht gefunden – OCR-Autofill (Namen/Gene-Seed/Chips) deaktiviert."
    echo "       Installieren mit: sudo pacman -S tesseract tesseract-data-eng"
fi

.venv/bin/python3 sm2_tool.py "$@"
