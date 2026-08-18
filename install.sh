#!/usr/bin/env bash
# Installs the Steam Games runner for the current user. No root required.
set -euo pipefail

SERVICE="io.github.johntr.yask"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"

RUNNER_DIR="$DATA_HOME/yask"
PLUGIN_DIR="$DATA_HOME/krunner/dbusplugins"
DBUS_DIR="$DATA_HOME/dbus-1/services"

command -v python3 >/dev/null || { echo "error: python3 not found" >&2; exit 1; }
python3 -c "import dbus, gi" 2>/dev/null || {
    echo "error: missing Python dependencies (dbus-python and PyGObject)." >&2
    echo "  Arch:   sudo pacman -S python-dbus python-gobject" >&2
    echo "  Fedora: sudo dnf install python3-dbus python3-gobject" >&2
    echo "  Debian: sudo apt install python3-dbus python3-gi" >&2
    exit 1
}

mkdir -p "$RUNNER_DIR" "$PLUGIN_DIR" "$DBUS_DIR"
install -m 755 "$SRC_DIR/src/yask.py" "$RUNNER_DIR/yask.py"
install -m 644 "$SRC_DIR/plasma-runner-yask.desktop" "$PLUGIN_DIR/plasma-runner-yask.desktop"

cat > "$DBUS_DIR/$SERVICE.service" <<SERVICE_EOF
[D-BUS Service]
Name=$SERVICE
Exec=$RUNNER_DIR/yask.py
SERVICE_EOF

# Make the session bus notice the new activatable service without a re-login.
# stop any running copy so an upgrade actually takes effect; D-Bus will
# activate the new one on the next query
pkill -f "$RUNNER_DIR/yask.py" 2>/dev/null || true

dbus-send --session --dest=org.freedesktop.DBus --type=method_call \
    / org.freedesktop.DBus.ReloadConfig 2>/dev/null || true

echo "Installed to $RUNNER_DIR"
found=$("$RUNNER_DIR/yask.py" --list | tail -1)
echo "$found"

if pgrep -x krunner >/dev/null; then
    echo "Restarting KRunner..."
    kquitapp6 krunner 2>/dev/null || killall krunner 2>/dev/null || true
    sleep 1
    (setsid krunner --daemon >/dev/null 2>&1 &)
fi

echo "Done. Open KRunner (Alt+Space) and type a game name."
