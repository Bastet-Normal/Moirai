#!/bin/sh
cd -- "$(dirname -- "$0")/.." || exit 1
if [ -n "$MOIRAI_PYTHON" ]; then
    exec "$MOIRAI_PYTHON" scripts/launch.py "$@"
fi
for minor in 14 13 12 11; do
    executable="python3.$minor"
    if command -v "$executable" >/dev/null 2>&1; then
        exec "$executable" scripts/launch.py "$@"
    fi
done
exec python3 scripts/launch.py "$@"
