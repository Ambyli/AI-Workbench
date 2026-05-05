DC ?= docker compose -f docker-compose.yml
UP_FLAGS ?= -d --remove-orphans

up:
	$(DC) up $(UP_FLAGS)
down:
	$(DC) stop
clean:
	$(DC) down --volumes --remove-orphans
logs:
	$(DC) logs -f

.PHONY: run start stop down logs