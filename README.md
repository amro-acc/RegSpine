# RegSpine — Run Guide

How to get RegSpine running from scratch on a new machine: backend (FastAPI +
LangGraph), frontend (React/Vite), database (Supabase/Postgres), and local
components (ChromaDB, SQLite cache). This is a hackathon submission with a
declared feature freeze — see [§13](#13-not-implemented-yet) for what's
intentionally not built.

Two credentials are **shared team secrets**, not something you provision
yourself — get these from a teammate before starting (§2). Everything else
you can set up independently (§3 onward).

---

## Tech stack

| Layer | Technology |
| --- | --- |
| Backend | FastAPI (Python), Pydantic v2 |
| Agent orchestration | LangGraph |
| LLMs | GPT-5.1 (extractor/reasoner, via Azure AI Foundry) · GPT-5.6-Luna (reasoner fallback) · Gemini-3.8-flash (independent judge, Google AI Studio) |
| Retrieval | ChromaDB (local, embedded) + `sentence-transformers/all-MiniLM-L6-v2` embeddings + `bge-reranker-base` cross-encoder reranking |
| Database | Supabase (hosted Postgres) |
| Local cache | SQLite (`cache.db`) — LLM response cache + LangGraph checkpointer |
| Frontend | React + TypeScript + Vite, Tailwind CSS, React Flow (`@xyflow/react`) for the lineage graph |

See [`techstack.md`](techstack.md) for the rationale behind each choice, cost/quota tradeoffs, and the operational cost model.

---

## 1. Prerequisites

| Tool | Version | Notes |
| --- | --- | --- |
| Python | 3.11+ | |
| Node.js | 20 LTS+ (recommended) | Not pinned anywhere in the repo (no `engines` field in `ui/web/package.json`) — 20+ is a safe floor given Vite 8 / React 19 / TypeScript ~6.0 |
| npm | ships with Node | — |
| Git | any recent version | — |
| A Postgres instance | — | The team uses a shared Supabase project (§2); a local/Docker Postgres also works if you point `DATABASE_URL` at it instead |

**`make`, Windows note:** the `Makefile` currently pins `SHELL := cmd.exe`, so
it's Windows-oriented as written and won't run as-is on macOS/Linux (no
`cmd.exe` there) — use the raw commands given alongside each `make` target
below if you're not on Windows. On Windows, install Make via:

```powershell
winget install GnuWin32.Make
```

Then **close and reopen your terminal** (PATH isn't picked up by
already-open shells). If `make` still isn't found, add it manually for the
session:

```powershell
$env:Path += ";C:\Program Files (x86)\GnuWin32\bin"
```

---

## 2. Get the shared credentials

Ask a teammate (or whoever owns the Supabase project / Foundry resource) for:

| Env var | What it is |
| --- | --- |
| `SUPABASE_URL` | Shared Supabase project REST URL |
| `SUPABASE_SERVICE_KEY` | Server-side only — **never** load this in the frontend/UI process |
| `DATABASE_URL` | Direct Postgres connection string (Supabase pooler) — used only by `make db` to apply migrations; the Supabase client above is REST-only and doesn't run migrations |
| `OPENAI_API_KEY` | A **Microsoft Foundry / Azure OpenAI** key, not a personal `sk-...` platform key |
| `AZURE_OPENAI_BASE_URL` | Pairs with the Foundry key above — e.g. `https://<resource>.services.ai.azure.com/openai/v1/`. Required whenever `OPENAI_API_KEY` is a Foundry key (a Foundry key against the plain OpenAI API fails auth outright) |

Do not commit these anywhere. They go in your local `.env` only (§4).

---

## 3. Self-serve credentials

| Env var | How to get it |
| --- | --- |
| `GOOGLE_API_KEY` | Free at [Google AI Studio](https://ai.google.dev) — used only for the `JUDGE` role (`judge_pool`, currently `gemini-3.8-flash`), which is low-volume, so the free tier is sufficient |

Everything else (`SQLITE_CACHE_PATH`, `REGSPINE_ENV`, `REGSPINE_CACHE`,
`REGSPINE_RUN_BUDGET_USD`) is a local runtime flag with a sane default — no
account needed.

---

## 4. Clone and configure the environment

```bash
git clone <repo-url> RegSpine
cd RegSpine
cp .env.example .env
```

Fill in `.env` with the credentials from §2 and §3. The full variable list
(with purpose) is documented inline in `.env.example` — that's the
authoritative source.

---

## 5. Backend: Python environment + dependencies

**Windows:**
```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

**macOS/Linux:**
```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Or, on Windows with Make set up: `make setup` (this also runs `npm install`
for the frontend in the same step — §9 covers the frontend on its own if you
want to do it separately).

Key backend dependencies installed: `fastapi`, `uvicorn`, `langgraph` +
`langgraph-checkpoint-sqlite`, `langchain-openai` (OpenAI calls, via its
`ChatOpenAI`), `google-genai` (Gemini calls — the raw SDK, not
`langchain-google-genai`; that wrapper rejects Google's newer key format, see
`src/llm/gemini_client.py`), `pydantic>=2.7`, `chromadb`,
`sentence-transformers`, `FlagEmbedding` (reranker), `supabase`,
`psycopg2-binary` (direct Postgres, needed for migrations),
`PyMuPDF`/`pdfplumber`/`openpyxl` (document parsing), `pyyaml`,
`python-dotenv`.

---

## 6. Database: apply migrations, seed baseline data

Migrations live in `db/migrations/*.sql` (forward-only, numbered:
`001_init.sql`, `002_add_compliance_status.sql`, etc.), tracked in a
`schema_migrations` table the script creates itself — safe to re-run, only
unapplied files execute.

```powershell
make db
# or directly:
.venv\Scripts\python.exe scripts/apply_schema.py
```

Then seed the bank entities the UI's dropdown and the audit endpoint depend
on (idempotent — safe to re-run, skips rows that already exist by name):

```powershell
.venv\Scripts\python.exe scripts/seed_supabase.py
```

This creates three bank profiles: "Meridian Bank USA", "Meridian Bank Europe
SE", and "Meridian Bank India". If you're pointing at a **fresh** Postgres instance (not the shared
Supabase project, which already has this applied), also run the one-off
licence correction so DORA obligations aren't filtered out for the EU entity:

```powershell
.venv\Scripts\python.exe scripts/update_bank_entity_licences.py
```

---

## 7. Local vector index: seed Chroma from the corpus

The repo ships two small synthetic documents (`corpus/regulations/`,
`corpus/bank/`) — the real Basel III/DORA/PCI DSS corpus hasn't been added
yet. `chroma_db/` is gitignored and rebuilt deterministically:

```powershell
make index
# or directly:
.venv\Scripts\python.exe scripts/seed_chroma.py
```

This also rewrites `corpus/manifest.json` (sha256 hash per document) — takes
roughly a couple of minutes the first time while the embedding/reranker
models download and load.

---

## 8. Start the backend API

```powershell
make api
# or directly:
.venv\Scripts\python.exe -m uvicorn src.api.main:app --reload --reload-dir src --reload-dir config --port 8055
```

Default port is **8055** (overridable: `API_PORT=9000 make api`, or set
`API_PORT` in your shell before invoking Make). If you change it, also update
`ui/web/.env`'s `VITE_API_PORT` to match (§9) — the frontend reads its API
base URL from there.

On startup, the server warms the embedding model and reranker before it
accepts requests (you'll see "Warming embedding model and reranker..." in
the terminal) — a few minutes the first time while they download, seconds on
later starts. This moves that cost to startup instead of onto whoever
triggers the first real audit.

Once running, check `http://localhost:8055/docs` for interactive API docs
(FastAPI's default Swagger UI — there's no separate `/health` route).

---

## 9. Frontend: install and start the React SPA

```bash
cd ui/web
npm install
npm run dev
# or: make ui   (from repo root, does the same cd + npm run dev)
```

Opens on `http://localhost:5173` (Vite's default) — **this port matters**:
the backend's `CORSMiddleware` only allows `http://localhost:5173` as an
origin, so don't change Vite's port without also updating
`src/api/main.py`'s CORS config.

`ui/web/.env` (tracked, non-secret — just the shared port default) should
already contain:
```
VITE_API_PORT=8055
```
Override locally via `ui/web/.env.local` (gitignored) if 8055 is unavailable
on your machine.

---

## 10. Verify everything works

1. `http://localhost:8055/docs` loads FastAPI's Swagger UI.
2. `http://localhost:5173` loads the React SPA and the bank-profile dropdown
   is populated (confirms `GET /api/v1/bank_entities` + Supabase connectivity).
3. Run a compliance audit from the UI end-to-end (confirms LLM provider keys,
   Chroma index, and the full LangGraph pipeline all work).

Backend routes, for reference: `POST /api/v1/audit`,
`GET /api/v1/bank_entities`, `POST /api/v1/changes/diff`,
`POST /api/v1/obligations/relations`, `GET /api/v1/lineage/{run_id}`,
`GET`/`POST /api/v1/review` (the human review queue — also mounted bare at
`/review`).

---

## 11. Run the test suite

```powershell
make test
# or directly:
.venv\Scripts\python.exe -m pytest tests/ -v
```

All tests mock the network boundary (LLM clients, Supabase) — no live API
keys or database connection needed to run them. Expect ~2–5 minutes; one
integration test loads the real embedding/reranker models, which is the slow
part.

To reproduce the reliability numbers cited in the architecture deck
(extraction F1, provenance grounding rate, mapping accuracy, etc.), run the
eval harness instead — this one does need live LLM keys on a cold cache:

```powershell
make eval
# or directly:
.venv\Scripts\python.exe evals/run_benchmarks.py
```

Writes `evals/results.json` and `evals/BENCHMARK_REPORT.md`. Every scorer
call goes through the same LLM cache as everything else, so a second run
replays instantly instead of re-hitting live providers.

---

## 12. Troubleshooting

- **`make` recipes fail with "CreateProcess ... failed"**: GNU Make's
  builtin-detection quirk on the GnuWin32 build — already worked around in
  the Makefile (every recipe line is `cd .`-prefixed). If you edit the
  Makefile yourself, keep that prefix or recipes will silently break again.
- **`make api` crash-loops with import tracebacks / `KeyboardInterrupt`
  during startup**: if this repo lives inside a OneDrive/Dropbox/cloud-synced
  folder, background sync continuously touches file timestamps, which
  `--reload`'s file watcher reads as "changed" and restarts uvicorn mid-import.
  Already mitigated by scoping `--reload-dir` to `src`/`config` only — if you
  still see this, check nothing else (antivirus, a stray `pip install`) is
  touching `.venv/`.
- **Port silently swallows every request, backend logs show nothing**: seen
  on some corporate-managed machines where endpoint-security software
  intercepts specific ports. Fix: change `API_PORT` (and `ui/web/.env`'s
  `VITE_API_PORT` to match) to an unused port.
- **`ModuleNotFoundError` running `uvicorn`/`pytest` directly**: you're on the
  system Python, not `.venv`. Always invoke `.venv\Scripts\python.exe -m ...`
  (Windows) / `.venv/bin/python -m ...` (macOS/Linux), or activate the venv
  first.
- **Gemini `API key not valid`**: Google migrated API keys from the legacy
  `AIza...` format to `AQ....` — make sure you generated a fresh key from
  Google AI Studio, not an old saved one.
- **Foundry key rejected against `api.openai.com`**: a Foundry-issued key only
  works when `AZURE_OPENAI_BASE_URL` is also set (§2) — it 400s against the
  plain OpenAI API.
- **`Warning: unauthenticated requests to the HF Hub`** on backend startup:
  harmless — only affects Hugging Face Hub rate limits/download speed for the
  embedding/reranker models, which are cached locally after the first run.
  Ignore unless you hit HF rate limits.

---

## 13. Not implemented yet

These `Makefile` targets exist for documented-interface consistency but exit
1 with an explanation — don't expect them to work:

| Target | Why it's stubbed |
| --- | --- |
| `make run` | No single-document pipeline CLI exists; the pipeline is invoked via `POST /api/v1/audit` instead |
| `make eval-fast` | No cut-down/sampled variant of `make eval` exists (§11 covers the real `make eval`) |
| `make faults` | Fault-injection harness not built |
| `make demo` | No scripted demo-replay harness built |

Also out of scope for now: OCR, multimodal ingestion, and cross-provider
fallback for `EXTRACTOR`.
