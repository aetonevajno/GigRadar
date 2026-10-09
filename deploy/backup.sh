#!/usr/bin/env bash
set -euo pipefail

backup_dir=${BACKUP_DIR:?Set BACKUP_DIR to a private backup directory}
mkdir -p "$backup_dir"
chmod 700 "$backup_dir"
backup_file="$backup_dir/gigradar-$(date -u +%Y%m%dT%H%M%SZ).dump"
temporary_file="$backup_file.partial"
trap 'rm -f "$temporary_file"' EXIT
docker compose -f docker-compose.yaml -f deploy/compose.prod.yaml exec -T postgres \
  pg_dump -U gigradar -d gigradar -Fc > "$temporary_file"
chmod 600 "$temporary_file"
mv "$temporary_file" "$backup_file"
printf '%s\n' "$backup_file"
