#!/usr/bin/env sh
set -eu
for dir in \
  /var/lib/amarktai-earn/artifacts \
  /var/lib/amarktai-earn/jobs \
  /var/lib/amarktai-earn/repos \
  /var/lib/amarktai-earn/cache \
  /var/lib/amarktai-earn/backups \
  /var/lib/amarktai-earn/logs \
  /var/lib/amarktai-earn/uploads
do
  mkdir -p "$dir"
  chown amarktai:amarktai "$dir"
  chmod 0750 "$dir"
done
