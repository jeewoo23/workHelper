#!/bin/zsh
set -e
cd "$(dirname "$0")/.."
echo "Route Controller frontend: http://localhost:8765/"
uv run route-controller serve --host 127.0.0.1 --port 8765
