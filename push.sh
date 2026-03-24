#!/bin/bash
# Пуш на GitHub используя GITHUB_USERNAME и GITHUB_PAT из .env
cd "$(dirname "$0")"
set -a
source .env 2>/dev/null
set +a
if [ -z "$GITHUB_USERNAME" ] || [ -z "$GITHUB_PAT" ]; then
    echo "Заполни GITHUB_USERNAME и GITHUB_PAT в .env"
    exit 1
fi
git push "https://${GITHUB_USERNAME}:${GITHUB_PAT}@github.com/cashdraft/json_video.git" main
