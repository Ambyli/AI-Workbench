SERVICES :=

define service
SERVICES += $(1)
SERVICES_ARGS_$(1) := $(2)

DC_$(1) := docker compose -f ai/docker-compose.$(1).yml --env-file .env -p ai-$(1)

up-$(1):
	$$(DC_$(1)) up -d $(2)

down-$(1):
	$$(DC_$(1)) stop $(2)

clean-$(1):
	$$(DC_$(1)) stop $(2) && $$(DC_$(1)) rm -f $(2)

very-clean-$(1):
	@if [ "$(CONFIRM)" != "yes" ]; then \
		echo "WARNING: This will stop containers, remove all volumes and images for $(1). Type CONFIRM=yes to proceed."; \
		false; \
	fi
	$$(DC_$(1)) down --volumes --rmi all $(2)

logs-$(1):
	$$(DC_$(1)) logs -f $(2)

build-$(1):
	$$(DC_$(1)) build $(2)

.PHONY: up-$(1) down-$(1) clean-$(1) very-clean-$(1) logs-$(1) build-$(1)
endef

$(eval $(call service,litellm,litellm))
$(eval $(call service,unsloth,unsloth))
$(eval $(call service,vllm,vllm-qwen vllm-qwen-vl))
$(eval $(call service,kokoro,kokoro-app kokoro-api))
$(eval $(call service,classifier,classifier))
$(eval $(call service,openwebui,openwebui))
$(eval $(call service,cloudflared,cloudflared))

setup: network
	cd widget && uv sync && cd ..
	cd widget && uv run claude_usage_widget.py &
	$(foreach s,$(SERVICES),$(DC_$(s)) up --build -d &&) true

network:
	docker network create ai_shared 2>/dev/null || true

up: network
	$(foreach s,$(SERVICES),$(DC_$(s)) up -d &&) true

down:
	$(foreach s,$(SERVICES),$(DC_$(s)) stop;)

clean:
	$(foreach s,$(SERVICES),$(DC_$(s)) stop && $(DC_$(s)) rm -f;)

very-clean:
	@if [ "$(CONFIRM)" != "yes" ]; then \
		echo "WARNING: This will stop containers, remove all volumes and images. Type CONFIRM=yes to proceed."; \
		false; \
	fi
	$(foreach s,$(SERVICES),$(DC_$(s)) down --volumes --rmi all;)

build:
	$(foreach s,$(SERVICES),$(DC_$(s)) build &&) true

logs:
	@echo "Use logs-<service> to follow specific service logs."
	@echo "Services: $(SERVICES)"

help:
	@echo ""
	@echo "Main stack:"
	@echo "  make setup       Install deps and start all services"
	@echo "  make network     Create shared Docker network"
	@echo "  make up          Start all services"
	@echo "  make down        Stop all services"
	@echo "  make clean       Stop and remove containers"
	@echo "  make very-clean  Stop, remove containers, volumes, and images"
	@echo "  make build       Rebuild all images"
	@echo ""
	@echo "Service stacks:"
	@$(foreach s,$(SERVICES),echo "  $(s): up-$(s)  down-$(s)  clean-$(s)  very-clean-$(s)  logs-$(s)  build-$(s)";)
	@echo ""

.PHONY: setup up down clean very-clean build logs help network
