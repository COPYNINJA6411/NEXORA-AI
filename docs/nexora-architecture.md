# NEXORA AI — Architecture and phase gates

## Product and scope

NEXORA AI is a local-first portfolio demonstration of enterprise analytics in its own folder and Git repository. The CFO Finance Assistant and DocuMind projects remain separate in the containing workspace.

The portfolio application is not production-hardened. Production use would require a security and reliability review, managed secrets, identity integration, operational monitoring, and deployment-specific controls.

## Architecture decisions

- **Backend:** FastAPI owns API contracts, authentication/authorization, analytics orchestration, SQL policy enforcement, and database access. API versions live under `/api/v1`.
- **Agent orchestration:** CrewAI is NEXORA's primary multi-agent framework. Business workflows use Crews with narrowly scoped tools; model selection is configured through `CREWAI_MODEL` and provider credentials.
- **Frontend:** Streamlit is a separate client and communicates with FastAPI over HTTP. It does not connect directly to business databases.
- **Database:** SQLite is the local no-service default; PostgreSQL is the deployable relational option. SQLAlchemy provides a shared access layer. Database URLs and credentials come from environment configuration.
- **AI providers:** CrewAI's LLM configuration supports choosing the model/provider by environment. Keys stay in environment/secret storage and are never checked into source. Database and seed operations do not need an LLM key.
- **RAG:** not part of the current foundation. Add only when governed metric or schema documentation needs retrieval at runtime.
- **Analytics and presentation:** Pandas, NumPy, scikit-learn, and Plotly for analysis and visualization. Forecast evaluation includes a chronological holdout with MAE, RMSE, and nonzero-only MAPE.
- **Local deployment:** services run independently in local processes and have separate Docker images in `docker-compose.nexora.yml`.
- **Agent interoperability:** the MCP adapter exposes selected read-only NEXORA services through Streamable HTTP at `/mcp`. CrewAI agents use the same application services through typed tools; other MCP hosts can call the interoperable MCP interface.
- **Privacy:** default to local synthetic data; do not persist prompts or query results unless a feature explicitly requires it. If conversation persistence is introduced, define retention, deletion, and access controls before implementation. API keys are excluded from logs and reports.
- **Security boundary:** generated SQL is parsed as a single read-only `SELECT`, restricted to approved tables/columns, executed through a read-only connection with a statement timeout and row cap. PostgreSQL is set transaction read-only; SQLite uses URI read-only mode plus `query_only`. Application checks supplement database permissions.
- **Authentication and authorization:** optional JWT bearer authentication uses Argon2 password hashes and viewer/analyst/admin roles. Local demo mode is unauthenticated. MCP is disabled when API authentication is enabled because MCP OAuth/token verification is not implemented. Docker ports are bound to loopback.

## Component map

```text
Streamlit client (app/frontend)
        │ HTTP / JSON
        ▼
FastAPI service (app/backend)
        ├── authentication and roles
        ├── analytics / AI provider adapters
        ├── SQL validation and execution policy
        └── SQLAlchemy database layer
                ├── SQLite (local default)
                └── PostgreSQL (configured deployment)
```

## Local run instructions

Install the project and MCP SDK in a Python 3.11–3.13 virtual environment with `python -m pip install -e ".[mcp]"` (on Windows, use `py -V:3.12 -m venv .venv` first). CrewAI is installed as NEXORA's primary orchestration dependency. Start the services in separate terminals from the repository root:

```powershell
python -m uvicorn app.backend.main:app --reload --port 8000
python -m streamlit run app/frontend/ui.py
```

The API is available at `http://127.0.0.1:8000`; its health endpoint is `/health` and interactive API documentation is `/docs`. The Streamlit client defaults to `http://127.0.0.1:8000`. Set `NEXORA_API_URL` if the API runs elsewhere.

CrewAI is NEXORA's primary orchestration dependency. Install MCP support with `python -m pip install -e ".[mcp]"`. With the extra installed, the same FastAPI process exposes NEXORA's MCP tools at `http://127.0.0.1:8000/mcp`. The tools are allow-listed application operations; they do not accept arbitrary SQL. Docker publishes the demo services only on loopback. For another MCP hostname, set `NEXORA_MCP_ALLOWED_HOSTS` to the exact host:port values that should be accepted.

To build and start both services with Docker:

```powershell
docker compose -f docker-compose.nexora.yml up --build
```

Open `http://localhost:8501` for Streamlit and `http://localhost:8000/docs` for the API. Stop the services with `Ctrl+C` or `docker compose -f docker-compose.nexora.yml down`.

## Phase 2 database and demo data

The SQLAlchemy schema in `app/database/models.py` defines `regions`, `customers`, `products`, `employees`, `orders`, `order_items`, `sales_targets`, and `marketing_expenses`, with primary keys, foreign keys, and uniqueness constraints. `dataset_info` marks the generated demo records as synthetic and records the generation seed. The generator uses seed `20261102` and creates a relational multi-year dataset; rerunning the seed command is idempotent. Use `--reset` only when you intend to replace the demo rows.

SQLite is the default and stores the database at `./data/nexora_demo.sqlite3`. PostgreSQL is supported through the Psycopg 3 driver. Set the URL in `.env` or the process environment; use the `postgresql+psycopg://` SQLAlchemy scheme for PostgreSQL. Keep credentials in the private `.env` file and do not put credentials in source control or logs.

```powershell
Copy-Item .env.example .env  # skip if .env already exists; merge NEXORA_* settings instead
python -m app.database.seed
```

Choose the active dataset explicitly with `NEXORA_DATASET=demo`. The API lists the selected dataset at `/api/v1/datasets`; it reports the synthetic label and does not expose the database URL. The Docker Compose configuration persists the SQLite database in a named volume by default. For a PostgreSQL instance, set `NEXORA_DATABASE_URL` to its connection URL in the process environment or private `.env` before starting Compose, then seed once with `docker compose -f docker-compose.nexora.yml run --rm api python -m app.database.seed`.

## MCP and CrewAI example

`app/integrations/mcp_server.py` exposes three read-only tools: list available datasets, inspect the business schema, and summarize completed-order sales over a date range with an optional region filter. These reuse `app/services/analytics.py`, which is also available to FastAPI handlers. The MCP SDK is an optional extra; CrewAI is the primary orchestration dependency for NEXORA.

`app/agents/sales_brief.py` is the primary two-agent CrewAI workflow: a Sales Data Analyst retrieves metrics through a bounded tool, then an Executive Brief Editor writes a concise evidence-based result. FastAPI exposes it at `/api/v1/assistant/sales-brief` and the Streamlit UI calls this endpoint. The response preserves deterministic source metrics separately from model-written prose.

`examples/crewai_sales_brief.py` is an additional interoperability example: a CrewAI agent connects to NEXORA's MCP `summarize_sales` tool. Configure an LLM key/provider in the private environment before calling either CrewAI workflow. Keep the unauthenticated MCP endpoint on localhost unless authentication and deployment controls are added.

## Phase plan and acceptance gates

The implementation has progressed through the documented phases. Each phase has explicit acceptance criteria; the test and evaluation commands below are reproducible locally.

| Phase | Deliverable | Acceptance criteria |
| --- | --- | --- |
| 1. Architecture and setup | Architecture record and runnable service/client skeleton | FastAPI starts and responds to `/health`; separate Streamlit client reaches it; local and Docker instructions are documented; existing project work is preserved. |
| 2. Database and demo data | SQLite/PostgreSQL data layer and reproducible synthetic schema | Both supported database configurations are documented; seeded data has keys/relationships and is labeled synthetic; dataset selection is explicit; seed command is idempotent and reports table counts. |
| 3. Natural language to SQL | Question intake, schema selection, SQL generation | 40 paraphrased demo questions produce accepted SQL and match independent result queries; ambiguous cases request clarification. |
| 4. SQL validation and execution | Safe query execution boundary | Single `SELECT` parsing, table/column allow-listing, read-only connections, timeout, and row limit are enforced; unsafe inputs are rejected before execution. |
| 5. Business intelligence assistant | Context-aware analytical responses | CrewAI orchestrates specialist agents using bounded tools; multi-turn context and supported metrics work; answers include source metrics and distinguish evidence from hypotheses. |
| 6. Interactive dashboard | Filterable analytics views | KPIs and charts derive from selected data; filters update displayed results; empty and error states are usable. |
| 7. Anomaly detection | Statistical anomaly workflows | Repeatable synthetic anomalies are identified; results include metric, period, magnitude, and method; explanatory hypotheses are labeled. |
| 8. Forecasting | Historical/forecast comparison | Forecasts are generated from observed data, visualized with history, and evaluated with MAE/RMSE plus MAPE only where actuals are nonzero and meaningful. |
| 9. Reporting | Downloadable business reports | PDF, CSV, and Excel exports reflect current database/analytics outputs and identify the dataset and generation time. |
| 10. Frontend integration | Complete Streamlit-to-API product flows | UI calls backend endpoints for implemented features; no direct database access from the frontend; loading, failure, and empty states are handled. |
| 11. Quality and security review | Repeatable verification and documented boundaries | Automated suite covers SQL policy, analytics, auth token issuance, and 40 NL-to-SQL cases; SQL, auth, secrets, privacy, error handling, and dependency boundaries are documented. |
| 12. Documentation and deployment | Portfolio-ready project handoff | README covers setup, architecture, configuration, sample questions, API, evaluation, limitations, and Docker local deployment; no production-hardening claim is made without evidence. |

## Evaluation definitions

- **SQL execution success:** fraction of in-scope evaluation questions that produce an accepted query and execute successfully.
- **SQL/result correctness:** compare normalized result rows against reviewed gold results for the same fixture; allow equivalent ordering only when the question does not request ordering. Report execution success separately from correctness.
- **Anomaly detection:** on labeled synthetic cases, report precision, recall, and F1; record false positives by metric/period. Do not claim performance on real business data from synthetic labels.
- **Forecasting:** use chronological holdout periods and report MAE and RMSE; report MAPE only for nonzero actuals and state exclusions.
- **Latency:** wall-clock response time, with provider/network time separated when available. Do not present local results as production service levels.
