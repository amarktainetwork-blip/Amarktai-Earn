#!/usr/bin/env bash
set -euo pipefail
file="${1:?usage: restore.sh backup.dump.gpg}"
: "${POSTGRES_DB:?}" "${POSTGRES_USER:?}" "${POSTGRES_PASSWORD:?}" "${BACKUP_PASSPHRASE:?}"
export PGHOST="${POSTGRES_HOST:-postgres}"
export PGPORT="${POSTGRES_PORT:-5432}"
export PGPASSWORD="$POSTGRES_PASSWORD"

cleanup_gnupg=0
if [[ -z "${GNUPGHOME:-}" ]]; then
  export GNUPGHOME="$(mktemp -d /tmp/amarktai-gnupg.XXXXXX)"
  cleanup_gnupg=1
else
  mkdir -p "$GNUPGHOME"
fi
chmod 700 "$GNUPGHOME"
cleanup() {
  if [[ "$cleanup_gnupg" == "1" ]]; then
    rm -rf "$GNUPGHOME"
  fi
}
trap cleanup EXIT

test -s "$file"
gpg --batch --quiet --pinentry-mode loopback --decrypt --passphrase-fd 3 "$file" 3<<<"$BACKUP_PASSPHRASE" \
  | pg_restore --exit-on-error --clean --if-exists --no-owner --no-privileges \
      -U "$POSTGRES_USER" -d "$POSTGRES_DB"
