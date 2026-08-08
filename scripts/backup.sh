#!/usr/bin/env bash
set -euo pipefail
: "${POSTGRES_DB:?}" "${POSTGRES_USER:?}" "${BACKUP_PASSPHRASE:?}"
out="/var/lib/amarktai-earn/backups/amarktai-earn-$(date -u +%Y%m%dT%H%M%SZ).sql.gz.gpg"
pg_dump -Fc -U "$POSTGRES_USER" "$POSTGRES_DB" | gzip | gpg --batch --yes --symmetric --cipher-algo AES256 --passphrase "$BACKUP_PASSPHRASE" -o "$out"
find /var/lib/amarktai-earn/backups -type f -name '*.gpg' -mtime +14 -delete
printf '%s\n' "$out"
