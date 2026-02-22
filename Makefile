# Tradecraft — Developer Commands
.PHONY: help dev build preview lint install test-pipeline clean

FRONTEND_DIR := frontend
VITE_GH_OWNER ?= your-github-username
VITE_GH_REPO  ?= Tradecraft

help:
	@echo ""
	@echo "  Tradecraft — Available Commands"
	@echo "  ─────────────────────────────────"
	@echo "  make install        Install frontend dependencies"
	@echo "  make dev            Start Vite dev server (localhost:5173)"
	@echo "  make build          Production build → frontend/dist/"
	@echo "  make preview        Preview production build locally"
	@echo "  make lint           Run ESLint on frontend/src/"
	@echo "  make test-pipeline  Run pipeline locally (mock mode)"
	@echo "  make clean          Remove build artifacts"
	@echo ""

install:
	cd $(FRONTEND_DIR) && npm ci

dev:
	cd $(FRONTEND_DIR) && VITE_GH_OWNER=$(VITE_GH_OWNER) VITE_GH_REPO=$(VITE_GH_REPO) npm run dev

build:
	cd $(FRONTEND_DIR) && \
	  VITE_GH_OWNER=$(VITE_GH_OWNER) \
	  VITE_GH_REPO=$(VITE_GH_REPO)  \
	  npm run build

preview:
	cd $(FRONTEND_DIR) && npm run preview

lint:
	cd $(FRONTEND_DIR) && npm run lint

test-pipeline:
	@echo "Running pipeline in mock mode (no GitHub token required)..."
	python3 lob/run_lob_simulation.py --event 0 --provider mock

clean:
	rm -rf $(FRONTEND_DIR)/dist $(FRONTEND_DIR)/node_modules
