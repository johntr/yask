#!/usr/bin/env bash
# Removes the Steam Games runner and everything it installed.
set -euo pipefail

SERVICE="io.github.johntr.yask"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"

# match the absolute installed path so nothing else gets caught
pkill -f "^${DATA_HOME}/yask/yask.py$" 2>/dev/null || true

rm -rf "$DATA_HOME/yask"
rm -f  "$DATA_HOME/krunner/dbusplugins/plasma-runner-yask.desktop"
rm -f  "$DATA_HOME/dbus-1/services/$SERVICE.service"

dbus-send --session --dest=org.freedesktop.DBus --type=method_call \
    / org.freedesktop.DBus.ReloadConfig 2>/dev/null || true

if pgrep -x krunner >/dev/null; then
    kquitapp6 krunner 2>/dev/null || killall krunner 2>/dev/null || true
    sleep 1
    (setsid krunner --daemon >/dev/null 2>&1 &)
fi

echo "Uninstalled."
