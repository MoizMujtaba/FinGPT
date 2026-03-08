.DEFAULT_GOAL := help
SHELL         := /bin/bash
PROJECT_ID    ?= $(shell gcloud config get-value project 2>/dev/null)
REGION        ?= us-central1
REGISTRY      ?= $(REGION)-docker.pkg.dev

.PHONY: help dev dev-down dev-build logs db-migrate test lint \
        docker-build docker-push deploy-all setup-gcp

# ── Help ──────────────────────────────────────────────────────────────────────

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ── Local dev (docker-compose) ────────────────────────────────────────────────

dev: ## Start all services locally
	docker compose up --build

dev-down: ## Stop all services
	docker compose down

dev-build: ## Rebuild all images without starting
	docker compose build

logs: ## Follow logs for all services
	docker compose logs -f

# ── Database ──────────────────────────────────────────────────────────────────

db-migrate: ## Apply SQL migrations (local postgres must be running)
	@for f in database/migrations/*.sql; do \
	  echo "Applying $$f..."; \
	  psql "$$DATABASE_URL" -f "$$f"; \
	done

# ── Testing ───────────────────────────────────────────────────────────────────

test: ## Run unit tests
	pytest tests/unit/ -v --tb=short

test-watch: ## Run tests in watch mode
	pytest-watch tests/unit/ -- -v

# ── Lint ──────────────────────────────────────────────────────────────────────

lint: ## Run ruff linter
	ruff check .

# ── Docker (GCP Artifact Registry) ───────────────────────────────────────────

SERVICES := ingress-api agent-ledger-api ops-console routing-engine kya-validator wallet-factory plaid-mcp

docker-build: ## Build all Docker images locally
	@for svc in $(SERVICES); do \
	  dockerfile=$$(echo $$svc | tr '-' '_'); \
	  echo "Building $$svc..."; \
	  docker build -t $(REGISTRY)/$(PROJECT_ID)/finogrid/$$svc:local -f services/$$dockerfile/Dockerfile . 2>/dev/null || \
	  docker build -t $(REGISTRY)/$(PROJECT_ID)/finogrid/$$svc:local -f mcp/$$dockerfile/Dockerfile . 2>/dev/null || true; \
	done

docker-push: docker-build ## Push images to Artifact Registry
	@for svc in $(SERVICES); do \
	  docker push $(REGISTRY)/$(PROJECT_ID)/finogrid/$$svc:local; \
	done

# ── GCP deployment ────────────────────────────────────────────────────────────

deploy-all: ## Trigger Cloud Build manually
	gcloud builds submit --config=cloudbuild.yaml \
	  --substitutions=_REGION=$(REGION),SHORT_SHA=$(shell git rev-parse --short HEAD)

# ── GCP project bootstrap (run once) ─────────────────────────────────────────

setup-gcp: ## Enable GCP APIs and create Artifact Registry repo
	@echo "Setting up GCP project: $(PROJECT_ID)"
	gcloud services enable \
	  cloudbuild.googleapis.com \
	  run.googleapis.com \
	  artifactregistry.googleapis.com \
	  secretmanager.googleapis.com \
	  sqladmin.googleapis.com \
	  --project=$(PROJECT_ID)
	gcloud artifacts repositories create finogrid \
	  --repository-format=docker \
	  --location=$(REGION) \
	  --project=$(PROJECT_ID) \
	  --description="Finogrid container images" || true
	gcloud auth configure-docker $(REGION)-docker.pkg.dev
	@echo "Done. Next steps:"
	@echo "  1. Create a Cloud SQL PostgreSQL 16 instance"
	@echo "  2. Store DATABASE_URL in Secret Manager: gcloud secrets create DATABASE_URL --data-file=-"
	@echo "  3. Connect your GitHub repo to Cloud Build triggers"
	@echo "  4. Run: make deploy-all"
