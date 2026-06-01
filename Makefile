DC = docker compose -f ai/docker-compose.yml
DC_LITE = docker compose -f ai/docker-compose.litellm.yml
DC_UNSLOTH = docker compose -f ai/docker-compose.unsloth.yml
DC_VLLM = docker compose -f ai/docker-compose.vllm.yml
UP_FLAGS ?= -d --remove-orphans

setup:
	cd widget && uv sync && cd ..
	cd widget && uv run claude_usage_widget.py &
	$(DC) up --build $(UP_FLAGS)

up:
	$(DC) up $(UP_FLAGS)

up-litellm:
	$(DC_LITE) up -d litellm

up-unsloth:
	$(DC_UNSLOTH) up -d unsloth

up-vllm:
	$(DC_VLLM) up -d vllm-qwen vllm-llama

down:
	$(DC) stop

down-litellm:
	$(DC_LITE) stop litellm

down-unsloth:
	$(DC_UNSLOTH) stop unsloth

down-vllm:
	$(DC_VLLM) stop vllm-qwen vllm-llama

clean:
	$(DC) down --volumes --remove-orphans

very-clean:
	$(DC) down --volumes --remove-orphans --rmi all

clean-litellm:
	$(DC_LITE) stop litellm && $(DC_LITE) rm -f litellm

clean-unsloth:
	$(DC_UNSLOTH) stop unsloth && $(DC_UNSLOTH) rm -f unsloth

clean-vllm:
	$(DC_VLLM) stop vllm-qwen vllm-llama && $(DC_VLLM) rm -f vllm-qwen vllm-llama

logs:
	$(DC) logs -f

logs-litellm:
	$(DC_LITE) logs -f litellm

logs-unsloth:
	$(DC_UNSLOTH) logs -f unsloth

logs-vllm:
	$(DC_VLLM) logs -f

build:
	$(DC) build

build-litellm:
	$(DC_LITE) build litellm

build-unsloth:
	$(DC_UNSLOTH) build unsloth

build-vllm:
	$(DC_VLLM) build

.PHONY: setup up up-litellm up-unsloth up-vllm down down-litellm down-unsloth down-vllm clean very-clean clean-litellm clean-unsloth clean-vllm logs logs-litellm logs-unsloth logs-vllm build build-litellm build-unsloth build-vllm
