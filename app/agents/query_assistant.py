"""CrewAI text-to-SQL workflow with a deterministic no-key demo fallback."""

from __future__ import annotations

from datetime import date, timedelta
import json
import os
import re
import time

from app.database.connection import make_database_engine
from app.services.analytics import dataset_metadata, describe_schema, execute_readonly_sql
from app.sql_policy import SQLPolicyError


REGIONS = ("North", "South", "East", "West", "Central", "Northeast", "Southeast", "Pacific")


def _llm_configured() -> bool:
    model = os.getenv("CREWAI_MODEL", "openai/gpt-4o-mini").lower()
    if model.startswith(("ollama/", "ollama_chat/", "lm_studio/")):
        return True
    provider = model.split("/", 1)[0]
    provider_env = {
        "openai": ("OPENAI_API_KEY",), "anthropic": ("ANTHROPIC_API_KEY",),
        "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"), "google": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
        "mistral": ("MISTRAL_API_KEY",), "groq": ("GROQ_API_KEY",),
    }
    return any(os.getenv(key) for key in provider_env.get(provider, ()))


def _date_filter(question: str) -> tuple[str, str]:
    q = question.lower()
    quarter = re.search(r"\bq([1-4])\s*(20\d{2})\b", q)
    if quarter:
        year, month = int(quarter.group(2)), (int(quarter.group(1)) - 1) * 3 + 1
        if month == 10:
            next_year, next_month = year + 1, 1
        else:
            next_year, next_month = year, month + 3
        return f"{year}-{month:02d}-01", f"{next_year}-{next_month:02d}-01"
    month_match = re.search(r"\b(20\d{2})-(0[1-9]|1[0-2])\b", q)
    if month_match:
        year, month = int(month_match.group(1)), int(month_match.group(2))
        start = date(year, month, 1)
        end = date(year + (month == 12), month % 12 + 1, 1)
        return start.isoformat(), end.isoformat()
    year_match = re.search(r"\b(20\d{2})\b", q)
    if year_match:
        year = int(year_match.group(1))
        return f"{year}-01-01", f"{year + 1}-01-01"
    if "last month" in q or "previous month" in q:
        today = date.today()
        end = date(today.year, today.month, 1)
        start = end - timedelta(days=1)
        start = date(start.year, start.month, 1)
        return start.isoformat(), end.isoformat()
    if "last quarter" in q or "previous quarter" in q:
        today = date.today()
        this_q = (today.month - 1) // 3
        start_month = this_q * 3 + 1
        end = date(today.year, start_month, 1)
        start = end - timedelta(days=1)
        start_month = ((start.month - 1) // 3) * 3 + 1
        start = date(start.year, start_month, 1)
        return start.isoformat(), end.isoformat()
    return "2024-01-01", "2027-01-01"


def deterministic_sql(question: str, *, dataset_id: str | None = None) -> str | None:
    """Support common portfolio-demo question families without inventing results."""
    q = question.lower()
    if not any(word in q for word in ("revenue", "sales", "profit", "margin", "cost", "order", "customer", "product")):
        return None
    if "segment" in q:
        group = "c.segment"
        joins = "JOIN customers c ON c.id = o.customer_id"
        label = "segment"
    elif "category" in q or "categories" in q:
        group = "p.category"
        joins = "JOIN order_items oi ON oi.order_id = o.id JOIN products p ON p.id = oi.product_id"
        label = "category"
    elif "by customer" in q or "customers" in q or "customer" in q:
        group = "c.name"
        joins = "JOIN customers c ON c.id = o.customer_id"
        label = "customer"
    elif "by product" in q or "products" in q or "product" in q:
        group = "p.name"
        joins = "JOIN order_items oi ON oi.order_id = o.id JOIN products p ON p.id = oi.product_id"
        label = "product"
    elif any(term in q for term in ("region", "north", "south", "east", "west", "central", "pacific")):
        group = "r.name"
        joins = "JOIN customers c ON c.id = o.customer_id JOIN regions r ON r.id = c.region_id"
        label = "region"
    elif any(term in q for term in ("trend", "monthly", "month by month", "over time")):
        engine = make_database_engine(dataset_id=dataset_id)
        try:
            date_expr = "DATE_TRUNC('month', o.order_date)" if engine.dialect.name == "postgresql" else "strftime('%Y-%m', o.order_date)"
        finally:
            engine.dispose()
        group = f"{date_expr}"
        joins = ""
        label = "month"
    else:
        group = ""
        joins = ""
        label = ""
    revenue = "SUM(oi.quantity * oi.unit_price * (1 - oi.discount_pct))"
    cost = "SUM(oi.quantity * p.unit_cost)"
    asks_order_count = "order" in q and not any(term in q for term in ("revenue", "sales", "profit", "margin", "cost"))
    metric = "profit" if "profit" in q or "margin" in q else "cost" if "cost" in q else "orders" if asks_order_count else "revenue"
    needs_product_join = "order_items oi" not in joins
    if needs_product_join:
        joins = (joins + " " if joins else "") + "JOIN order_items oi ON oi.order_id = o.id JOIN products p ON p.id = oi.product_id"
    if metric == "profit":
        expression = f"({revenue} - {cost})"
    elif metric == "cost":
        expression = cost
    elif metric == "orders":
        expression = "COUNT(DISTINCT o.id)"
    else:
        expression = revenue
    alias = "profit" if metric == "profit" else "cost" if metric == "cost" else "orders" if metric == "orders" else "revenue"
    start, end = _date_filter(q)
    where = f"o.status = 'Completed' AND o.order_date >= '{start}' AND o.order_date < '{end}'"
    for region in REGIONS:
        if re.search(rf"\b{region.lower()}\b", q):
            if "regions r" not in joins:
                joins += " JOIN customers c ON c.id = o.customer_id JOIN regions r ON r.id = c.region_id"
            where += f" AND r.name = '{region}'"
            break
    selection = f", {group} AS {label}" if group else ""
    grouping = f" GROUP BY {group}" if group else ""
    ordering = f" ORDER BY {alias} DESC" if group else ""
    top = " LIMIT 10" if any(word in q for word in ("top", "highest", "most", "best")) else ""
    if metric == "profit" and "margin" in q:
        expression = f"({revenue} - {cost}) * 100.0 / NULLIF({revenue}, 0)"
        alias = "profit_margin_pct"
        selection = f", {group} AS {label}" if group else ""
        ordering = f" ORDER BY {alias} DESC" if group else ""
    return (
        f"SELECT {expression} AS {alias}{selection} FROM orders o {joins} "
        f"WHERE {where}{grouping}{ordering}{top}"
    ).strip()


def _strip_sql(raw: str) -> str:
    value = raw.strip()
    if value.upper().startswith("CLARIFY:"):
        return value
    value = re.sub(r"^```(?:sql)?\s*|\s*```$", "", value, flags=re.I)
    match = re.search(r"(?is)\bselect\b.*", value)
    return match.group(0).strip() if match else value


def _crew_generate_sql(question: str, dataset_id: str | None, history: list[dict[str, str]]) -> str:
    from crewai import Agent, Crew, Task
    from crewai.tools import tool

    @tool("inspect_nexora_schema")
    def inspect_nexora_schema() -> str:
        """Retrieve approved business tables, columns, and relationships for the active dataset."""
        return json.dumps(describe_schema(dataset_id), ensure_ascii=False)

    schema_agent = Agent(
        role="NEXORA Schema Analyst",
        goal="Identify the relevant approved tables and fields for a business question",
        backstory="You ground query design in the live NEXORA schema and never assume columns.",
        tools=[inspect_nexora_schema],
        llm=os.getenv("CREWAI_MODEL", "openai/gpt-4o-mini"),
        allow_delegation=False,
        max_iter=2,
        verbose=False,
    )
    history_text = "\n".join(f"{item['role']}: {item['content']}" for item in history[-6:])
    task = Task(
        description=(
            "Call inspect_nexora_schema, then translate the business question into exactly one SQL SELECT. "
            "Use only the listed tables/columns. Completed sales use orders.status='Completed'. "
            "Net revenue is quantity*unit_price*(1-discount_pct); cost is quantity*unit_cost. "
            "Return one SQL statement only, without markdown or explanation. If the request cannot be answered "
            "from this schema or is materially ambiguous, return CLARIFY: followed by one concise question. "
            "Never produce writes, DDL, multiple statements, system-table access, or unapproved functions. "
            "Question: {question}\nRecent conversation context: {history}"
        ),
        expected_output="One SELECT statement or a CLARIFY: question.",
        agent=schema_agent,
    )
    result = Crew(agents=[schema_agent], tasks=[task], verbose=False, memory=False).kickoff(
        inputs={"question": question, "history": history_text or "none"}
    )
    return str(result)


def _explain_with_crew(question: str, sql: str, rows: list[dict[str, object]], history: list[dict[str, str]], data_label: str) -> str:
    from crewai import Agent, Crew, Task

    analyst = Agent(
        role="Business Data Analyst", goal="Explain the query results using only observed evidence",
        backstory="You distinguish values from hypotheses and explicitly mention scope and definitions.",
        llm=os.getenv("CREWAI_MODEL", "openai/gpt-4o-mini"), allow_delegation=False, max_iter=2, verbose=False,
    )
    editor = Agent(
        role="Executive Insight Editor", goal="Write a concise answer for a business user",
        backstory="You preserve caveats and never claim causation from correlation.",
        llm=os.getenv("CREWAI_MODEL", "openai/gpt-4o-mini"), allow_delegation=False, max_iter=2, verbose=False,
    )
    analysis = Task(
        description=(
            "Answer the user's question from the exact rows returned by NEXORA. Cite the date/region scope, "
            "describe SQL measures if clear, and separate evidence from hypotheses. Identify the source data as {data_label}. "
            "Question: {question}\nSQL: {sql}\nRows: {rows}\nRecent context: {history}"
        ),
        expected_output="Evidence-grounded analysis with relevant caveats.", agent=analyst,
    )
    response = Task(
        description="Turn the analyst's findings into a clear response in 2–5 sentences. Keep all measured values and caveats.",
        expected_output="A concise business answer, with no invented facts.", agent=editor, context=[analysis],
    )
    result = Crew(agents=[analyst, editor], tasks=[analysis, response], verbose=False, memory=False).kickoff(
        inputs={"question": question, "sql": sql, "rows": json.dumps(rows, ensure_ascii=False), "history": json.dumps(history[-6:], ensure_ascii=False), "data_label": data_label}
    )
    return str(result)


def ask_data_question(
    question: str,
    *,
    dataset_id: str | None = None,
    history: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    question = question.strip()
    if not question or len(question) > 2000:
        raise ValueError("Enter a question between 1 and 2,000 characters.")
    history = history or []
    if len(history) > 20 or any(len(item.get("content", "")) > 4000 for item in history):
        raise ValueError("Conversation context is too large; keep the last 20 messages under 4,000 characters each.")
    started = time.perf_counter()
    llm_mode = _llm_configured()
    if llm_mode:
        generated = _crew_generate_sql(question, dataset_id, history)
        if generated.strip().upper().startswith("CLARIFY:"):
            return {"clarification_required": True, "answer": generated.split(":", 1)[1].strip(), "dataset_id": dataset_id or "demo", "sql": None, "rows": []}
        sql = _strip_sql(generated)
        source = "CrewAI-generated SQL"
    else:
        sql = deterministic_sql(question, dataset_id=dataset_id)
        if not sql:
            return {
                "clarification_required": True,
                "answer": "I can answer questions about revenue, cost, profit, margins, orders, customers, products, regions, categories, and segments. Add a date or grouping to narrow the question.",
                "dataset_id": dataset_id or "demo", "sql": None, "rows": [], "source": "deterministic question router",
            }
        source = "deterministic demo query"
    result = execute_readonly_sql(sql, dataset_id=dataset_id)
    metadata = dataset_metadata(dataset_id)
    data_label = metadata["label"]
    if llm_mode:
        answer = _explain_with_crew(question, result["sql"], result["rows"], history, data_label)
    else:
        answer = f"The query returned {result['row_count']} result row(s) from {data_label}; inspect the rows and SQL below for the exact filters and calculations."
    return {
        "clarification_required": False, "answer": answer, "dataset_id": result["dataset_id"],
        "sql": result["sql"], "rows": result["rows"], "columns": result["columns"],
        "row_count": result["row_count"], "truncated": result["truncated"],
        "latency_ms": round((time.perf_counter() - started) * 1000, 2), "source": source,
        "data_label": data_label,
    }
