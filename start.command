#!/bin/zsh
# Double-click this in Finder to start AI Pulse; your browser opens on its own.
# Close the Terminal window (or press Ctrl+C) to stop it.
cd "$(dirname "$0")" || exit 1
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
exec "$UV" run pulse
