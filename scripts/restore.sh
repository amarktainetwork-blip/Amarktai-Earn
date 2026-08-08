#!/usr/bin/env bash
set -euo pipefail
file="${1:?usage: restore.sh backup.sql.gz.gpg}"
: "${POSTGRES_DB:?}" "${POSTGRES_USER:?}" "${BACKUP_PASSPHRASE:?}"
gpg --batch --quiet --decrypt --passphrase "$BACKUP_PASSPHRASE" "$file" | gunzip | pg_restore --clean --if-exists -U "$POSTGRES_USER" -d "$POSTGRES_DB"
