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

up-vllm:
	$(DC) up -d vllm

down-vllm:
	$(DC) stop vllm

clean-vllm:
	$(DC) stop vllm && $(DC) rm -f vllm

logs-vllm:
	$(DC) logs -f vllm

build-vllm:
	$(DC) build vllm

.PHONY: up up-litellm up-unsloth up-vllm down down-litellm down-unsloth down-vllm clean clean-litellm clean-unsloth clean-vllm logs logs-litellm logs-unsloth logs-vllm build build-litellm build-unsloth build-vllm
