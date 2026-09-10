#!/usr/bin/env bash
set -euo pipefail
umask 077

release_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
release_sha=$(basename "$release_dir")
reason=${1:-manual}
if [[ $EUID -ne 0 || ! $release_sha =~ ^[0-9a-f]{40}$ || ! $reason =~ ^[a-zA-Z0-9_-]+$ ]]; then
  echo 'Run with sudo from an installed release; reason must contain only letters, digits, underscores or hyphens.' >&2
  exit 1
fi
[[ "$release_dir" == "/opt/tt-ada/releases/$release_sha" ]]
compose="$release_dir/deploy/production-compose.sh"

# 防止定时备份与发版前备份交叠；不停止其他应用或操作其数据库。
exec 9>/opt/tt-ada/backups/.lock
flock -w 60 9
backup_dir="/opt/tt-ada/backups/$(date -u +%Y%m%dT%H%M%SZ)-$reason"
mkdir -m 0700 "$backup_dir"
"$compose" exec -T db pg_dump -U postgres -d app --format=custom --no-owner --no-acl > "$backup_dir/app.dump.partial"
mv "$backup_dir/app.dump.partial" "$backup_dir/app.dump"
"$compose" exec -T db pg_restore --list < "$backup_dir/app.dump" > "$backup_dir/archive-list.txt"
"$compose" exec -T db pg_dumpall -U postgres --globals-only > "$backup_dir/globals.sql"
"$compose" exec -T redis redis-cli SAVE > /dev/null
"$compose" cp redis:/data/dump.rdb "$backup_dir/redis.rdb"
install -m 0600 /etc/tt-ada/production.env "$backup_dir/production.env"
printf '%s\n' "$release_sha" > "$backup_dir/release-sha.txt"
"$compose" exec -T db psql -U postgres -d app -Atc 'SELECT version_num FROM alembic_version' > "$backup_dir/alembic-head.txt"
docker image inspect "tt-ada:$release_sha" --format '{{.Id}}' > "$backup_dir/image-id.txt"
(cd "$backup_dir" && sha256sum app.dump globals.sql redis.rdb production.env > SHA256SUMS)
touch "$backup_dir/COMPLETE"
echo "Backup complete: $backup_dir"
