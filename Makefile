# danger_zone — docker compose wrappers
#
# Usage:   make <target>     (try `make help` for the full list)
#
# All targets are thin wrappers around `docker compose` so you can still drop
# down to the raw command if you need something exotic.

COMPOSE      := docker compose -f docker/compose.yaml
COMPOSE_DEV  := docker compose -f docker/compose.yaml -f docker/compose.dev.yaml

.DEFAULT_GOAL := help

.PHONY: help build up up-dev down restart logs shell test rebuild ps clean lint format pytest

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?##.*$$' $(MAKEFILE_LIST) \
	  | sort \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

# ---- Docker -------------------------------------------------------------

build:        ## Build the production image (cached)
	$(COMPOSE) build

rebuild:      ## Force a clean rebuild (no cache)
	$(COMPOSE) build --no-cache

up:           ## Start the prod stack (detached)
	$(COMPOSE) up -d
	@echo "danger_zone -> http://localhost:8001"

up-dev:       ## Start the dev stack (foreground, live --reload)
	$(COMPOSE_DEV) up

down:         ## Stop and remove the stack (keeps volumes)
	$(COMPOSE) down

restart:      ## Restart the running container
	$(COMPOSE) restart

logs:         ## Tail container logs
	$(COMPOSE) logs -f --tail=100

shell:        ## Open a bash shell inside the running container
	$(COMPOSE) exec app /bin/bash

ps:           ## Show container status + healthcheck state
	$(COMPOSE) ps

test:         ## Run pytest inside a one-off container
	$(COMPOSE) run --rm app pytest -x

clean:        ## Stop the stack, remove the local image, drop dangling volumes
	$(COMPOSE) down --rmi local --volumes --remove-orphans

# ---- Host-side dev (no Docker) -------------------------------------------
# Quick wrappers so you don't have to remember the full uvicorn flag set.

pytest:       ## Run the test suite on the host (uv-managed venv)
	uv run pytest -x

lint:         ## Ruff lint
	uv run ruff check .

format:       ## Ruff format (in place)
	uv run ruff format .
