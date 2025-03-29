#!/bin/bash
LOG_FILE="/tmp/tmux_viewer.log"

while true; do
  {
    echo "=== $(date) ==="
    uv run src/my_small_tools/tmux_viewer.py
  } > "$LOG_FILE"
  sleep 5
done
