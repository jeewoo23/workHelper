#!/bin/zsh
set -e
cd "$(dirname "$0")/.."
echo "Route Controller frontend: http://localhost:8765/"
python3 -m http.server 8765 --bind 127.0.0.1 --directory frontend
