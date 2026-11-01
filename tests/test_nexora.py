from __future__ import annotations

import json
from datetime import date, datetime

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import issue_token, password_hasher
from app.agents.query_assistant import ask_data_question
from app.database.connection import make_database_engine
from app.database.models import Base, DatasetInfo, Order
from app.database.seed import seed_demo_dataset
from app.services.analytics import detect_anomalies, forecast_revenue
from app.services.reporting import export_csv, report_payload
from app.sql_policy import SQLPolicyError, validate_select
from scripts.evaluate_nl2sql import evaluate


def test_select_policy_adds_a_bounded_limit():
    sql = validate_select("SELECT id, status FROM orders")
    assert "LIMIT 500" in sql.upper()


def test_sqlite_readonly_engine_rejects_database_writes():
    engine = make_database_engine(read_only=True)
    try:
        with pytest.raises(SQLAlchemyError):
            with engine.begin() as connection:
                connection.execute(text("UPDATE products SET unit_price = unit_price WHERE id = 1"))
    finally:
        engine.dispose()


def test_seed_reset_refuses_a_dataset_labeled_as_real(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'real.sqlite3'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session, session.begin():
        session.add(DatasetInfo(dataset_id="demo", label="Client data", is_synthetic=False,
                                random_seed=1, description="External dataset", generated_at=datetime.now()))
    try:
        with pytest.raises(ValueError, match="non-synthetic"):
            seed_demo_dataset(engine, reset=True)
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(Order)) == 0
    finally:
        engine.dispose()


@pytest.mark.parametrize("sql", [
    "DELETE FROM orders",
    "SELECT id FROM orders; DELETE FROM orders",
    "SELECT * FROM orders",
    "SELECT name FROM sqlite_master",
    "SELECT load_extension('evil') FROM products",
    "SELECT pg_sleep(10) FROM orders",
])
def test_select_policy_rejects_unsafe_sql(sql):
    with pytest.raises(SQLPolicyError):
        validate_select(sql)


def test_anomaly_detection_matches_all_labeled_demo_events():
    evaluation = detect_anomalies()["evaluation"]
    assert evaluation == {"precision": 1.0, "recall": 1.0, "f1": 1.0, "labeled_events": 4}


def test_forecast_reports_holdout_error_metrics():
    forecast = forecast_revenue(horizon=3)
    assert len(forecast["forecast"]) == 3
    assert set(forecast["backtest"]) >= {"mae", "rmse", "mape_pct", "holdout_months"}
    assert all(row["revenue"] >= 0 for row in forecast["forecast"])


def test_forty_question_text_to_sql_benchmark_is_correct():
    result = evaluate()
    assert result["questions"] == 40
    assert result["sql_validation_accuracy"] == 1.0
    assert result["execution_rate"] == 1.0
    assert result["result_accuracy"] == 1.0


def test_no_key_assistant_answers_supported_question_from_demo(monkeypatch):
    monkeypatch.setenv("CREWAI_MODEL", "openai/gpt-4o-mini")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result = ask_data_question("revenue by region in 2025")
    assert result["source"] == "deterministic demo query"
    assert result["row_count"] > 0
    assert result["sql"].upper().startswith("SELECT")


def test_csv_report_contains_synthetic_dataset_metadata():
    payload = report_payload(date(2025, 1, 1), date(2025, 12, 31))
    output = export_csv(payload).decode("utf-8-sig")
    assert "Synthetic enterprise demo data" in output
    assert "KPI,Value" in output


def test_password_login_issues_scoped_role_token(monkeypatch):
    hashed = password_hasher.hash("Nexora-demo-pass-2026")
    monkeypatch.setenv("NEXORA_USERS_JSON", json.dumps({"analyst": {"role": "analyst", "password_hash": hashed}}))
    monkeypatch.setenv("NEXORA_JWT_SECRET", "test-secret-" * 4)
    token, ttl, role = issue_token("analyst", "Nexora-demo-pass-2026")
    assert token and ttl > 0 and role == "analyst"
    with pytest.raises(Exception):
        issue_token("analyst", "wrong-password")
