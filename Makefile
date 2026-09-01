# Compose stack registry — each call registers a stack and its default service list.
STACKS :=

define service
STACKS += $(1)
STACK_$(1) := $(2)
DC_$(1) := docker compose -f ai/$(1)/docker-compose.$(1).yml --env-file .env -p ai-$(1)
endef

$(eval $(call service,litellm,litellm))
$(eval $(call service,unsloth,unsloth))
$(eval $(call service,vllm,qwen3.8 qwen3.6 vllm-qwen-vl))
$(eval $(call service,llama,glm5.2))
$(eval $(call service,kokoro,kokoro-app kokoro-api))
$(eval $(call service,madlad,madlad-app madlad-api))
$(eval $(call service,classifier,classifier))
$(eval $(call service,openwebui,openwebui))
$(eval $(call service,oauth2-proxy,oauth2-proxy oauth2-assets oauth2-proxy-n8n))
$(eval $(call service,cloudflared,cloudflared))
$(eval $(call service,roofix,roofix))
$(eval $(call service,interceptor,interceptor))
$(eval $(call service,searxng,searxng))
$(eval $(call service,sandbox,sandbox-db sandbox-egress sandbox-proxy sandbox-runner))
$(eval $(call service,n8n,n8n-db n8n))

# Introspection targets consumed by the `_make_ai_complete` bash completion
# function (installed once via `eval "$$(make completion-bash)"`). The
# completion shells out to `make -s list-stacks` for the second positional
# arg and `make -s list-services <stack>` for the third. Removing these
# breaks stack/service tab-completion while leaving verb completion working
# — that's the failure mode we hit before.
list-stacks:
	@echo $(STACKS)

list-services:
	@echo $(if $(STACK),$(STACK_$(STACK)))

# Comma constant — Make can't easily embed a literal comma inside a
# $(subst ...) call without one.
comma := ,

# Format the profiles to include on `build` as "--profile p1 --profile p2 …".
#
# Selection rules:
#   * PROFILES set on the command line (e.g. `make build sandbox
#     PROFILES=build`) — use those, comma-separated. Empty is treated as
#     "not set" (Make's $(if) evaluates empty as false).
#   * PROFILES unset — auto-discover every profile declared in the
#     stack's compose file via `docker compose config --profiles`
#     (Compose v2). New profile gates in any compose file are picked
#     up automatically; nothing here has to know which stacks use them.
#
# This lets `build` build profile-gated services (e.g. sandbox base-image
# builders under `profiles: [build]`) by default — without it, `POST
# /run` later fails with "No such image: sandbox-python:latest".
profile_flags = $(if $(PROFILES),$(foreach p,$(subst $(comma), ,$(PROFILES)),--profile $(p)),$(shell $(DC_$(1)) config --profiles 2>/dev/null | awk 'NF{printf "--profile %s ", $$0}'))

# Parse positional args: first goal is the verb, remaining goals are:
#   $(STACK) — compose stack name (optional; empty = all stacks)
#   $(SVC)   — service filter within that stack (optional; empty = stack defaults)
ARGS  := $(filter-out $(firstword $(MAKECMDGOALS)),$(MAKECMDGOALS))
STACK := $(firstword $(ARGS))
SVC   := $(wordlist 2,999,$(ARGS))

# Swallow each positional arg as a no-op target so Make doesn't try to build it.
$(foreach a,$(ARGS),$(eval $(a):;@:))

# Guard: error when STACK is set but doesn't match a registered stack.
# Empty STACK means "operate on all stacks" and skips the check.
check_stack = $(if $(STACK),$(if $(filter $(STACK),$(STACKS)),,$(error Unknown stack '$(STACK)'. Known stacks: $(STACKS))))

# Selected compose command + services (only meaningful when STACK is set).
DC       = $(DC_$(STACK))
SERVICES = $(if $(SVC),$(SVC),$(STACK_$(STACK)))

network:
	docker network create ai_shared 2>/dev/null || true

setup: network
	cd widget && uv sync && cd ..
	cd widget && uv run claude_usage_widget.py &
	$(foreach s,$(STACKS),$(DC_$(s)) up --build -d &&) true

up: network
	$(check_stack)
ifdef STACK
	$(DC) up -d $(SERVICES)
else
	$(foreach s,$(STACKS),$(DC_$(s)) up -d &&) true
endif

down:
	$(check_stack)
ifdef STACK
	$(DC) stop $(SERVICES)
else
	$(foreach s,$(STACKS),$(DC_$(s)) stop;)
endif

clean:
	$(check_stack)
ifdef STACK
	$(DC) stop $(SERVICES) && $(DC) rm -f $(SERVICES)
else
	$(foreach s,$(STACKS),$(DC_$(s)) stop && $(DC_$(s)) rm -f;)
endif

very-clean:
	$(check_stack)
	@if [ "$(CONFIRM)" != "yes" ]; then \
		echo "WARNING: This will stop containers, remove all volumes and images$(if $(STACK), for $(STACK),). Type CONFIRM=yes to proceed."; \
		false; \
	fi
ifdef STACK
	$(DC) down --volumes --rmi all
else
	$(foreach s,$(STACKS),$(DC_$(s)) down --volumes --rmi all;)
endif

build:
	$(check_stack)
ifdef STACK
	$(DC) pull --ignore-pull-failures $(SERVICES)
	$(DC) $(call profile_flags,$(STACK)) build $(if $(SVC),$(SVC))
else
	$(foreach s,$(STACKS),$(DC_$(s)) pull --ignore-pull-failures && $(DC_$(s)) $(call profile_flags,$(s)) build &&) true
endif

logs:
	$(check_stack)
ifdef STACK
	$(DC) logs -f $(SERVICES)
else
	@echo "Use: make logs <stack> [service...] to follow specific service logs."
	@echo "Stacks: $(STACKS)"
endif

help:
	@echo ""
	@echo "Usage: make <verb> [stack] [service...]"
	@echo ""
	@echo "Verbs:"
	@echo "  setup       Install deps and start all services"
	@echo "  network     Create shared Docker network"
	@echo "  up          Start services (all stacks, one stack, or specific services)"
	@echo "  down        Stop services"
	@echo "  clean       Stop and remove containers"
	@echo "  very-clean  Stop, remove containers, volumes, and images (needs CONFIRM=yes)"
	@echo "  build       Rebuild images"
	@echo "  logs        Follow service logs"
	@echo ""
	@echo "Stacks: $(STACKS)"
	@echo ""
	@echo "Examples:"
	@echo "  make up                                # start every stack"
	@echo "  make up vllm                           # start vllm stack (default services)"
	@echo "  make up vllm qwen3.6                   # start only qwen3.6 in vllm stack"
	@echo "  make build sandbox                     # build every service + every profile in the sandbox stack"
	@echo "  make build sandbox PROFILES=build      # build only services under the 'build' profile"
	@echo "  make build sandbox PROFILES=build,x    # build multiple profiles (comma-separated)"
	@echo "  make logs kokoro                       # tail kokoro logs"
	@echo ""
	@echo "Note: 'make build' auto-includes every profile declared in a stack's compose"
	@echo "file (see 'docker compose config --profiles'). Override with PROFILES=<list>"
	@echo "to narrow the set — the default of 'all profiles' works for most operators."
	@echo ""

.PHONY: setup network up down clean very-clean build logs help list-stacks list-services
