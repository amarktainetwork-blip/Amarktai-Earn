#!/usr/bin/env bash
set -euo pipefail
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

backup_dir="/var/lib/amarktai-earn/backups"
out="$backup_dir/amarktai-earn-$(date -u +%Y%m%dT%H%M%SZ).dump.gpg"
tmp="$out.part"
mkdir -p "$backup_dir"
cleanup() {
  rm -f "$tmp"
  if [[ "$cleanup_gnupg" == "1" ]]; then
    rm -rf "$GNUPGHOME"
  fi
}
trap cleanup EXIT

pg_dump --format=custom --no-owner --no-privileges -U "$POSTGRES_USER" "$POSTGRES_DB" \
  | gpg --batch --yes --pinentry-mode loopback --symmetric --cipher-algo AES256 \
      --passphrase-fd 3 --output "$tmp" 3<<<"$BACKUP_PASSPHRASE"
mv "$tmp" "$out"

# Never retain a backup we cannot decrypt and parse as a PostgreSQL archive.
gpg --batch --quiet --pinentry-mode loopback --decrypt --passphrase-fd 3 "$out" 3<<<"$BACKUP_PASSPHRASE" \
  | pg_restore --list >/dev/null
find "$backup_dir" -type f -name '*.dump.gpg' -mtime +14 -delete
printf '%s\n' "$out"
