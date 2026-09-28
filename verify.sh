#!/bin/bash
# waiting-line — verified in AppPlayer. Prerequisites: tools/appplayer.py header.
set -euo pipefail
cd "$(dirname "$0")"
echo "   [1/2] waiting_server (dart analyze)"
( cd waiting_server && dart pub get >/dev/null && dart analyze | tail -1 )
echo "   [2/2] open in AppPlayer, drive it, capture"
rm -f captures/*.png
python3 verify.py
COUNT=$(ls captures/*.png | wc -l | tr -d ' ')
[ "$COUNT" -eq 4 ] || { echo "   expected 4 captures, got $COUNT"; exit 1; }
