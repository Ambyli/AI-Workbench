DC ?= docker compose
UP_FLAGS ?= -d --remove-orphans

up:
	$(DC) up $(UP_FLAGS)

up-litellm:
	$(DC) up -d litellm

up-unsloth:
	$(DC) up -d unsloth

down:
	$(DC) stop

down-litellm:
	$(DC) stop litellm

down-unsloth:
	$(DC) stop unsloth

clean:
	$(DC) down --volumes --remove-orphans

clean-litellm:
	$(DC) stop litellm && $(DC) rm -f litellm

clean-unsloth:
	$(DC) stop unsloth && $(DC) rm -f unsloth

logs:
	$(DC) logs -f

logs-litellm:
	$(DC) logs -f litellm

logs-unsloth:
	$(DC) logs -f unsloth

build:
	$(DC) build

build-litellm:
	$(DC) build litellm

build-unsloth:
	$(DC) build unsloth

.PHONY: up up-litellm up-unsloth down down-litellm down-unsloth clean clean-litellm clean-unsloth logs logs-litellm logs-unsloth build build-litellm build-unsloth
