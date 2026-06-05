DC = docker compose -f ai/docker-compose.yml --env-file .env
UP_FLAGS ?= -d --remove-orphans
SERVICES :=

define service
SERVICES += $(1)

DC_$(1) := docker compose -f ai/docker-compose.$(1).yml --env-file .env -p $(1)

up-$(1):
	$$(DC_$(1)) up -d $(2)

down-$(1):
	$$(DC_$(1)) stop $(2)

clean-$(1):
	$$(DC_$(1)) stop $(2) && $$(DC_$(1)) rm -f $(2)

very-clean-$(1):
	$$(DC_$(1)) down --volumes --rmi all $(2)

logs-$(1):
	$$(DC_$(1)) logs -f $(2)

build-$(1):
	$$(DC_$(1)) build $(2)

.PHONY: up-$(1) down-$(1) clean-$(1) very-clean-$(1) logs-$(1) build-$(1)
endef

$(eval $(call service,litellm,litellm))
$(eval $(call service,unsloth,unsloth))
$(eval $(call service,vllm,vllm-qwen vllm-llama))
$(eval $(call service,kokoro,kokoro-app kokoro-api))

setup: network
	cd widget && uv sync && cd ..
	cd widget && uv run claude_usage_widget.py &
	$(DC) up --build $(UP_FLAGS)

network:
	docker network create ai_shared 2>/dev/null || true

up:
	$(DC) up $(UP_FLAGS)

down:
	$(DC) stop

clean:
	$(DC) down --volumes --remove-orphans

very-clean:
	$(DC) down --volumes --remove-orphans --rmi all

logs:
	$(DC) logs -f

build:
	$(DC) build

help:
	@echo ""
	@echo "Main stack:"
	@echo "  make setup       Install deps and start all services"
	@echo "  make network     Create shared Docker network"
	@echo "  make up          Start"
	@echo "  make down        Stop"
	@echo "  make clean       Stop and remove containers + volumes"
	@echo "  make very-clean  Stop, remove containers, volumes, and images"
	@echo "  make logs        Follow logs"
	@echo "  make build       Rebuild images"
	@echo ""
	@echo "Service stacks:"
	@$(foreach s,$(SERVICES),echo "  $(s): up-$(s)  down-$(s)  clean-$(s)  very-clean-$(s)  logs-$(s)  build-$(s)";)
	@echo ""

.PHONY: setup up down clean very-clean logs build help
