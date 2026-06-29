#!/usr/bin/env bash
# Build Pack Sync for Linux (Arch)
# Requirements: Python 3.11+ must be installed
# Internet access is needed on first run to install PyInstaller + Pillow + pystray.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# If not running in a terminal (e.g. double-clicked in file manager),
# relaunch this script inside the first available terminal emulator.
if [ ! -t 1 ]; then
    for term in konsole gnome-terminal kitty alacritty xfce4-terminal xterm; do
        if command -v "$term" &>/dev/null; then
            case "$term" in
                konsole)        exec konsole -e bash "$0" ;;
                gnome-terminal) exec gnome-terminal -- bash "$0" ;;
                kitty)          exec kitty bash "$0" ;;
                alacritty)      exec alacritty -e bash "$0" ;;
                xfce4-terminal) exec xfce4-terminal -x bash "$0" ;;
                xterm)          exec xterm -e bash "$0" ;;
            esac
        fi
    done
    echo "No terminal emulator found. Run this script from a terminal." >&2
    exit 1
fi

set -euo pipefail

cd "$SCRIPT_DIR"

echo
echo "  =========================================="
echo "    Pack Sync Builder | Linux x64"
echo "  =========================================="
echo

# ── Install system dependencies (requires sudo) ───────────────────────────────
echo "  Checking system dependencies ..."
MISSING_PKGS=()
for pkg in python tk; do
    if ! pacman -Q "$pkg" &>/dev/null; then
        MISSING_PKGS+=("$pkg")
    fi
done

if [ ${#MISSING_PKGS[@]} -gt 0 ]; then
    echo "  Installing missing packages: ${MISSING_PKGS[*]}"
    sudo pacman -S --noconfirm "${MISSING_PKGS[@]}"
    echo "  Done."
else
    echo "  All system dependencies already installed."
fi
echo

# ── Find Python 3.11+ ─────────────────────────────────────────────────────────
PY=""
for candidate in python3 python; do
    if command -v "$candidate" &>/dev/null; then
        ver=$("$candidate" -c "import sys; print(sys.version_info >= (3,11))" 2>/dev/null || true)
        if [ "$ver" = "True" ]; then
            PY="$candidate"
            break
        fi
    fi
done

if [ -z "$PY" ]; then
    echo "  ERROR: Python 3.11+ not found."
    echo "  Install it with:  sudo pacman -S python"
    exit 1
fi

echo "  Python found:"
"$PY" --version
echo

# ── Stop any running Pack Sync instance ───────────────────────────────────────
echo "  Stopping any running Pack Sync ..."
pkill -f "PackSync" 2>/dev/null && echo "  (killed running instance)" || echo "  (none running)"
echo

# ── Set up a virtual environment (Arch's system Python has no pip) ────────────
VENV_DIR="$SCRIPT_DIR/.venv"
if [ ! -f "$VENV_DIR/bin/python" ]; then
    echo "  Creating virtual environment ..."
    "$PY" -m venv "$VENV_DIR"
    echo "  Done."
    echo
fi
PY="$VENV_DIR/bin/python"

# ── Run the build ─────────────────────────────────────────────────────────────
echo "  Running build.py ..."
echo

if "$PY" build.py; then
    echo
    echo "  ============================================================"
    echo "    Build complete!"
    echo "    Output: dist/linux-x64/PackSync"
    echo "  ============================================================"
    echo

    # Open the output folder in the file manager if one is available
    OUT_DIR=""
    for d in dist/linux-*/; do
        if [ -f "${d}PackSync" ]; then
            OUT_DIR="$d"
            break
        fi
    done

    if [ -n "$OUT_DIR" ]; then
        echo "  Output folder: $SCRIPT_DIR/$OUT_DIR"
        if command -v xdg-open &>/dev/null; then
            xdg-open "$SCRIPT_DIR/$OUT_DIR" 2>/dev/null &
        fi
    fi
else
    echo
    echo "  BUILD FAILED. Check the errors above."
    echo
fi

echo
read -rp "  Press Enter to close ..."
