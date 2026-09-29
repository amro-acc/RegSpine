# RegAgentX — Makefile (CLAUDE.md §2's documented command interface, now real)
#
# Every Python recipe invokes the project .venv explicitly
# (.venv/Scripts/python.exe), never bare `python`/`uvicorn`/`pytest`. This is
# deliberate: running these commands under whatever interpreter happens to
# be first on PATH — instead of .venv — is exactly the bug that broke
# `make api` on first use (ModuleNotFoundError: No module named 'supabase',
# because the global Python install has none of requirements.txt on it).
# Targets no longer depend on the caller having activated the venv first.
#
# SHELL is pinned to cmd.exe (always present on Windows), not left to Make's
# auto-detection, which failed to find a usable sh.exe in this PowerShell
# session even though Git for Windows provides one elsewhere on PATH.
#
# Every non-`cd`-led recipe line below is prefixed with `cd .` (a harmless
# no-op). This isn't decorative: GNU Make 3.81 (the GnuWin32 Windows build,
# 2006-era, installed via `winget install GnuWin32.Make`) only invokes
# $(SHELL) for a recipe line if the line contains a shell metacharacter OR
# its first word is a recognized shell builtin (cd, echo, if, for, ...).
# `.venv/Scripts/python.exe -m uvicorn ...` matches neither test, so Make
# took a "fast path" and called Windows CreateProcess directly on the
# whole line as a single argv0 — which fails with "CreateProcess ...
# failed" / "e=2: The system cannot find the file specified" regardless of
# what SHELL is set to. Verified by testing SHELL=cmd.exe alone first: it
# still failed with the identical error, which is what proved this was a
# builtin-detection issue, not a shell-resolution issue. Leading `cd .`
# satisfies the builtin check and forces routing through $(SHELL).
#
# VENV_PY uses backslashes deliberately: cmd.exe only recognizes a leading
# `.\` / `\` / `X:\` as "this is a path, not a bare PATH-searched command
# name" when the separator is a backslash. `.venv/Scripts/python.exe` (forward
# slash) was tried first and failed with "'.venv' is not recognized as an
# internal or external command" — cmd.exe treated the whole forward-slash
# string as a literal command name to search PATH for, found nothing.

SHELL       := cmd.exe
.SHELLFLAGS := /C

VENV_PY := .venv\Scripts\python.exe

# Overridable: `API_PORT=9000 make api` (or export API_PORT in the shell/.env
# before invoking make -- any pre-existing environment variable of this name
# is picked up automatically by Make, `?=` only sets the default when unset).
# Not hardcoded because this project has already twice hit a dev port
# becoming permanently unusable on a specific machine -- port 8000 first,
# then port 8010 too (both silently swallowed every request without ever
# reaching the real backend process; the intercepting process's PID wasn't
# resolvable by Get-Process or Get-CimInstance either, consistent with
# corporate endpoint-security/proxy software on a managed laptop). Keep
# ui/web/.env's VITE_API_PORT in sync with whatever this actually is.
API_PORT ?= 8055

.PHONY: setup db index api ui run eval eval-fast test faults demo

setup:
	cd . && $(VENV_PY) -m pip install -r requirements.txt
	cd ui/web && npm install

db:
	cd . && $(VENV_PY) scripts/apply_schema.py

index:
	cd . && $(VENV_PY) scripts/seed_chroma.py

# --reload-dir scopes the reloader to src/config only. Without it, uvicorn
# watches the whole cwd recursively -- including .venv's tens of thousands of
# files. This repo lives inside a OneDrive-synced folder, and OneDrive's
# background sync touches file timestamps continuously even with no real
# content change; watchfiles reads that as "modified" and restarts uvicorn
# mid-import, producing a crash-loop of KeyboardInterrupt/import tracebacks.
api:
	cd . && $(VENV_PY) -m uvicorn src.api.main:app --reload --reload-dir src --reload-dir config --port $(API_PORT)

ui:
	cd ui/web && npm run dev

test:
	cd . && $(VENV_PY) -m pytest tests/ -v

# --- Not built this cycle (see docs/roadmap.md) ---
# CLAUDE.md §2 documents these as the intended interface, but no backing
# script/harness exists yet for any of them. Failing loudly here beats
# either omitting the target (silently breaking the documented interface)
# or a target that "succeeds" without doing anything.

run:
	@echo make run: not built yet -- no single-document pipeline CLI exists.
	@echo state_graph.py is invoked via src/api/main.py's POST /api/v1/audit instead.
	@exit 1

eval:
	@echo make eval: not built yet -- evals/golden and evals/scorers are both empty;
	@echo no golden sets have been labelled (spec.md section 8.3). See docs/roadmap.md.
	@exit 1

eval-fast:
	@echo make eval-fast: not built yet -- same blocker as 'make eval'.
	@exit 1

faults:
	@echo make faults: not built yet -- fault-injection harness is not implemented.
	@exit 1

demo:
	@echo make demo: not built yet -- no scripted demo-replay harness is implemented.
	@exit 1
