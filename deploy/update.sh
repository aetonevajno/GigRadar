#!/usr/bin/env bash
set -euo pipefail

target_commit=${1:?Pass the tested main commit SHA}
if [[ ! $target_commit =~ ^[0-9a-f]{40}$ ]]; then
  printf 'Expected a full Git commit SHA\n' >&2
  exit 2
fi

repo_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_dir"

exec 9>"$repo_dir/.git/deploy.lock"
flock 9

if [[ ! -f .env ]]; then
  printf 'Create %s/.env before deploying\n' "$repo_dir" >&2
  exit 1
fi
if [[ $(git branch --show-current) != main ]]; then
  printf 'The deployment checkout must be on main\n' >&2
  exit 1
fi
if [[ -n $(git status --porcelain) ]]; then
  printf 'The deployment checkout has local changes; refusing to overwrite them\n' >&2
  exit 1
fi

git fetch --no-tags origin main
if [[ $(git rev-parse origin/main) != "$target_commit" ]]; then
  printf 'A newer main commit exists; skipping %s\n' "$target_commit"
  exit 0
fi

compose=(docker compose -f docker-compose.yaml -f deploy/compose.prod.yaml -f deploy/compose.shared-vps.yaml)
"${compose[@]}" config --quiet
existing_database=$("${compose[@]}" ps -a -q postgres)
if [[ -n $existing_database ]]; then
  if [[ -z $("${compose[@]}" ps --status running -q postgres) ]]; then
    printf 'PostgreSQL is stopped; start it and back it up before deploying\n' >&2
    exit 1
  fi
  BACKUP_DIR=${BACKUP_DIR:-$HOME/gigradar-backups} bash deploy/backup.sh
fi

git merge --ff-only "$target_commit"
"${compose[@]}" config --quiet
"${compose[@]}" build
"${compose[@]}" up -d --no-build
sleep 5
for service in postgres api bot collector notifier db-monitor prometheus grafana web; do
  if [[ -z $("${compose[@]}" ps --status running -q "$service") ]]; then
    printf 'Service %s is not running after deployment\n' "$service" >&2
    "${compose[@]}" ps >&2
    exit 1
  fi
done
"${compose[@]}" exec -T api python -c \
  "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5).close()"
printf 'Deployed %s\n' "$target_commit"
