.PHONY: dev test check migrate up down logs bootstrap-owner

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
