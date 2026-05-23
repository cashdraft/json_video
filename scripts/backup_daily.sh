#!/usr/bin/env bash
# Ежедневный дамп проекта json_video → /srv/backups/json_video/
# Без срендеренных видео (*.mp4, каталоги montage/remotion output).
# Запуск: вручную или /etc/cron.d/json-video-backup (03:30)

set -euo pipefail

PROJECT_ROOT="/srv/json_video"
BACKUP_ROOT="/srv/backups/json_video"
RETENTION_DAYS="${JSON_VIDEO_BACKUP_RETENTION_DAYS:-7}"
INCLUDE_VENV="${JSON_VIDEO_BACKUP_INCLUDE_VENV:-0}"

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
ARCHIVE_NAME="json_video_${TIMESTAMP}.tar.gz"
ARCHIVE_PATH="${BACKUP_ROOT}/${ARCHIVE_NAME}"
LOCK_FILE="${BACKUP_ROOT}/.backup.lock"
LOG_FILE="${BACKUP_ROOT}/backup.log"

mkdir -p "${BACKUP_ROOT}"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "${LOG_FILE}"
}

cleanup_lock() {
  rm -f "${LOCK_FILE}"
}
trap cleanup_lock EXIT

if [[ -e "${LOCK_FILE}" ]]; then
  log "SKIP: предыдущий бэкап ещё выполняется (lock ${LOCK_FILE})"
  exit 0
fi
echo "$$" > "${LOCK_FILE}"

if [[ ! -d "${PROJECT_ROOT}" ]]; then
  log "ERROR: каталог проекта не найден: ${PROJECT_ROOT}"
  exit 1
fi

EXCLUDES=(
  # Срендеренные / итоговые видео
  --exclude='*.mp4'
  --exclude='data/job_videos'
  --exclude='data/job_remotion'
  --exclude='data/scenes_lab/remotion'
  --exclude='remotion/out'
  --exclude='remotion/scene_renders'
  # Тяжёлые артефакты, восстанавливаются через npm/pip
  --exclude='remotion/node_modules'
  --exclude='remotion/.cache'
  --exclude='remotion/build'
  --exclude='remotion/.remotion'
  --exclude='**/__pycache__'
  --exclude='*.pyc'
  --exclude='*.pyo'
  --exclude='.git/objects'
)

if [[ "${INCLUDE_VENV}" != "1" ]]; then
  EXCLUDES+=(--exclude='.venv')
  EXCLUDES+=(--exclude='venv')
  EXCLUDES+=(--exclude='env')
fi

log "START: ${ARCHIVE_NAME}"
log "  источник: ${PROJECT_ROOT}"
log "  venv в архиве: $([[ "${INCLUDE_VENV}" == "1" ]] && echo да || echo нет)"
log "  без: *.mp4, data/job_videos, data/job_remotion, data/scenes_lab/remotion"

GIT_REV=""
if command -v git >/dev/null 2>&1 && [[ -d "${PROJECT_ROOT}/.git" ]]; then
  GIT_REV="$(git -C "${PROJECT_ROOT}" rev-parse --short HEAD 2>/dev/null || true)"
fi

MANIFEST="${BACKUP_ROOT}/.manifest_${TIMESTAMP}.txt"
{
  echo "created_at=$(date -Iseconds)"
  echo "hostname=$(hostname)"
  echo "project_root=${PROJECT_ROOT}"
  echo "archive=${ARCHIVE_NAME}"
  echo "git_rev=${GIT_REV}"
  echo "include_venv=${INCLUDE_VENV}"
  echo "retention_days=${RETENTION_DAYS}"
  echo "excludes_videos=*.mp4,data/job_videos,data/job_remotion,data/scenes_lab/remotion,remotion/out,remotion/scene_renders"
  echo "excludes_build=remotion/node_modules,caches,__pycache__"
  [[ "${INCLUDE_VENV}" != "1" ]] && echo "note_venv=восстановление: cd ${PROJECT_ROOT} && python3 -m venv .venv && pip install -r requirements.txt"
  echo "note_videos=срендеренные MP4 не в архиве; Remotion/монтаж пересобирается из job JSON + медиа сцен"
} > "${MANIFEST}"

if tar -czf "${ARCHIVE_PATH}" \
  "${EXCLUDES[@]}" \
  -C "$(dirname "${PROJECT_ROOT}")" \
  "$(basename "${PROJECT_ROOT}")" 2>>"${LOG_FILE}"; then
  SIZE="$(du -h "${ARCHIVE_PATH}" | cut -f1)"
  log "OK: ${ARCHIVE_PATH} (${SIZE})"
  mv -f "${MANIFEST}" "${BACKUP_ROOT}/latest_manifest.txt"
else
  log "ERROR: tar failed for ${ARCHIVE_PATH}"
  rm -f "${ARCHIVE_PATH}" "${MANIFEST}"
  exit 1
fi

ln -sfn "${ARCHIVE_NAME}" "${BACKUP_ROOT}/latest.tar.gz"

DELETED=0
while IFS= read -r -d '' old; do
  rm -f "${old}"
  DELETED=$((DELETED + 1))
done < <(find "${BACKUP_ROOT}" -maxdepth 1 -type f -name 'json_video_*.tar.gz' -mtime +"${RETENTION_DAYS}" -print0 2>/dev/null || true)

if [[ "${DELETED}" -gt 0 ]]; then
  log "ROTATE: удалено старых архивов: ${DELETED} (старше ${RETENTION_DAYS} дн.)"
fi

log "DONE"
exit 0
