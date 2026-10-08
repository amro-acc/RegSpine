# RegSpine — Makefile
#
# Every recipe calls .venv's python directly instead of whatever happens
# to be first on PATH, so these targets work whether or not you've
# activated the venv in your shell.
#
# SHELL is pinned to cmd.exe (always available on Windows). Every
# non-`cd`-led recipe line below is prefixed with a harmless `cd .` —
# GNU Make's Windows build needs that to route the line through
# $(SHELL) instead of exec'ing it directly, which fails. Keep the
# prefix if you add or edit recipes here.
#
# VENV_PY uses backslashes on purpose — cmd.exe only treats a leading
# `.\` as "this is a path" (not a PATH-searched command name) when the
# separator is a backslash.

SHELL       := cmd.exe
.SHELLFLAGS := /C

VENV_PY := .venv\Scripts\python.exe

# Overridable: `API_PORT=9000 make api` (or set API_PORT in your shell
# or .env before running make). Keep ui/web/.env's VITE_API_PORT in
# sync with whatever this ends up being.
API_PORT ?= 8055

.PHONY: setup db index api ui run eval eval-fast test faults demo

setup:
	cd . && $(VENV_PY) -m pip install -r requirements.txt
	cd ui/web && npm install

db:
	cd . && $(VENV_PY) scripts/apply_schema.py

index:
	cd . && $(VENV_PY) scripts/seed_chroma.py

# --reload-dir scopes the reloader to src/config only. Without it,
# uvicorn watches the whole cwd recursively, including .venv — and on
# a OneDrive-synced checkout, background sync keeps touching file
# timestamps, which trips the reloader into a restart loop mid-import.
api:
	cd . && $(VENV_PY) -m uvicorn src.api.main:app --reload --reload-dir src --reload-dir config --port $(API_PORT)

ui:
	cd ui/web && npm run dev

test:
	cd . && $(VENV_PY) -m pytest tests/ -v

# Runs all 5 scorers against the golden sets (evals/golden/) and writes
# evals/results.json + evals/BENCHMARK_REPORT.md. Every scorer call goes
# through the same LLM cache everything else does, so a second run
# replays instantly instead of re-hitting live providers.
eval:
	cd . && $(VENV_PY) evals/run_benchmarks.py

# --- Not built yet ---
# Kept as real targets so the command interface stays documented and
# consistent, even though nothing backs them yet. Failing loudly beats
# quietly omitting the target or having it "succeed" without doing
# anything.

run:
	@echo make run: not built yet -- no single-document pipeline CLI exists.
	@echo state_graph.py is invoked via src/api/main.py's POST /api/v1/audit instead.
	@exit 1

eval-fast:
	@echo make eval-fast: not built yet -- no cut-down/sampled variant of 'make eval' exists.
	@exit 1

faults:
	@echo make faults: not built yet -- fault-injection harness is not implemented.
	@exit 1

demo:
	@echo make demo: not built yet -- no scripted demo-replay harness is implemented.
	@exit 1
