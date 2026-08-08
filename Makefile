.PHONY: stage-job-asset dev test check migrate up down logs bootstrap-owner genx-sync genx-reconcile agentgigs-sync agentgigs-webhook agentgigs-watch-once production-check backup smoke

dev:
	DJANGO_DB_ENGINE=sqlite DJANGO_DEBUG=1 python manage.py runserver

test:
	DJANGO_DB_ENGINE=sqlite python manage.py test

check:
	DJANGO_DB_ENGINE=sqlite python manage.py check

migrate:
	python manage.py migrate

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f --tail=200

bootstrap-owner:
	python manage.py bootstrap_owner

genx-sync:
	python manage.py sync_genx_catalog

genx-reconcile:
	python manage.py reconcile_genx

agentgigs-sync:
	python manage.py sync_agentgigs

agentgigs-webhook:
	python manage.py register_agentgigs_webhook

agentgigs-watch-once:
	python manage.py run_agentgigs_watcher --once

# Usage: make stage-job-asset JOB=<uuid> PATH=/var/lib/amarktai-earn/uploads/file
stage-job-asset:
	python manage.py stage_job_asset --job "$(JOB)" --path "$(PATH)"

production-check:
	docker compose exec -T web python manage.py production_check

backup:
	docker compose exec -T web /app/scripts/backup.sh

smoke:
	./scripts/smoke-test.sh
