# NEXORA AI

NEXORA AI is a local-first portfolio application for business analytics. It demonstrates a separate FastAPI backend and Streamlit client, guarded natural-language-to-SQL, CrewAI multi-agent workflows, MCP interoperability, dashboards, anomaly signals, forecasting, and downloadable reports.

All bundled business records are reproducible synthetic data. This is a portfolio demonstration, not a production-hardened reporting service.

## Run locally

Use Python 3.11–3.13. In PowerShell, from this folder:

```powershell
py -V:3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[mcp,dev]"
Copy-Item .env.example .env
python -m app.database.seed
```

Add a model-provider key to the private `.env` file for CrewAI features. In two separate terminals, start the API and UI:

```powershell
python -m uvicorn app.backend.main:app --reload --port 8000
python -m streamlit run app/frontend/ui.py
```

Open `http://127.0.0.1:8501`. The API health check is `http://127.0.0.1:8000/health`; API docs are at `/docs`. The MCP endpoint is `/mcp` when the MCP extra is installed and app authentication is disabled.

## Docker

```powershell
Copy-Item .env.example .env
docker compose -f docker-compose.nexora.yml up --build
```

The UI is at `http://127.0.0.1:8501`. Both published ports are bound to loopback; SQLite data persists in a Docker volume.

## Verify

```powershell
python -m pytest -q
python scripts/evaluate_nl2sql.py
```

The 40-question benchmark compares generated demo SQL results with separate reference queries. Synthetic anomaly metrics are compared with four seeded, labeled events. These scores do not predict performance on real company data or LLM-generated SQL.

See [the project guide](docs/project-guide.md) for the technology overview and setup details, and [the architecture plan](docs/nexora-architecture.md) for acceptance criteria and security decisions.
