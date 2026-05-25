#!/usr/bin/env bash
# Download SAM 2.1 tiny checkpoint into sam2_service/checkpoints/
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"
URL="https://dl.fbaipublicfiles.com/segment_anything_2/092824/sam2.1_hiera_tiny.pt"
FILE="sam2.1_hiera_tiny.pt"
if [[ -f "$FILE" ]]; then
  echo "Already exists: $FILE"
  exit 0
fi
echo "Downloading $FILE ..."
curl -L --fail --connect-timeout 30 --max-time 900 -o "$FILE" "$URL"
echo "Done: $DIR/$FILE ($(du -h "$FILE" | cut -f1))"
