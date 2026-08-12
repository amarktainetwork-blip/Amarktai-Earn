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
verify_tmp="$(mktemp /tmp/amarktai-backup-verify.XXXXXX.dump)"
mkdir -p "$backup_dir"
cleanup() {
  rm -f "$tmp" "$verify_tmp"
  if [[ "$cleanup_gnupg" == "1" ]]; then
    rm -rf "$GNUPGHOME"
  fi
}
trap cleanup EXIT

pg_dump --format=custom --no-owner --no-privileges -U "$POSTGRES_USER" "$POSTGRES_DB" \
  | gpg --batch --yes --pinentry-mode loopback --symmetric --cipher-algo AES256 \
      --passphrase-fd 3 --output "$tmp" 3<<<"$BACKUP_PASSPHRASE"

# Fully decrypt and parse the temporary encrypted artifact before publishing it.
# Do not stream decrypted bytes directly into pg_restore: pg_restore may close
# the pipe before gpg has finished writing, which turns a valid archive into a
# false-negative Broken pipe under set -o pipefail.
gpg --batch --yes --quiet --pinentry-mode loopback --decrypt --passphrase-fd 3 \
  --output "$verify_tmp" "$tmp" 3<<<"$BACKUP_PASSPHRASE"
pg_restore --list "$verify_tmp" >/dev/null

# Only a verified archive is ever promoted to the durable final filename.
mv "$tmp" "$out"
find "$backup_dir" -type f -name '*.dump.gpg' -mtime +14 -delete
printf '%s\n' "$out"
