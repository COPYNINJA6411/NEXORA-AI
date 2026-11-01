"""NEXORA AI HTTP API, CrewAI workflows, database services, and optional MCP mount."""

from contextlib import asynccontextmanager
from datetime import date
import logging
import os
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from starlette.responses import Response

from app.auth import auth_enabled, current_principal, issue_token, require_roles
from app.database.connection import configured_datasets
from app.services import analytics, reporting
from app.sql_policy import SQLPolicyError

try:
    from app.integrations.mcp_server import mcp_server
except ModuleNotFoundError as exc:
    if exc.name != "mcp":
        raise
    mcp_server = None

# The MCP SDK server is intentionally local-demo only until a token verifier is configured.
if mcp_server is not None and not auth_enabled():
    allowed_hosts = [host.strip() for host in os.getenv("NEXORA_MCP_ALLOWED_HOSTS", "").split(",") if host.strip()]
    if allowed_hosts:
        from mcp.server.transport_security import TransportSecuritySettings

        mcp_asgi_app = mcp_server.streamable_http_app(transport_security=TransportSecuritySettings(allowed_hosts=allowed_hosts))
    else:
        mcp_asgi_app = mcp_server.streamable_http_app()
else:
    mcp_asgi_app = None


@asynccontextmanager
async def app_lifespan(_app):
    if mcp_asgi_app is not None:
        async with mcp_server.session_manager.run():
            yield
    else:
        yield


app = FastAPI(
    title="NEXORA AI API",
    version="1.0.0",
    description="Local-first enterprise analytics API with CrewAI orchestration and guarded SQL.",
    lifespan=app_lifespan,
)
logger = logging.getLogger("nexora.api")


class LoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=1, max_length=1024)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    role: str


class ConversationMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=4000)


class QuestionRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    dataset_id: str = Field(default="demo", pattern=r"^[A-Za-z0-9_-]{1,40}$")
    history: list[ConversationMessage] = Field(default_factory=list, max_length=20)


class SalesBriefRequest(BaseModel):
    start_date: date
    end_date: date
    region: str | None = None
    dataset_id: str = Field(default="demo", pattern=r"^[A-Za-z0-9_-]{1,40}$")


class ReportRequest(BaseModel):
    start_date: date
    end_date: date
    region: str | None = None
    category: str | None = None
    segment: str | None = None
    dataset_id: str = Field(default="demo", pattern=r"^[A-Za-z0-9_-]{1,40}$")
    format: Literal["csv", "xlsx", "pdf"] = "pdf"


@app.get("/health", tags=["Operations"])
def health() -> dict[str, object]:
    return {"status": "ok", "service": "nexora-api", "orchestrator": "CrewAI", "version": app.version}


@app.get("/api/v1", tags=["Metadata"])
def api_metadata() -> dict[str, object]:
    return {"product": "NEXORA AI", "api_version": "v1", "orchestrator": "CrewAI", "docs": "/docs"}


@app.get("/api/v1/auth/status", tags=["Authentication"])
def auth_status() -> dict[str, object]:
    return {"auth_enabled": auth_enabled(), "roles": ["viewer", "analyst", "admin"]}


@app.post("/api/v1/auth/token", response_model=LoginResponse, tags=["Authentication"])
def create_access_token(request: LoginRequest) -> LoginResponse:
    if not auth_enabled():
        raise HTTPException(status_code=409, detail="Authentication is disabled in local-demo mode.")
    token, ttl, role = issue_token(request.username, request.password)
    return LoginResponse(access_token=token, expires_in=ttl, role=role)


@app.get("/api/v1/datasets", tags=["Database"], dependencies=[Depends(require_roles("viewer", "analyst", "admin"))])
def available_datasets() -> dict[str, object]:
    try:
        return analytics.list_datasets()
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/v1/database/overview", tags=["Database"], dependencies=[Depends(require_roles("admin"))])
def database_overview(dataset_id: str = "demo") -> dict[str, object]:
    try:
        return analytics.database_overview(dataset_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/v1/database/schema", tags=["Database"], dependencies=[Depends(require_roles("analyst", "admin"))])
def database_schema(dataset_id: str = "demo") -> dict[str, object]:
    try:
        return analytics.describe_schema(dataset_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/v1/database/tables/{table_name}/preview", tags=["Database"], dependencies=[Depends(require_roles("admin"))])
def table_preview(table_name: str, dataset_id: str = "demo", limit: int = Query(default=50, ge=1, le=100)) -> dict[str, object]:
    try:
        return analytics.preview_table(table_name, dataset_id=dataset_id, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/v1/database/filters", tags=["Database"], dependencies=[Depends(require_roles("viewer", "analyst", "admin"))])
def database_filter_options(dataset_id: str = "demo") -> dict[str, list[str]]:
    return analytics.dashboard_filter_options(dataset_id)


@app.post("/api/v1/assistant/ask", tags=["Assistant"], dependencies=[Depends(require_roles("analyst", "admin"))])
def ask_assistant(request: QuestionRequest) -> dict[str, object]:
    try:
        from app.agents.query_assistant import ask_data_question

        return ask_data_question(
            request.question,
            dataset_id=request.dataset_id,
            history=[message.model_dump() for message in request.history],
        )
    except SQLPolicyError as exc:
        raise HTTPException(status_code=400, detail=f"The generated query was rejected: {exc}") from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=408, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("CrewAI assistant request failed")
        raise HTTPException(status_code=502, detail="The assistant could not complete this request. Check database and LLM configuration.") from exc


@app.post("/api/v1/assistant/sales-brief", tags=["Assistant"], dependencies=[Depends(require_roles("analyst", "admin"))])
def generate_sales_brief(request: SalesBriefRequest) -> dict[str, object]:
    if request.end_date < request.start_date:
        raise HTTPException(status_code=422, detail="end_date must be on or after start_date")
    try:
        from app.agents.sales_brief import build_sales_brief

        brief, source_metrics = build_sales_brief(request.start_date, request.end_date, request.region, request.dataset_id)
        return {"orchestrator": "CrewAI", "brief": brief, "source_metrics": source_metrics, "data_label": source_metrics.get("data_label", "configured dataset")}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("CrewAI sales brief generation failed")
        raise HTTPException(status_code=502, detail="CrewAI could not generate the brief. Check the configured LLM provider and database.") from exc


@app.get("/api/v1/analytics/dashboard", tags=["Analytics"], dependencies=[Depends(require_roles("viewer", "analyst", "admin"))])
def dashboard(
    start_date: date = date(2026, 1, 1), end_date: date = date(2026, 10, 31),
    region: str | None = None, category: str | None = None, segment: str | None = None, dataset_id: str = "demo",
) -> dict[str, object]:
    try:
        return analytics.dashboard_data(start_date, end_date, region=region, category=category, segment=segment, dataset_id=dataset_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/v1/analytics/anomalies", tags=["Analytics"], dependencies=[Depends(require_roles("analyst", "admin"))])
def anomalies(dataset_id: str = "demo") -> dict[str, object]:
    return analytics.detect_anomalies(dataset_id)


@app.get("/api/v1/analytics/forecast", tags=["Analytics"], dependencies=[Depends(require_roles("analyst", "admin"))])
def forecast(dataset_id: str = "demo", horizon: int = Query(default=3, ge=1, le=18)) -> dict[str, object]:
    try:
        return analytics.forecast_revenue(dataset_id, horizon=horizon)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/v1/reports/export", tags=["Reports"], dependencies=[Depends(require_roles("analyst", "admin"))])
def export_report(request: ReportRequest) -> Response:
    try:
        payload = reporting.report_payload(
            request.start_date, request.end_date, region=request.region, category=request.category,
            segment=request.segment, dataset_id=request.dataset_id,
        )
        if request.format == "csv":
            content, media_type = reporting.export_csv(payload), "text/csv; charset=utf-8"
        elif request.format == "xlsx":
            content, media_type = reporting.export_xlsx(payload), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        else:
            content, media_type = reporting.export_pdf(payload), "application/pdf"
        filename = f"nexora-report-{request.start_date}-{request.end_date}.{request.format}"
        return Response(content=content, media_type=media_type, headers={"Content-Disposition": f'attachment; filename="{filename}"'})
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/v1/admin/demo/seed", tags=["Database"], dependencies=[Depends(require_roles("admin"))])
def reseed_demo(reset: bool = False) -> dict[str, object]:
    from app.database.connection import make_database_engine
    from app.database.seed import seed_demo_dataset

    engine = make_database_engine(dataset_id="demo")
    try:
        return seed_demo_dataset(engine, reset=reset)
    finally:
        engine.dispose()


if mcp_asgi_app is not None:
    app.mount("/", mcp_asgi_app)
