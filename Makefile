up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f

psql:
	docker compose exec postgres psql -U postgres

redis-cli:
	docker compose exec redis redis-cli

sh-api:
	docker compose exec api bash

migrate:
	docker compose exec api alembic upgrade head

rebuild:
	docker compose down -v --remove-orphans
	docker compose build --no-cache
	docker compose up -d
