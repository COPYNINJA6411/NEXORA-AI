# NEXORA AI — Project guide

## What it does

NEXORA AI is a local-first portfolio application for asking questions about business data, reviewing dashboards and anomaly signals, forecasting sales, and exporting reports. A separate FastAPI service owns the data and business logic. Streamlit calls it over HTTP. CrewAI is the primary agent orchestrator; an MCP server exposes a small set of read-only tools so other MCP clients, including CrewAI, can reuse the same analytics.

The earlier CFO Finance Assistant and DocuMind projects remain in the containing workspace, outside this standalone NEXORA folder.

All bundled NEXORA records are generated synthetic data. Values use synthetic demo currency units. The project is a portfolio demonstration, not a production-hardened reporting service.

## Architecture

```text
User ── Streamlit ──HTTP──> FastAPI ──> Analytics services ──> SQLAlchemy ──> SQLite/PostgreSQL
                                  ├── CrewAI agents and bounded tools
                                  └── MCP Streamable HTTP tools ──> CrewAI or another MCP host
```

The UI does not connect to the database. SQL requests pass through a SQLGlot `SELECT` policy, a table and column allow-list, a read-only connection, an execution timeout, and a 500 row cap. CrewAI tools call the same application services. The MCP endpoint publishes specific analytics operations; it does not expose free-form SQL.

## Technologies

| Technology | What it does here |
| --- | --- |
| Python | Application, seed generator, and integrations. |
| FastAPI | Versioned HTTP API, auth, request validation, and service endpoints. |
| Streamlit | Separate client for dashboards, assistant, database explorer, forecast, anomalies, and reports. |
| CrewAI | Primary multi-agent workflow for NL-to-SQL and evidence-based sales briefs. |
| MCP Python SDK | Streamable HTTP interoperability for approved tools. |
| SQLAlchemy, SQLite, Psycopg/PostgreSQL | Relational models and local or server database connections. |
| SQLGlot | Parses generated SQL before it is allowed to run. |
| Pandas and NumPy | Data shaping, aggregate analysis, and evaluation calculations. |
| scikit-learn | Linear regression baseline for forecasting, with a NumPy fallback. |
| Plotly | Interactive charts in Streamlit. |
| PyJWT and pwdlib/Argon2 | Optional bearer-token login and password hashing. |
| ReportLab and openpyxl | PDF and Excel report exports. |
| Docker Compose | Starts API and UI as separate local services. |
| Pytest | Security, analytics, authentication, and question-evaluation checks. |

RAG is intentionally not in the app: the current schema and metric definitions are small enough to inspect directly. Add retrieval if governed definitions or a large documentation corpus later needs search.

## Features and phase acceptance

The implementation is complete for this portfolio scope. Gates are recorded in [the architecture plan](nexora-architecture.md).

| Phase | Completed acceptance evidence |
| --- | --- |
| 1. Runnable foundation | FastAPI health route and separate Streamlit client; local and Docker run paths documented. |
| 2. Database | Reproducible SQLite seed, PostgreSQL URL support, explicit dataset catalog, labeled synthetic data. |
| 3–4. Question to safe SQL | Schema-grounded CrewAI SQL path, deterministic no-key demo routing, displayed SQL/results, AST allow-list and timeout/row cap. |
| 5. Agent analysis | CrewAI analyst/editor workflow returns exact source metrics separately from prose. |
| 6. Dashboard | Date, region, category, and segment filters with KPI and chart views. |
| 7. Anomaly detection | Sales, marketing expense, product, and customer-volume signals; labeled demo event evaluation. |
| 8. Forecast | Linear-regression trend and chronological holdout MAE, RMSE, and nonzero-only MAPE. |
| 9. Reports | Filtered CSV, XLSX, and PDF exports. |
| 10. Frontend and API | Streamlit uses FastAPI endpoints for all main workflows. |
| 11. Security and quality | Optional roles, password hashes/JWT, read-only SQL controls, and tests. |
| 12. Handoff | Setup, architecture, configuration, evaluation, Docker, and limitations documented. |

## Setup and run

Use Python 3.11–3.13. From the repository root in PowerShell:

```powershell
py -V:3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[mcp,dev]"
Copy-Item .env.example .env
python -m app.database.seed
```

Start the API and UI in separate terminals:

```powershell
python -m uvicorn app.backend.main:app --reload --port 8000
python -m streamlit run app/frontend/ui.py
```

Open `http://127.0.0.1:8501`. The API docs are at `http://127.0.0.1:8000/docs`; health is at `/health`. This standalone folder's `.env.example` contains only NEXORA settings. Keep `.env` private and do not overwrite an existing local configuration.

To use Docker, copy `.env.example` to `.env`, then run:

```powershell
docker compose -f docker-compose.nexora.yml up --build
```

The UI is at `http://127.0.0.1:8501`. SQLite is persisted in the `nexora_data` volume. Docker ports are bound to loopback.

## Configure model providers

`CREWAI_MODEL` chooses the CrewAI model (for example, `openai/gpt-4o-mini`). Set that provider's key in `.env`, such as `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `MISTRAL_API_KEY`, or `GROQ_API_KEY`. Ollama-compatible model names are also recognized. Without a provider key, the question assistant uses a limited deterministic demo router; the multi-agent sales brief requires a working CrewAI provider.

Keys are not stored by the app in its database or included in reports. Prompts/results are not persisted by NEXORA. A configured model provider may process prompts under its own service terms and retention policy; avoid sending sensitive company data in this portfolio demo.

## Configure authentication

Authentication is disabled for the local no-login demo. For user and role checks, create an Argon2 password hash interactively:

```powershell
python scripts/create_nexora_user.py
```

Copy the JSON entry into private `NEXORA_USERS_JSON`, set `NEXORA_AUTH_ENABLED=true`, and set `NEXORA_JWT_SECRET` to a random secret of at least 32 characters. Roles are `viewer`, `analyst`, and `admin`. The API issues expiring JWT bearer tokens; the Streamlit client stores the token only in its session state. Keep the demo on localhost. The MCP endpoint is disabled when app authentication is enabled because MCP OAuth/token verification is not implemented in this portfolio app.

## Datasets and database

The default `NEXORA_DATABASE_URL` points to `sqlite:///./data/nexora_demo.sqlite3`. PostgreSQL uses a SQLAlchemy URL such as `postgresql+psycopg://user:password@host:5432/database`. Do not use an application owner or write-enabled role for an external dataset; grant SELECT access only.

Multiple datasets can be configured with `NEXORA_DATASETS_JSON`, for example:

```text
{"demo":"sqlite:///./data/nexora_demo.sqlite3","finance":"postgresql+psycopg://readonly:REDACTED@db.example:5432/finance"}
```

Treat database URLs as secrets and store them in a private environment or secret manager. Dataset IDs are passed to API routes; connection strings are never returned from the dataset endpoint.

The seed creates regions, customers, products, employees, orders, order items, sales targets, marketing expenses, and dataset metadata. Four labeled synthetic events cover West-region revenue drop, marketing spend spike, product sales surge, and customer order burst. To replace the local demo data, use `python -m app.database.seed --reset`.

## Evaluation and tests

The deterministic NL-to-SQL smoke benchmark compares 40 question phrasings against reviewed SQL results:

```powershell
python scripts/evaluate_nl2sql.py
```

It reports SQL validation accuracy, execution rate, result accuracy, and local latency. The current local run scored 40/40 (100%) on this synthetic fixture. The anomaly evaluator reports precision/recall/F1 against four intentionally injected labels; current run scored 1.0 for each. These scores do not measure real business data or LLM-generated SQL quality. Forecast backtesting reports MAE/RMSE and MAPE for nonzero holdout actuals.

Run the automated suite with:

```powershell
python -m pytest -q
```

## Production boundary

Before serving real business data, add an identity provider, MCP OAuth/token verification, managed secret storage, database-enforced least privilege, TLS, rate limits, audit policy, backups, monitoring, deployment-specific privacy/retention controls, and a security review. Verify schema and metric definitions with the data owner. Forecasts are a simple linear trend and do not model seasonality or external drivers. Anomaly explanations are investigation hypotheses, not proven causes.
