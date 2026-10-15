# Python for scripts/deploy.py (stdlib only, 3.10+). Override with
# `make up PYTHON=python3.12 ...` if the box's default is older.
PYTHON ?= python3

# Compose stack registry — each call registers a stack and its default service list.
# DC_<stack> is the raw compose command; DEPLOY_<stack> is scripts/deploy.py
# pointed at the same compose file, env file and project — what `make up` runs.
STACKS :=

define service
STACKS += $(1)
STACK_$(1) := $(2)
DC_$(1) := docker compose -f ai/$(1)/docker-compose.$(1).yml --env-file .env -p ai-$(1)
DEPLOY_$(1) := $(PYTHON) scripts/deploy.py -f ai/$(1)/docker-compose.$(1).yml --env-file .env -p ai-$(1)
endef

$(eval $(call service,litellm,litellm prometheus))
$(eval $(call service,unsloth,unsloth))
$(eval $(call service,vllm,qwen3.8 qwen3.8-solo qwen3.6 vllm-qwen-vl muse-glimmer))
$(eval $(call service,llama,glm5.2 qwen3.8-flash glm5.3-flash))
$(eval $(call service,kokoro,kokoro-app kokoro-api))
$(eval $(call service,madlad,madlad-app madlad-api))
$(eval $(call service,classifier,classifier-db classifier))
$(eval $(call service,detector,detector))
$(eval $(call service,openwebui,openwebui))
$(eval $(call service,oauth2-proxy,oauth2-proxy oauth2-assets))
$(eval $(call service,cloudflared,cloudflared))
$(eval $(call service,roofix,roofix))
$(eval $(call service,interceptor,interceptor))
$(eval $(call service,searxng,searxng))
$(eval $(call service,sandbox,sandbox-db sandbox-egress sandbox-proxy sandbox-runner))
$(eval $(call service,n8n,n8n-db n8n))
$(eval $(call service,open-terminal,open-terminal))
$(eval $(call service,trino,hive-metastore-db hive-metastore lake-s3 lake-s3-init trino-auth-init trino-coordinator trino-mcp superset-db superset))
$(eval $(call service,semantic-router,vllm-sr-models-init vllm-sr-router vllm-sr-envoy))
$(eval $(call service,supabase,supabase-db supavisor auth rest realtime imgproxy storage meta studio supabase-api functions))
# Last on purpose: it binds the host ports the swap-class services above used
# to publish, so on a full `make up` they must have released them first.
# See ai/edge/EDGE.md § One-time cutover. edge-certs (its TLS certificate,
# EDGE.md § TLS) comes first: the edge waits for it to be healthy.
$(eval $(call service,edge,edge-certs edge))

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
# $(sort) dedupes, so `make up litellm litellm` doesn't define the rule twice.
$(foreach a,$(sort $(ARGS)),$(eval $(a):;@:))

# Guard: error when STACK is set but doesn't match a registered stack.
# Empty STACK means "operate on all stacks" and skips the check.
check_stack = $(if $(STACK),$(if $(filter $(STACK),$(STACKS)),,$(error Unknown stack '$(STACK)'. Known stacks: $(STACKS))))

# Selected compose command + services (only meaningful when STACK is set).
DC       = $(DC_$(STACK))
SERVICES = $(if $(SVC),$(SVC),$(STACK_$(STACK)))

# Lines of history `logs` prints per service before following. Override with
# `make logs <stack> TAIL=2000`, or `TAIL=all` for the full history.
TAIL ?= 500
# FOLLOW=0 prints the history and exits instead of following, so `make logs`
# can feed a pipe: `make -s logs litellm FOLLOW=0 TAIL=all | grep overflow`.
FOLLOW ?= 1

# `make up` switches. Only the literal value 1 turns one on, so a stray
# DRY_RUN=true in the environment (roofix's .env setting, if exported) can't.
#   RECREATE=1  plain `docker compose up -d` (the pre-deploy.py behaviour)
#   DRY_RUN=1   print the deploy plan, change nothing
#   FORCE=1     swap swap-class services even when they are up to date
DEPLOY_FLAGS = $(if $(filter 1,$(DRY_RUN)),--dry-run) $(if $(filter 1,$(FORCE)),--force)

network:
	docker network create ai_shared 2>/dev/null || true
	# terminal_net is deliberately NOT ai_shared: it joins only open-terminal
	# and openwebui, keeping the model-driven shell away from litellm,
	# roofix-db, sandbox-runner (docker.sock), lake-s3 and n8n. Plain bridge,
	# not internal — open-terminal needs a gateway for pip/apt, narrowed by
	# OPEN_TERMINAL_ALLOWED_DOMAINS. See ai/open-terminal/OPEN_TERMINAL.md.
	docker network create terminal_net 2>/dev/null || true

setup: network
	cd widget && uv sync && cd ..
	cd widget && uv run claude_usage_widget.py &
	$(foreach s,$(STACKS),$(DC_$(s)) up --build -d &&) true

# Zero-downtime deploy: scripts/deploy.py swaps services labelled
# com.zeo.deploy=swap side by side and recreates the rest exactly as
# `docker compose up -d` did, printing why. See ai/edge/EDGE.md.
up: $(if $(filter 1,$(DRY_RUN)),,network)
	$(check_stack)
ifeq ($(RECREATE),1)
ifdef STACK
	$(DC) up -d $(SERVICES)
else
	$(foreach s,$(STACKS),$(DC_$(s)) up -d &&) true
endif
else
ifdef STACK
	$(DEPLOY_$(STACK)) $(DEPLOY_FLAGS) $(SERVICES)
else
	$(foreach s,$(STACKS),$(DEPLOY_$(s)) $(DEPLOY_FLAGS) &&) true
endif
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
	$(DC) logs $(if $(filter 0,$(FOLLOW)),,-f) --tail $(TAIL) $(SERVICES)
else
	@echo "Use: make logs <stack> [service...] to follow specific service logs."
	@echo "Stacks: $(STACKS)"
endif

ps:
	$(check_stack)
ifdef STACK
	$(DC) ps $(SERVICES)
else
	@echo "Use: make ps <stack> [service...] to list active containers for a stack."
	@echo "Stacks: $(STACKS)"
endif

# Run a command in a service's running container, found by its compose labels
# — swap-class containers are named <service>-<hash>, so `docker exec <name>`
# no longer has a fixed name to use. CMD defaults to an interactive shell; a
# TTY is requested only when stdin is one, so it also works in pipes/scripts.
#   make exec litellm litellm CMD='curl -s http://roofix:8080/status'
exec:
	$(check_stack)
	@if [ -z "$(STACK)" ] || [ -z "$(SVC)" ]; then \
		echo "Usage: make exec <stack> <service> [CMD='command ...']"; exit 2; \
	fi
	@cid=$$(docker ps -q --filter label=com.docker.compose.project=ai-$(STACK) \
		--filter label=com.docker.compose.service=$(firstword $(SVC)) | head -1); \
	if [ -z "$$cid" ]; then \
		echo "No running container for service '$(firstword $(SVC))' in stack '$(STACK)'."; exit 1; \
	fi; \
	if [ -t 0 ]; then tty=-it; else tty=-i; fi; \
	docker exec $$tty $$cid $(or $(CMD),sh)

help:
	@echo ""
	@echo "Usage: make <verb> [stack] [service...]"
	@echo ""
	@echo "Verbs:"
	@echo "  setup       Install deps and start all services"
	@echo "  network     Create shared Docker network"
	@echo "  up          Deploy services (all stacks, one stack, or specific services):"
	@echo "              swap-class services are replaced side by side with no downtime,"
	@echo "              the rest are recreated as before (each line says which and why)"
	@echo "  down        Stop services"
	@echo "  clean       Stop and remove containers"
	@echo "  very-clean  Stop, remove containers, volumes, and images (needs CONFIRM=yes)"
	@echo "  build       Rebuild images"
	@echo "  logs        Follow service logs (last 500 lines; TAIL=n or TAIL=all; FOLLOW=0 to exit)"
	@echo "  ps          List active containers for a stack"
	@echo "  exec        Run CMD in a service's running container (default: sh)"
	@echo ""
	@echo "Switches for up:"
	@echo "  RECREATE=1  plain 'docker compose up -d' (no side-by-side swap)"
	@echo "  DRY_RUN=1   print the deploy plan without changing anything"
	@echo "  FORCE=1     swap even when a swap-class service is up to date"
	@echo ""
	@echo "Stacks: $(STACKS)"
	@echo ""
	@echo "Examples:"
	@echo "  make up                                # start every stack"
	@echo "  make up vllm                           # start vllm stack (default services)"
	@echo "  make up vllm qwen3.6                   # start only qwen3.6 in vllm stack"
	@echo "  make up litellm DRY_RUN=1              # show what a litellm deploy would do"
	@echo "  make up litellm FORCE=1                # swap litellm even if unchanged"
	@echo "  make up litellm RECREATE=1             # old behaviour: plain compose up -d"
	@echo "  make build sandbox                     # build every service + every profile in the sandbox stack"
	@echo "  make build sandbox PROFILES=build      # build only services under the 'build' profile"
	@echo "  make build sandbox PROFILES=build,x    # build multiple profiles (comma-separated)"
	@echo "  make logs kokoro                       # tail kokoro logs (last 500 lines, then follow)"
	@echo "  make logs kokoro TAIL=all              # full history, then follow"
	@echo "  make -s logs litellm FOLLOW=0 TAIL=all | grep overflow   # history into a pipe"
	@echo "  make ps sandbox                        # list active sandbox containers"
	@echo "  make exec litellm litellm CMD='env'    # run a command in the litellm container"
	@echo "  make exec supabase auth                # shell in the running auth container"
	@echo ""
	@echo "Note: 'make build' auto-includes every profile declared in a stack's compose"
	@echo "file (see 'docker compose config --profiles'). Override with PROFILES=<list>"
	@echo "to narrow the set — the default of 'all profiles' works for most operators."
	@echo ""

.PHONY: setup network up down clean very-clean build logs ps exec help list-stacks list-services
