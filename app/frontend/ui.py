"""NEXORA Streamlit client: dashboards, CrewAI assistant, reports, and database explorer."""

from __future__ import annotations

import json
import os
from datetime import date, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd
import plotly.express as px
import streamlit as st
from dotenv import load_dotenv

load_dotenv()
API_BASE_URL = os.getenv("NEXORA_API_URL", "http://127.0.0.1:8000").rstrip("/")


class APIError(RuntimeError):
    def __init__(self, message: str, status: int = 500):
        super().__init__(message)
        self.status = status


def api_request(path: str, *, method: str = "GET", payload: dict | None = None, query: dict | None = None, timeout: int = 30) -> tuple[object, str]:
    url = API_BASE_URL + path
    if query:
        url += "?" + urlencode({key: value for key, value in query.items() if value is not None})
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    token = st.session_state.get("access_token")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            content_type = response.headers.get("Content-Type", "")
            content = response.read()
        if "json" in content_type:
            return json.loads(content), content_type
        return content, content_type
    except HTTPError as exc:
        try:
            data = json.loads(exc.read().decode("utf-8"))
            message = data.get("detail", f"API returned {exc.code}")
        except (ValueError, OSError):
            message = f"API returned {exc.code}"
        raise APIError(str(message), exc.code) from exc
    except (URLError, TimeoutError) as exc:
        raise APIError(f"Could not reach the NEXORA API at {API_BASE_URL}: {exc}", 503) from exc


def _money(value: float) -> str:
    return f"{value:,.2f}"


def _metrics_cards(kpis: dict[str, object], data_label: str) -> None:
    tiles = [
        ("Revenue", f"{_money(float(kpis.get('revenue', 0)))}", data_label),
        ("Profit", f"{_money(float(kpis.get('profit', 0)))}", f"{kpis.get('profit_margin_pct', 0)}% margin"),
        ("Customers", f"{int(kpis.get('customers', 0)):,}", "completed-order customers"),
        ("Average order", f"{_money(float(kpis.get('average_order_value', 0)))}", f"{kpis.get('revenue_growth_pct', 0)}% revenue growth vs prior period"),
    ]
    columns = st.columns(len(tiles))
    for column, (label, value, help_text) in zip(columns, tiles):
        column.metric(label, value, help=help_text)


def _date_filter(prefix: str) -> tuple[date, date]:
    max_date = date(2026, 10, 31)
    end_default = min(date.today(), max_date)
    start_default = end_default - timedelta(days=89)
    cols = st.columns(2)
    start = cols[0].date_input("Start date", value=start_default, key=f"{prefix}_start")
    end = cols[1].date_input("End date", value=end_default, key=f"{prefix}_end")
    return start, end


def _dashboard(dataset_id: str) -> None:
    st.title("Business overview")
    st.caption("Revenue, cost, profit, and customer trends for completed orders.")
    with st.spinner("Loading dashboard filters…"):
        options, _ = api_request("/api/v1/database/filters", query={"dataset_id": dataset_id})
    with st.form("overview_filters"):
        start, end = _date_filter("overview")
        filters = st.columns(3)
        region = filters[0].selectbox("Region", ["All"] + options["regions"])
        category = filters[1].selectbox("Category", ["All"] + options["categories"])
        segment = filters[2].selectbox("Customer segment", ["All"] + options["segments"])
        submitted = st.form_submit_button("Update dashboard", type="primary")
    query = {"start_date": start.isoformat(), "end_date": end.isoformat(), "dataset_id": dataset_id,
             "region": None if region == "All" else region,
             "category": None if category == "All" else category,
             "segment": None if segment == "All" else segment}
    with st.spinner("Loading sales analytics…"):
        data, _ = api_request("/api/v1/analytics/dashboard", query=query)
    st.caption(f"Data source: {data.get('data_label', 'configured dataset')}")
    _metrics_cards(data["kpis"], data.get("data_label", "configured dataset"))
    if not data["monthly"]:
        st.info("No completed orders match this date range and filter selection. Try a wider date range or reset a filter.")
    if submitted:
        st.toast("Dashboard filters applied")
    monthly = pd.DataFrame(data["monthly"])
    region_data = pd.DataFrame(data["regions"])
    product_data = pd.DataFrame(data["products"])
    left, right = st.columns(2)
    with left:
        st.subheader("Monthly revenue and profit")
        if not monthly.empty:
            chart = monthly.melt(id_vars="month", value_vars=["revenue", "profit"], var_name="measure", value_name="amount")
            st.plotly_chart(px.line(chart, x="month", y="amount", color="measure", markers=True, title="Actual monthly performance"), width="stretch")
        else:
            st.info("No rows match these filters.")
    with right:
        st.subheader("Revenue by region")
        if not region_data.empty:
            st.plotly_chart(px.bar(region_data, x="region", y="revenue", color="region"), width="stretch")
        else:
            st.info("No regional results for this date range.")
    left, right = st.columns(2)
    with left:
        st.subheader("Top products")
        if not product_data.empty:
            st.dataframe(product_data, width="stretch", hide_index=True)
        else:
            st.caption("No product results for this selection.")
    with right:
        st.subheader("Segments")
        segment_data = pd.DataFrame(data["segments"])
        if not segment_data.empty:
            st.dataframe(segment_data, width="stretch", hide_index=True)
        else:
            st.caption("No customer segment results for this selection.")


def _assistant(dataset_id: str, *, sql_only: bool = False) -> None:
    st.title("SQL Explorer" if sql_only else "AI Data Analyst")
    st.caption("CrewAI plans the analysis; NEXORA validates and runs one bounded read-only SELECT.")
    if not sql_only:
        for item in st.session_state.get("chat_history", []):
            with st.chat_message(item["role"]):
                st.markdown(item["content"])
                if item.get("sql"):
                    with st.expander("SQL and result rows"):
                        st.code(item["sql"], language="sql")
                        st.dataframe(pd.DataFrame(item.get("rows", [])), width="stretch", hide_index=True)
    with st.form(f"ask_form_{sql_only}"):
        question = st.text_area("Business question", placeholder="Example: Show revenue by region for Q2 2026", height=90)
        submitted = st.form_submit_button("Analyze question", type="primary")
    if not submitted:
        return
    if not question.strip():
        st.warning("Enter a business question to analyze.")
        return
    history = st.session_state.get("chat_history", [])[-8:] if not sql_only else []
    try:
        with st.spinner("CrewAI is selecting tables, building a safe query, and analyzing the result…"):
            result, _ = api_request("/api/v1/assistant/ask", method="POST", payload={"question": question, "dataset_id": dataset_id, "history": history}, timeout=180)
        if result.get("clarification_required"):
            st.info(result["answer"])
            return
        st.markdown(result["answer"])
        st.caption(f"{result['source']} · {result['latency_ms']} ms · {result['row_count']} rows · {result.get('data_label', 'configured dataset')}")
        with st.expander("Generated SQL and returned rows", expanded=sql_only):
            st.code(result["sql"], language="sql")
            st.dataframe(pd.DataFrame(result["rows"]), width="stretch", hide_index=True)
        if not sql_only:
            st.session_state.setdefault("chat_history", []).extend([
                {"role": "user", "content": question},
                {"role": "assistant", "content": result["answer"], "sql": result["sql"], "rows": result["rows"]},
            ])
    except APIError as exc:
        st.error(str(exc))


def _anomalies(dataset_id: str) -> None:
    st.title("Anomaly detection")
    st.caption("Trailing-median signals help identify unusual movements. Hypotheses are not proven causes.")
    with st.spinner("Checking sales history for unusual movements…"):
        data, _ = api_request("/api/v1/analytics/anomalies", query={"dataset_id": dataset_id})
    evaluation = data["evaluation"]
    cols = st.columns(4)
    cols[0].metric("Signals", len(data["anomalies"]))
    cols[1].metric("Precision", evaluation["precision"] if evaluation["precision"] is not None else "n/a")
    cols[2].metric("Recall", evaluation["recall"] if evaluation["recall"] is not None else "n/a")
    cols[3].metric("F1", evaluation["f1"] if evaluation["f1"] is not None else "n/a", help="Compared with labeled synthetic events only.")
    if data["anomalies"]:
        st.dataframe(pd.DataFrame(data["anomalies"]), width="stretch", hide_index=True)
    else:
        st.info("No points crossed the current thresholds.")
    with st.expander("Known synthetic events used for evaluation"):
        if data["synthetic_labels"]:
            st.dataframe(pd.DataFrame(data["synthetic_labels"]), width="stretch", hide_index=True)
        else:
            st.info("This dataset has no labeled synthetic events; precision and recall are unavailable.")
    st.info(data["note"])


def _forecast(dataset_id: str) -> None:
    st.title("Revenue forecasting")
    st.caption("A linear trend is fit to monthly history. Backtest metrics use a chronological holdout; this baseline does not model seasonality.")
    with st.spinner("Building the revenue baseline and backtest…"):
        data, _ = api_request("/api/v1/analytics/forecast", query={"dataset_id": dataset_id})
    frame = pd.DataFrame(data["history"] + data["forecast"])
    st.plotly_chart(px.line(frame, x="month", y="revenue", color="kind", markers=True, title="Historical revenue and three-month forecast"), width="stretch")
    cols = st.columns(4)
    cols[0].metric("MAE", _money(data["backtest"]["mae"]))
    cols[1].metric("RMSE", _money(data["backtest"]["rmse"]))
    cols[2].metric("MAPE", f"{data['backtest']['mape_pct']}%" if data["backtest"]["mape_pct"] is not None else "n/a")
    cols[3].metric("Holdout", f"{data['backtest']['holdout_months']} months")
    st.dataframe(pd.DataFrame(data["forecast"]), width="stretch", hide_index=True)
    st.warning(data["limitations"])


def _reports(dataset_id: str) -> None:
    st.title("Business reports")
    st.write("Generate PDF, Excel, or CSV reports from the selected dataset and filters.")
    with st.form("report_form"):
        start, end = _date_filter("report")
        form_cols = st.columns(3)
        region = form_cols[0].text_input("Region (optional)")
        category = form_cols[1].text_input("Category (optional)")
        segment = form_cols[2].text_input("Segment (optional)")
        report_format = st.selectbox("Format", ["pdf", "xlsx", "csv"])
        submitted = st.form_submit_button("Build report", type="primary")
    if submitted:
        payload = {"start_date": start.isoformat(), "end_date": end.isoformat(), "dataset_id": dataset_id,
                   "region": region or None, "category": category or None, "segment": segment or None, "format": report_format}
        try:
            content, _ = api_request("/api/v1/reports/export", method="POST", payload=payload, timeout=60)
            st.download_button("Download report", data=content, file_name=f"nexora-report.{report_format}", mime={"pdf": "application/pdf", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "csv": "text/csv"}[report_format])
        except APIError as exc:
            st.error(str(exc))


def _database_admin(dataset_id: str) -> None:
    st.title("Database management")
    try:
        overview, _ = api_request("/api/v1/database/overview", query={"dataset_id": dataset_id})
        schema, _ = api_request("/api/v1/database/schema", query={"dataset_id": dataset_id})
    except APIError as exc:
        st.error(str(exc))
        return
    table_names = [item["name"] for item in overview["tables"]]
    selection = st.selectbox("Table", table_names)
    row = next(item for item in overview["tables"] if item["name"] == selection)
    st.metric("Rows", f"{row['row_count']:,}")
    st.json(row["columns"])
    if st.button("Preview first 50 rows"):
        preview, _ = api_request(f"/api/v1/database/tables/{selection}/preview", query={"dataset_id": dataset_id, "limit": 50})
        st.dataframe(pd.DataFrame(preview["rows"]), width="stretch", hide_index=True)
    with st.expander("Foreign keys and schema"):
        st.json(schema)


def _settings() -> None:
    st.title("Settings")
    st.write(f"**API:** `{API_BASE_URL}`")
    st.write(f"**Model:** `{os.getenv('CREWAI_MODEL', 'openai/gpt-4o-mini')}`")
    st.write("**Conversation storage:** Streamlit session only; chat history is not written to the database.")
    st.write("**Database credentials:** configured on the backend through environment variables; not sent to this client.")
    if st.session_state.get("access_token"):
        st.success(f"Signed in as role: {st.session_state.get('user_role', 'user')}")
        if st.button("Sign out"):
            for key in ("access_token", "user_role", "chat_history"):
                st.session_state.pop(key, None)
            st.rerun()


def main() -> None:
    st.set_page_config(page_title="NEXORA AI", page_icon="✦", layout="wide", initial_sidebar_state="expanded")
    st.title("NEXORA AI")
    st.caption("Enterprise intelligence and analytics · CrewAI orchestration · Read-only data access")
    try:
        auth_info, _ = api_request("/api/v1/auth/status", timeout=5)
        health, _ = api_request("/health", timeout=5)
    except APIError as exc:
        st.error(f"NEXORA API is unavailable: {exc}")
        st.stop()
    if auth_info["auth_enabled"] and not st.session_state.get("access_token"):
        st.subheader("Sign in")
        with st.form("login_form"):
            username = st.text_input("Username")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Sign in", type="primary")
        if submitted:
            try:
                token, _ = api_request("/api/v1/auth/token", method="POST", payload={"username": username, "password": password})
                st.session_state["access_token"] = token["access_token"]
                st.session_state["user_role"] = token["role"]
                st.rerun()
            except APIError as exc:
                st.error(str(exc))
        st.stop()
    try:
        catalog, _ = api_request("/api/v1/datasets")
    except APIError as exc:
        st.error(str(exc))
        st.stop()
    datasets = [item for item in catalog["datasets"] if item.get("available")]
    if not datasets:
        st.error("No seeded dataset is available. Run `python -m app.database.seed` on the backend.")
        st.stop()
    role = st.session_state.get("user_role", "admin")
    pages = ["Overview", "Analytics Dashboard", "Settings"]
    if role in {"analyst", "admin"}:
        pages[1:1] = ["AI Data Analyst", "SQL Explorer"]
        pages[-1:-1] = ["Anomaly Detection", "Revenue Forecasting", "Business Reports"]
    if role == "admin":
        pages.insert(-1, "Database Management")
    with st.sidebar:
        st.markdown("### NEXORA AI")
        page = st.radio("Navigate", pages, label_visibility="collapsed")
        dataset_by_id = {item["id"]: item for item in datasets}
        selected_dataset = st.selectbox("Dataset", list(dataset_by_id), format_func=lambda value: dataset_by_id[value]["label"])
        current_dataset = dataset_by_id[selected_dataset]
        st.caption("Synthetic demo data" if current_dataset.get("is_synthetic") else "Configured business dataset")
        st.caption(f"API · {health['status']} · CrewAI")
    try:
        if page in ("Overview", "Analytics Dashboard"):
            _dashboard(selected_dataset)
        elif page == "AI Data Analyst":
            _assistant(selected_dataset)
        elif page == "SQL Explorer":
            _assistant(selected_dataset, sql_only=True)
        elif page == "Anomaly Detection":
            _anomalies(selected_dataset)
        elif page == "Revenue Forecasting":
            _forecast(selected_dataset)
        elif page == "Business Reports":
            _reports(selected_dataset)
        elif page == "Database Management":
            _database_admin(selected_dataset)
        else:
            _settings()
    except APIError as exc:
        st.error(str(exc))
    except ValueError as exc:
        st.error(str(exc))


main()
