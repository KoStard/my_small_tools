#!/bin/bash
while true; do
  clear
  uv run src/my_small_tools/tmux_viewer.py || echo "Error occurred at $(date)"
  sleep 5
done
