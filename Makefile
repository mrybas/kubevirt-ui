.PHONY: dev build test lint clean help

# Variables
DOCKER_COMPOSE = docker compose
DOCKER = docker
IMAGE_REGISTRY ?= ghcr.io/mrybas/kubevirt-ui
IMAGE_TAG ?= latest

# Colors
GREEN := \033[0;32m
NC := \033[0m

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "$(GREEN)%-20s$(NC) %s\n", $$1, $$2}'

# =============================================================================
# Development
# =============================================================================

dev: ## Start development environment
	$(DOCKER_COMPOSE) up --build

dev-backend: ## Start only backend in dev mode
	$(DOCKER_COMPOSE) up --build backend

dev-frontend: ## Start only frontend in dev mode
	$(DOCKER_COMPOSE) up --build frontend

logs: ## Show logs from all services
	$(DOCKER_COMPOSE) logs -f

stop: ## Stop development environment
	$(DOCKER_COMPOSE) down

restart: stop dev ## Restart development environment

# =============================================================================
# Build
# =============================================================================

build: build-backend build-frontend ## Build all Docker images

build-backend: ## Build backend Docker image
	$(DOCKER) build -t $(IMAGE_REGISTRY)/backend:$(IMAGE_TAG) ./backend

build-frontend: ## Build frontend Docker image
	$(DOCKER) build -t $(IMAGE_REGISTRY)/frontend:$(IMAGE_TAG) ./frontend

push: push-backend push-frontend ## Push all Docker images

push-backend: ## Push backend Docker image
	$(DOCKER) push $(IMAGE_REGISTRY)/backend:$(IMAGE_TAG)

push-frontend: ## Push frontend Docker image
	$(DOCKER) push $(IMAGE_REGISTRY)/frontend:$(IMAGE_TAG)

# =============================================================================
# Testing
# =============================================================================

test: test-backend test-frontend ## Run all tests

test-backend: ## Run backend tests
	$(DOCKER_COMPOSE) run --rm backend pytest -v

test-frontend: ## Run frontend tests
	$(DOCKER_COMPOSE) run --rm frontend npm test

test-e2e: ## Run end-to-end tests
	$(DOCKER_COMPOSE) -f docker-compose.yml -f docker-compose.e2e.yml up --abort-on-container-exit --exit-code-from e2e

# =============================================================================
# Linting
# =============================================================================

lint: lint-backend lint-frontend ## Run all linters

lint-backend: ## Lint backend code
	$(DOCKER_COMPOSE) run --rm backend sh -c "ruff check . && ruff format --check ."

lint-frontend: ## Lint frontend code
	$(DOCKER_COMPOSE) run --rm frontend npm run lint

format: format-backend format-frontend ## Format all code

format-backend: ## Format backend code
	$(DOCKER_COMPOSE) run --rm backend sh -c "ruff format ."

format-frontend: ## Format frontend code
	$(DOCKER_COMPOSE) run --rm frontend npm run format

# =============================================================================
# Helm
# =============================================================================

helm-lint: ## Lint Helm chart
	helm lint helm/kubevirt-ui

helm-template: ## Template Helm chart
	helm template kubevirt-ui helm/kubevirt-ui

helm-package: ## Package Helm chart
	helm package helm/kubevirt-ui -d dist/

helm-push: helm-package ## Push Helm chart to OCI registry
	helm push dist/kubevirt-ui-*.tgz oci://$(IMAGE_REGISTRY)/charts

# =============================================================================
# Cleanup
# =============================================================================

clean: ## Clean all build artifacts
	$(DOCKER_COMPOSE) down -v --rmi local
	rm -rf dist/
	rm -rf backend/__pycache__
	rm -rf backend/.pytest_cache
	rm -rf frontend/node_modules
	rm -rf frontend/dist

clean-docker: ## Remove all related Docker images
	$(DOCKER) rmi $(IMAGE_REGISTRY)/backend:$(IMAGE_TAG) || true
	$(DOCKER) rmi $(IMAGE_REGISTRY)/frontend:$(IMAGE_TAG) || true

# =============================================================================
# Utilities
# =============================================================================

shell-backend: ## Open shell in backend container
	$(DOCKER_COMPOSE) run --rm backend /bin/sh

shell-frontend: ## Open shell in frontend container
	$(DOCKER_COMPOSE) run --rm frontend /bin/sh

deps-update-backend: ## Update backend dependencies
	$(DOCKER_COMPOSE) run --rm backend pip-compile requirements.in -o requirements.txt

deps-update-frontend: ## Update frontend dependencies
	$(DOCKER_COMPOSE) run --rm frontend npm update
