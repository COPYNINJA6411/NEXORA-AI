"""Run a small, reproducible 40-question text-to-SQL demo evaluation."""

from __future__ import annotations

import json
import time
from typing import Any

from app.agents.query_assistant import deterministic_sql
from app.services.analytics import execute_readonly_sql
from app.database.connection import make_database_engine
from app.sql_policy import validate_select


def _gold(metric: str, group: str | None = None) -> str:
    joins = "JOIN order_items oi ON oi.order_id = o.id JOIN products p ON p.id = oi.product_id"
    exprs = {
        "revenue": "SUM(oi.quantity * oi.unit_price * (1 - oi.discount_pct))",
        "cost": "SUM(oi.quantity * p.unit_cost)",
        "profit": "SUM(oi.quantity * oi.unit_price * (1 - oi.discount_pct)) - SUM(oi.quantity * p.unit_cost)",
        "orders": "COUNT(DISTINCT o.id)",
    }
    if group == "region":
        joins = "JOIN customers c ON c.id = o.customer_id JOIN regions r ON r.id = c.region_id " + "JOIN order_items oi ON oi.order_id = o.id JOIN products p ON p.id = oi.product_id"
    elif group == "segment":
        joins = "JOIN customers c ON c.id = o.customer_id " + joins
    if group == "month":
        engine = make_database_engine(read_only=True)
        try:
            field = "DATE_TRUNC('month', o.order_date)" if engine.dialect.name == "postgresql" else "strftime('%Y-%m', o.order_date)"
        finally:
            engine.dispose()
    else:
        field = {"product": "p.name", "category": "p.category", "segment": "c.segment", "region": "r.name"}.get(group or "")
    select_group = f", {field} AS {group}" if field else ""
    group_by = f" GROUP BY {field} ORDER BY {metric} DESC" if field else ""
    return (f"SELECT {exprs[metric]} AS {metric}{select_group} FROM orders o {joins} "
            f"WHERE o.status='Completed' AND o.order_date >= '2025-01-01' AND o.order_date < '2026-01-01'{group_by}")


CASES = [
    ("revenue", None, ["total revenue in 2025", "total sales in 2025", "revenue for 2025", "sales for 2025", "how much revenue in 2025"]),
    ("revenue", "region", ["revenue by region in 2025", "sales by region in 2025", "compare regional revenue for 2025", "show revenue per region in 2025", "regional sales in 2025"]),
    ("revenue", "product", ["top products by revenue in 2025", "sales by product in 2025", "product revenue for 2025", "revenue per product in 2025", "which products had the most sales in 2025"]),
    ("cost", "category", ["cost by category in 2025", "product category costs in 2025", "show cost per category for 2025", "costs by categories in 2025", "how much did each category cost in 2025"]),
    ("profit", "segment", ["profit by segment in 2025", "customer segment profit in 2025", "profit per segment for 2025", "compare segment profit in 2025", "which segments generated profit in 2025"]),
    ("orders", "segment", ["order count by segment in 2025", "number of orders by segment in 2025", "orders by segment during 2025", "compare order counts per segment in 2025", "which segments placed the most orders in 2025"]),
    ("revenue", "month", ["monthly revenue trend in 2025", "revenue month by month in 2025", "monthly sales in 2025", "revenue over time in 2025", "show monthly sales trend for 2025"]),
    ("profit", "region", ["profit by region in 2025", "regional profit in 2025", "profit for each region in 2025", "compare regions by profit in 2025", "which regions earned the most profit in 2025"]),
]


def _rows_equal(actual: list[dict[str, Any]], expected: list[dict[str, Any]]) -> bool:
    if len(actual) != len(expected):
        return False
    for left, right in zip(actual, expected):
        if left.keys() != right.keys():
            return False
        for key in left:
            a, b = left[key], right[key]
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                if abs(float(a) - float(b)) > max(0.01, abs(float(b)) * 1e-9):
                    return False
            elif a != b:
                return False
    return True


def evaluate() -> dict[str, Any]:
    results = []
    durations = []
    for metric, group, questions in CASES:
        expected = execute_readonly_sql(_gold(metric, group))
        for question in questions:
            started = time.perf_counter()
            try:
                sql = deterministic_sql(question)
                if not sql:
                    raise ValueError("Question was not routed")
                sql = validate_select(sql)
                actual = execute_readonly_sql(sql)
                durations.append((time.perf_counter() - started) * 1000)
                results.append({"question": question, "sql_valid": True, "executed": True,
                                "correct": _rows_equal(actual["rows"], expected["rows"])})
            except Exception as exc:
                durations.append((time.perf_counter() - started) * 1000)
                results.append({"question": question, "sql_valid": False, "executed": False,
                                "correct": False, "error": str(exc)})
    total = len(results)
    valid = sum(bool(item["sql_valid"]) for item in results)
    executed = sum(bool(item["executed"]) for item in results)
    correct = sum(bool(item["correct"]) for item in results)
    return {
        "dataset_id": "demo", "questions": total,
        "sql_validation_accuracy": round(valid / total, 3),
        "execution_rate": round(executed / total, 3),
        "result_accuracy": round(correct / total, 3),
        "mean_latency_ms": round(sum(durations) / len(durations), 2),
        "cases": results,
    }


if __name__ == "__main__":
    print(json.dumps(evaluate(), indent=2))
