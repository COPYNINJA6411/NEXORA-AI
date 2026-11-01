"""Read-only analytics and database-management services shared by all adapters."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import math
import os
import time
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import MetaData, Table, distinct, func, inspect, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.database.connection import configured_datasets, make_database_engine
from app.database.models import (
    Base, Customer, DatasetInfo, Employee, MarketingExpense, Order, OrderItem,
    Product, Region, SalesTarget, SyntheticEvent,
)
from app.sql_policy import MAX_RESULT_ROWS, schema_allowlist, validate_select


def _json_value(value: Any) -> Any:
    if isinstance(value, (date,)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def list_datasets() -> dict[str, object]:
    configured = configured_datasets()
    result = []
    for dataset_id in configured:
        engine = make_database_engine(dataset_id=dataset_id, read_only=True)
        try:
            with Session(engine) as session:
                info = session.scalar(select(DatasetInfo).order_by(DatasetInfo.dataset_id).limit(1))
                if info is None:
                    result.append({"id": dataset_id, "label": dataset_id, "is_synthetic": False, "available": False})
                else:
                    result.append({
                        "id": dataset_id,
                        "label": info.label if dataset_id == "demo" else f"{info.label} ({dataset_id})",
                        "is_synthetic": bool(info.is_synthetic),
                        "available": True,
                        "description": info.description,
                    })
        except SQLAlchemyError:
            result.append({"id": dataset_id, "label": dataset_id, "is_synthetic": False, "available": False})
        finally:
            engine.dispose()
    requested = os.getenv("NEXORA_DATASET", "demo")
    selected = requested if requested in configured else ("demo" if "demo" in configured else next(iter(configured)))
    return {"selected_dataset": selected, "database_available": any(item["available"] for item in result), "datasets": result}


def dataset_metadata(dataset_id: str | None = None) -> dict[str, object]:
    """Return a safe display label and synthetic-data flag for the selected database."""
    selected = dataset_id or "demo"
    engine = make_database_engine(dataset_id=selected, read_only=True)
    try:
        with Session(engine) as session:
            info = session.scalar(select(DatasetInfo).limit(1))
        if info is not None:
            return {"dataset_id": selected, "label": info.label, "is_synthetic": bool(info.is_synthetic),
                    "description": info.description}
    except SQLAlchemyError:
        pass
    finally:
        engine.dispose()
    return {"dataset_id": selected, "label": selected, "is_synthetic": False, "description": "Configured dataset"}


def describe_schema(dataset_id: str | None = None) -> dict[str, object]:
    engine = make_database_engine(dataset_id=dataset_id, read_only=True)
    try:
        inspector = inspect(engine)
        allowlist = schema_allowlist(dataset_id)
        tables = []
        for name in sorted(allowlist):
            columns = inspector.get_columns(name)
            tables.append({
                "name": name,
                "columns": [{"name": c["name"], "type": str(c["type"]), "nullable": c["nullable"]} for c in columns],
                "primary_key": inspector.get_pk_constraint(name).get("constrained_columns", []),
                "foreign_keys": [
                    {"columns": fk.get("constrained_columns", []), "references_table": fk.get("referred_table"),
                     "references_columns": fk.get("referred_columns", [])}
                    for fk in inspector.get_foreign_keys(name)
                ],
            })
        metadata = dataset_metadata(dataset_id)
        return {"dataset_id": dataset_id or "demo", "tables": tables, "data_label": metadata["label"], "is_synthetic": metadata["is_synthetic"]}
    finally:
        engine.dispose()


def database_overview(dataset_id: str | None = None) -> dict[str, object]:
    engine = make_database_engine(dataset_id=dataset_id, read_only=True)
    try:
        allowed = set(schema_allowlist(dataset_id))
        inspector = inspect(engine)
        tables = []
        for table_name in sorted(allowed):
            tables.append({
                "name": table_name,
                "row_count": int(_count_rows(engine, table_name)),
                "columns": [{"name": c["name"], "type": str(c["type"])} for c in inspector.get_columns(table_name)],
            })
        return {"dataset_id": dataset_id or "demo", "tables": tables}
    finally:
        engine.dispose()


def _count_rows(engine, table_name: str) -> int:
    with engine.connect() as connection:
        return int(connection.execute(text(f'SELECT COUNT(*) FROM "{table_name}"')).scalar_one())


def preview_table(table_name: str, *, dataset_id: str | None = None, limit: int = 50) -> dict[str, object]:
    if not 1 <= limit <= 100:
        raise ValueError("Preview row limit must be between 1 and 100.")
    allowlist = schema_allowlist(dataset_id)
    if table_name not in allowlist:
        raise ValueError("That table is not available for preview.")
    engine = make_database_engine(dataset_id=dataset_id, read_only=True)
    try:
        table = Table(table_name, MetaData(), autoload_with=engine)
        with engine.connect() as connection:
            rows = connection.execute(select(table).limit(limit)).mappings().all()
        return {
            "table": table_name,
            "columns": [{"name": column.name, "type": str(column.type)} for column in table.columns],
            "rows": [{key: _json_value(value) for key, value in row.items()} for row in rows],
            "row_count_returned": len(rows),
        }
    finally:
        engine.dispose()


def execute_readonly_sql(sql: str, *, dataset_id: str | None = None, timeout_seconds: int = 5) -> dict[str, object]:
    if not 1 <= timeout_seconds <= 15:
        raise ValueError("Query timeout must be between 1 and 15 seconds.")
    safe_sql = validate_select(sql, dataset_id=dataset_id, row_limit=MAX_RESULT_ROWS)
    engine = make_database_engine(dataset_id=dataset_id, read_only=True)
    started = time.perf_counter()
    try:
        with engine.connect() as connection:
            with connection.begin():
                sqlite_raw = None
                if engine.dialect.name == "sqlite":
                    sqlite_raw = connection.connection.driver_connection
                    sqlite_raw.execute("PRAGMA query_only=ON")
                    deadline = time.monotonic() + timeout_seconds
                    sqlite_raw.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1000)
                else:
                    connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                    connection.exec_driver_sql(f"SET LOCAL statement_timeout = '{timeout_seconds}s'")
                try:
                    result = connection.execute(text(safe_sql))
                    columns = list(result.keys())
                    rows = result.fetchmany(MAX_RESULT_ROWS + 1)
                    truncated = len(rows) > MAX_RESULT_ROWS
                    rows = rows[:MAX_RESULT_ROWS]
                finally:
                    if sqlite_raw is not None:
                        sqlite_raw.set_progress_handler(None, 0)
                        sqlite_raw.execute("PRAGMA query_only=OFF")
        return {
            "sql": safe_sql,
            "columns": columns,
            "rows": [{key: _json_value(value) for key, value in row._mapping.items()} for row in rows],
            "row_count": len(rows),
            "truncated": truncated,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "dataset_id": dataset_id or "demo",
        }
    except SQLAlchemyError as exc:
        message = str(exc).lower()
        if "interrupted" in message or "statement timeout" in message or "canceling statement" in message:
            raise TimeoutError(f"Query exceeded the {timeout_seconds}-second execution limit.") from exc
        raise
    finally:
        engine.dispose()


def _sales_rows(
    start_date: date,
    end_date: date,
    *,
    region: str | None = None,
    category: str | None = None,
    segment: str | None = None,
    dataset_id: str | None = None,
) -> pd.DataFrame:
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")
    if (end_date - start_date).days > 3660:
        raise ValueError("date range must be ten years or less")
    revenue = OrderItem.quantity * OrderItem.unit_price * (Decimal("1") - OrderItem.discount_pct)
    cost = OrderItem.quantity * Product.unit_cost
    query = (
        select(
            Order.id.label("order_id"), Customer.id.label("customer_id"),
            Order.order_date.label("order_date"), Region.name.label("region"),
            Customer.segment.label("segment"), Product.id.label("product_id"),
            Product.name.label("product"), Product.category.label("category"),
            revenue.label("revenue"), cost.label("cost"),
        )
        .select_from(Order)
        .join(Customer, Customer.id == Order.customer_id)
        .join(Region, Region.id == Customer.region_id)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .join(Product, Product.id == OrderItem.product_id)
        .where(Order.status == "Completed", Order.order_date >= start_date, Order.order_date <= end_date)
    )
    if region:
        query = query.where(Region.name == region)
    if category:
        query = query.where(Product.category == category)
    if segment:
        query = query.where(Customer.segment == segment)
    engine = make_database_engine(dataset_id=dataset_id, read_only=True)
    try:
        with engine.connect() as connection:
            with connection.begin():
                if engine.dialect.name == "sqlite":
                    connection.exec_driver_sql("PRAGMA query_only=ON")
                else:
                    connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                    connection.exec_driver_sql("SET LOCAL statement_timeout = '10s'")
                result = connection.execute(query).mappings().all()
        return pd.DataFrame([dict(row) for row in result])
    finally:
        engine.dispose()


def get_sales_summary(
    start_date: date,
    end_date: date,
    region: str | None = None,
    *, dataset_id: str | None = None,
) -> dict[str, object]:
    frame = _sales_rows(start_date, end_date, region=region, dataset_id=dataset_id)
    revenue = float(frame["revenue"].sum()) if not frame.empty else 0.0
    cost = float(frame["cost"].sum()) if not frame.empty else 0.0
    profit = revenue - cost
    order_count = int(frame["order_id"].nunique()) if not frame.empty else 0
    customer_count = int(frame["customer_id"].nunique()) if not frame.empty else 0
    metadata = dataset_metadata(dataset_id)
    unit = "synthetic demo currency units" if metadata["is_synthetic"] else "dataset currency units"
    return {
        "dataset_id": dataset_id or "demo", "metric": "completed_order_sales",
        "start_date": start_date.isoformat(), "end_date": end_date.isoformat(), "region": region,
        "completed_orders": order_count, "customers": customer_count,
        "revenue": round(revenue, 2), "cost": round(cost, 2), "profit": round(profit, 2),
        "profit_margin_pct": round(profit / revenue * 100, 2) if revenue else 0.0,
        "average_order_value": round(revenue / order_count, 2) if order_count else 0.0,
        "unit": unit,
        "data_label": metadata["label"],
        "definition": "Sales are completed orders only; net line revenue is quantity × unit price × (1 − line discount); cost is quantity × product unit cost.",
    }


def dashboard_data(
    start_date: date,
    end_date: date,
    *, region: str | None = None,
    category: str | None = None,
    segment: str | None = None,
    dataset_id: str | None = None,
) -> dict[str, object]:
    days = (end_date - start_date).days + 1
    if days <= 0 or days > 3660:
        raise ValueError("Select a valid date range of ten years or less.")
    prev_start = start_date - timedelta(days=days)
    frame = _sales_rows(prev_start, end_date, region=region, category=category, segment=segment, dataset_id=dataset_id)
    metadata = dataset_metadata(dataset_id)
    if frame.empty:
        return {"dataset_id": dataset_id or "demo", "synthetic": metadata["is_synthetic"], "data_label": metadata["label"], "kpis": {"revenue": 0, "profit": 0, "profit_margin_pct": 0, "customers": 0, "average_order_value": 0, "revenue_growth_pct": 0}, "monthly": [], "regions": [], "products": [], "segments": [], "customers": []}
    frame["order_date"] = pd.to_datetime(frame["order_date"])
    frame["profit"] = frame["revenue"].astype(float) - frame["cost"].astype(float)
    current = frame[(frame.order_date.dt.date >= start_date) & (frame.order_date.dt.date <= end_date)]
    previous = frame[(frame.order_date.dt.date >= prev_start) & (frame.order_date.dt.date < start_date)]
    revenue = float(current.revenue.sum())
    cost_total = float(current.cost.sum())
    profit = revenue - cost_total
    previous_revenue = float(previous.revenue.sum())
    monthly_df = current.groupby(current.order_date.dt.to_period("M")).agg(revenue=("revenue", "sum"), cost=("cost", "sum"), profit=("profit", "sum"), orders=("order_id", "nunique")).reset_index()
    monthly_df["month"] = monthly_df.order_date.astype(str)
    def grouped(column: str, limit: int = 12) -> list[dict[str, object]]:
        values = current.groupby(column).agg(revenue=("revenue", "sum"), cost=("cost", "sum"), orders=("order_id", "nunique"), customers=("customer_id", "nunique")).reset_index()
        values["profit"] = values.revenue - values.cost
        return [{column: str(row[column]), "revenue": round(float(row.revenue), 2), "cost": round(float(row.cost), 2), "profit": round(float(row.profit), 2), "orders": int(row.orders), "customers": int(row.customers)} for _, row in values.sort_values("revenue", ascending=False).head(limit).iterrows()]
    kpis = {
        "revenue": round(revenue, 2), "cost": round(cost_total, 2), "profit": round(profit, 2),
        "profit_margin_pct": round(profit / revenue * 100, 2) if revenue else 0.0,
        "customers": int(current.customer_id.nunique()), "orders": int(current.order_id.nunique()),
        "average_order_value": round(revenue / current.order_id.nunique(), 2) if current.order_id.nunique() else 0.0,
        "revenue_growth_pct": round((revenue / previous_revenue - 1) * 100, 2) if previous_revenue else 0.0,
        "previous_period_revenue": round(previous_revenue, 2),
    }
    return {
        "dataset_id": dataset_id or "demo", "synthetic": metadata["is_synthetic"], "data_label": metadata["label"],
        "filters": {"start_date": start_date.isoformat(), "end_date": end_date.isoformat(), "region": region, "category": category, "segment": segment},
        "kpis": kpis,
        "monthly": [{"month": row.month, "revenue": round(float(row.revenue), 2), "cost": round(float(row.cost), 2), "profit": round(float(row.profit), 2), "orders": int(row.orders)} for row in monthly_df.itertuples()],
        "regions": grouped("region"), "products": grouped("product"), "segments": grouped("segment"), "customers": grouped("customer_id", 10),
    }


def dashboard_filter_options(dataset_id: str | None = None) -> dict[str, list[str]]:
    engine = make_database_engine(dataset_id=dataset_id, read_only=True)
    try:
        with Session(engine) as session:
            return {
                "regions": list(session.scalars(select(Region.name).order_by(Region.name))),
                "categories": list(session.scalars(select(Product.category).distinct().order_by(Product.category))),
                "segments": list(session.scalars(select(Customer.segment).distinct().order_by(Customer.segment))),
            }
    finally:
        engine.dispose()


def _monthly_region_revenue(dataset_id: str | None = None) -> pd.DataFrame:
    revenue = OrderItem.quantity * OrderItem.unit_price * (Decimal("1") - OrderItem.discount_pct)
    query = (
        select(Order.order_date, Region.name.label("region"), revenue.label("revenue"))
        .select_from(Order).join(Customer, Customer.id == Order.customer_id)
        .join(Region, Region.id == Customer.region_id).join(OrderItem, OrderItem.order_id == Order.id)
        .where(Order.status == "Completed")
    )
    engine = make_database_engine(dataset_id=dataset_id, read_only=True)
    try:
        with engine.connect() as connection:
            rows = connection.execute(query).mappings().all()
        frame = pd.DataFrame([dict(row) for row in rows])
        if frame.empty:
            return frame
        frame["month"] = pd.to_datetime(frame.order_date).dt.to_period("M").astype(str)
        return frame.groupby(["month", "region"], as_index=False).revenue.sum()
    finally:
        engine.dispose()


def _product_monthly_revenue(dataset_id: str | None = None) -> pd.DataFrame:
    revenue = OrderItem.quantity * OrderItem.unit_price * (Decimal("1") - OrderItem.discount_pct)
    query = (
        select(Order.order_date, Product.id.label("product_id"), Product.name.label("product"), revenue.label("revenue"))
        .select_from(Order).join(OrderItem, OrderItem.order_id == Order.id)
        .join(Product, Product.id == OrderItem.product_id).where(Order.status == "Completed")
    )
    engine = make_database_engine(dataset_id=dataset_id, read_only=True)
    try:
        with engine.connect() as connection:
            rows = connection.execute(query).mappings().all()
        frame = pd.DataFrame([dict(row) for row in rows])
        if frame.empty:
            return frame
        frame["month"] = pd.to_datetime(frame.order_date).dt.to_period("M").astype(str)
        return frame.groupby(["month", "product_id", "product"], as_index=False).revenue.sum()
    finally:
        engine.dispose()


def detect_anomalies(dataset_id: str | None = None) -> dict[str, object]:
    series = _monthly_region_revenue(dataset_id)
    detected: list[dict[str, object]] = []
    if not series.empty:
        series["month_date"] = pd.to_datetime(series.month + "-01")
        for region_name, group in series.groupby("region"):
            group = group.sort_values("month_date").reset_index(drop=True)
            for index, row in group.iterrows():
                baseline = group.iloc[max(0, index - 6):index].revenue
                if len(baseline) < 3:
                    continue
                median = float(baseline.median())
                actual = float(row.revenue)
                deviation = (actual / median - 1) if median else 0
                if deviation <= -0.65 or deviation >= 1.50:
                    detected.append({
                        "metric": "completed_sales_revenue", "region": region_name, "period": row.month,
                        "actual": round(actual, 2), "baseline_median": round(median, 2),
                        "change_pct": round(deviation * 100, 2), "method": "six-month trailing median; −65%/+150% threshold",
                        "kind": "observed anomaly", "hypothesis": "Review order volume, cancellations, and product mix for this period.",
                    })
    engine = make_database_engine(dataset_id=dataset_id, read_only=True)
    try:
        with Session(engine) as session:
            expenses = session.execute(
                select(MarketingExpense.month_start, Region.name, MarketingExpense.channel, MarketingExpense.amount)
                .join(Region, Region.id == MarketingExpense.region_id)
                .order_by(Region.name, MarketingExpense.channel, MarketingExpense.month_start)
            ).all()
            labeled_events = session.scalars(select(SyntheticEvent)).all()
            region_names = {region.id: region.name for region in session.scalars(select(Region)).all()}
            labels = [{"metric": event.metric, "region": region_names.get(event.region_id),
                       "product_id": event.product_id, "customer_id": event.customer_id,
                       "period": event.period_start.strftime("%Y-%m"), "event_type": event.event_type,
                       "description": event.description}
                      for event in labeled_events]
        expense_frame = pd.DataFrame([{"month": row.month_start.strftime("%Y-%m"), "region": row.name, "channel": row.channel, "amount": float(row.amount)} for row in expenses])
        if not expense_frame.empty:
            for (region_name, channel), group in expense_frame.groupby(["region", "channel"]):
                group = group.sort_values("month").reset_index(drop=True)
                for index, row in group.iterrows():
                    baseline = group.iloc[max(0, index - 6):index].amount
                    if len(baseline) < 3:
                        continue
                    median = float(baseline.median())
                    if median and float(row.amount) / median >= 4.5:
                        detected.append({
                            "metric": "marketing_expense", "region": region_name, "period": row.month,
                            "channel": channel, "actual": round(float(row.amount), 2), "baseline_median": round(median, 2),
                            "change_pct": round((float(row.amount) / median - 1) * 100, 2),
                            "method": "six-month trailing median; 4.5× threshold", "kind": "observed anomaly",
                            "hypothesis": "Check campaign pacing, invoices, and one-time event costs.",
                        })
    finally:
        engine.dispose()
    product_series = _product_monthly_revenue(dataset_id)
    if not product_series.empty:
        product_series["month_date"] = pd.to_datetime(product_series.month + "-01")
        for product_id, group in product_series.groupby("product_id"):
            group = group.sort_values("month_date").reset_index(drop=True)
            product_name = str(group.iloc[0].product)
            for index, row in group.iterrows():
                baseline = group.iloc[max(0, index - 6):index].revenue
                if len(baseline) < 3:
                    continue
                median = float(baseline.median())
                actual = float(row.revenue)
                ratio = actual / median if median else 0
                if ratio >= 2.5 or (ratio and ratio <= 0.3):
                    detected.append({
                        "metric": "product_revenue", "region": None, "product_id": int(product_id), "product": product_name,
                        "period": row.month, "actual": round(actual, 2), "baseline_median": round(median, 2),
                        "change_pct": round((ratio - 1) * 100, 2), "method": "six-month product median; 2.5×/0.3× threshold",
                        "kind": "observed anomaly", "hypothesis": "Review availability, discounting, and one-off orders for this product.",
                    })

    engine = make_database_engine(dataset_id=dataset_id, read_only=True)
    try:
        with Session(engine) as session:
            customer_orders = session.execute(
                select(Order.order_date, Customer.id.label("customer_id"), Region.name.label("region"))
                .join(Customer, Customer.id == Order.customer_id).join(Region, Region.id == Customer.region_id)
                .where(Order.status == "Completed")
            ).all()
    finally:
        engine.dispose()
    if customer_orders:
        order_frame = pd.DataFrame([{"month": row.order_date.strftime("%Y-%m"), "customer_id": row.customer_id, "region": row.region} for row in customer_orders])
        counts = order_frame.groupby(["customer_id", "region", "month"]).size().rename("orders").reset_index()
        all_months = pd.period_range("2024-01", "2026-10", freq="M").astype(str).tolist()
        for customer_id, group in counts.groupby("customer_id"):
            region_name = str(group.iloc[0].region)
            series_map = dict(zip(group.month, group.orders))
            values = [int(series_map.get(month, 0)) for month in all_months]
            for index in range(6, len(all_months)):
                baseline = values[index - 6:index]
                actual = values[index]
                baseline_mean = sum(baseline) / len(baseline)
                if actual >= 8 and actual >= max(5, baseline_mean * 5):
                    detected.append({
                        "metric": "customer_order_count", "region": region_name, "customer_id": int(customer_id),
                        "period": all_months[index], "actual": actual, "baseline_mean": round(baseline_mean, 2),
                        "change_pct": round((actual / max(baseline_mean, 0.01) - 1) * 100, 2),
                        "method": "six-month customer order baseline; minimum 8 orders and 5× threshold",
                        "kind": "observed anomaly", "hypothesis": "Review bulk purchases, account changes, and possible order duplication.",
                    })

    detected_keys = {(item["metric"], item.get("region"), item["period"], item.get("product_id"), item.get("customer_id")) for item in detected}
    label_keys = {(item["metric"], item.get("region"), item["period"], item.get("product_id"), item.get("customer_id")) for item in labels}
    true_positive = len(detected_keys & label_keys)
    precision = (true_positive / len(detected_keys) if detected_keys else 0.0) if label_keys else None
    recall = true_positive / len(label_keys) if label_keys else None
    f1 = (2 * precision * recall / (precision + recall) if precision + recall else 0.0) if precision is not None and recall is not None else None
    return {
        "dataset_id": dataset_id or "demo", "anomalies": sorted(detected, key=lambda item: abs(item["change_pct"]), reverse=True),
        "synthetic_labels": labels, "evaluation": {"precision": round(precision, 3) if precision is not None else None,
            "recall": round(recall, 3) if recall is not None else None, "f1": round(f1, 3) if f1 is not None else None,
            "labeled_events": len(label_keys)},
        "note": "Anomaly flags are statistical signals. Explanations are review hypotheses, not proven causes.",
    }


def _monthly_sales(dataset_id: str | None = None) -> pd.DataFrame:
    frame = _monthly_region_revenue(dataset_id)
    if frame.empty:
        return pd.DataFrame(columns=["month", "revenue"])
    return frame.groupby("month", as_index=False).revenue.sum().sort_values("month")


def forecast_revenue(dataset_id: str | None = None, horizon: int = 3, holdout_months: int = 6) -> dict[str, object]:
    if not 1 <= horizon <= 18:
        raise ValueError("Forecast horizon must be between 1 and 18 months.")
    history = _monthly_sales(dataset_id)
    if len(history) < 12:
        raise ValueError("At least twelve months of revenue history are required to forecast.")
    values = history.revenue.astype(float).to_numpy()
    test_count = min(holdout_months, max(3, len(values) // 5))
    train = values[:-test_count]
    train_x = np.arange(len(train), dtype=float)
    test_x = np.arange(len(train), len(values), dtype=float)
    try:
        from sklearn.linear_model import LinearRegression

        model = LinearRegression().fit(train_x.reshape(-1, 1), train)
        validation = np.maximum(0, model.predict(test_x.reshape(-1, 1)))
        model_name = "scikit-learn LinearRegression"
    except ImportError:
        slope, intercept = np.polyfit(train_x, train, 1)
        validation = np.maximum(0, intercept + slope * test_x)
        model_name = "NumPy least-squares linear trend"
    actual = values[-test_count:]
    error = validation - actual
    nonzero = actual != 0
    metrics = {
        "mae": round(float(np.mean(np.abs(error))), 2),
        "rmse": round(float(np.sqrt(np.mean(error ** 2))), 2),
        "mape_pct": round(float(np.mean(np.abs(error[nonzero] / actual[nonzero])) * 100), 2) if np.any(nonzero) else None,
        "mape_excluded_zero_actuals": int(np.sum(~nonzero)),
        "holdout_months": test_count,
    }
    all_x = np.arange(len(values), dtype=float)
    try:
        from sklearn.linear_model import LinearRegression

        full_model = LinearRegression().fit(all_x.reshape(-1, 1), values)
        predict = lambda x: float(full_model.predict(np.array([[x]], dtype=float))[0])
        model_name = "scikit-learn LinearRegression"
    except ImportError:
        full_slope, full_intercept = np.polyfit(all_x, values, 1)
        predict = lambda x: float(full_intercept + full_slope * x)
        model_name = "NumPy least-squares linear trend"
    last_month = pd.Period(history.iloc[-1].month, freq="M")
    forecast = []
    for offset in range(1, horizon + 1):
        period = last_month + offset
        prediction = max(0.0, predict(len(values) + offset - 1))
        forecast.append({"month": str(period), "revenue": round(prediction, 2), "kind": "forecast"})
    actual_history = [{"month": str(row.month), "revenue": round(float(row.revenue), 2), "kind": "actual"} for row in history.itertuples()]
    return {
        "dataset_id": dataset_id or "demo", "method": model_name, "horizon_months": horizon,
        "history": actual_history, "forecast": forecast, "backtest": metrics,
        "limitations": "This baseline extrapolates one trend and does not model seasonality or external drivers.",
    }
