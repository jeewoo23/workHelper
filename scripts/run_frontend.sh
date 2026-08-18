#!/bin/zsh
set -e
cd "$(dirname "$0")/.."
if [[ -f .env ]]; then
  set -a
  source .env
  set +a
fi
echo "Route Controller frontend: http://localhost:8765/"
uv run --extra device route-controller serve --host 127.0.0.1 --port 8765
