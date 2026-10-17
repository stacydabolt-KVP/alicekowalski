#!/bin/sh
# Обёртка: ставит зависимость при первом запуске и вызывает add_photos.py
DIR="$(cd "$(dirname "$0")" && pwd)"
if [ ! -x "$DIR/.venv/bin/python" ]; then
  python3 -m venv "$DIR/.venv" && "$DIR/.venv/bin/pip" install -q cryptography
fi
exec "$DIR/.venv/bin/python" "$DIR/add_photos.py" "$@"
