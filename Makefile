# Convenience wrapper around the Docker commands. make is NOT required — every target is a one-line
# docker command you can run directly, which matters on Windows where make is unavailable outside WSL.

.PHONY: help base build up down logs test shell clean

CONTAINER ?= crawling-reviews-crawler-1

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

base: ## Build the base image (OS, Chrome, fonts) — slow, needed once
	docker compose --profile build build base

build: ## Build the application image
	docker compose build crawler

up: ## Start the service on http://localhost:8080
	docker compose up --build

down: ## Stop and remove containers
	docker compose down

logs: ## Follow logs
	docker compose logs -f crawler

test: ## Run the test suite (no browser or network needed)
	PYTHONPATH=src python -m pytest -q

shell: ## Shell inside the running container
	docker exec -it $(CONTAINER) bash

clean: ## Remove generated profile data
	rm -rf src/crawling_reviews/profiles/_data
