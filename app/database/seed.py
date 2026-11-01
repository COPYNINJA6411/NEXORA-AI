"""Deterministic generator and loader for the clearly labeled demo dataset."""

from __future__ import annotations

import argparse
import random
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import delete, func, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.database.connection import make_database_engine
from app.database.models import (
    Base, Customer, DatasetInfo, Employee, MarketingExpense, Order, OrderItem,
    Product, Region, SalesTarget, SyntheticEvent,
)

DEMO_DATASET_ID = "demo"
DEMO_DATASET_LABEL = "Synthetic enterprise demo data"
DEMO_SEED = 20261102
DEMO_DESCRIPTION = (
    "Reproducible synthetic records for development and evaluation; includes labeled West-region "
        "sales, spend, product, and customer-burst events; not real customer, employee, sales, or marketing data."
)
REGION_NAMES = ("North", "South", "East", "West", "Central", "Northeast", "Southeast", "Pacific")
SEGMENTS = ("Enterprise", "Mid-Market", "Small Business")
PRODUCT_CATALOG = (
    ("Analytics Suite", "Software", 1299), ("Cloud Platform", "Software", 899),
    ("Security Stack", "Software", 1099), ("Payments Hub", "Services", 649),
    ("Advisory Services", "Services", 2250), ("Data Connector", "Integration", 399),
    ("Workflow Pro", "Software", 749), ("Customer Insights", "Analytics", 999),
)
CHANNELS = ("Search", "Events", "Partner", "Social")


def migrate_schema(engine: Engine) -> None:
    """Create missing objects and add the additive product-cost column to older demo files."""
    Base.metadata.create_all(engine)
    product_columns = {column["name"] for column in inspect(engine).get_columns("products")}
    if "unit_cost" not in product_columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE products ADD COLUMN unit_cost NUMERIC(12, 2)"))
            connection.execute(text("UPDATE products SET unit_cost = ROUND(unit_price * 0.62, 2) WHERE unit_cost IS NULL"))
    event_columns = {column["name"] for column in inspect(engine).get_columns("synthetic_events")}
    if event_columns and "customer_id" not in event_columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE synthetic_events ADD COLUMN customer_id INTEGER REFERENCES customers(id)"))


def _month_start(year: int, month: int) -> date:
    return date(year, month, 1)


def seed_demo_dataset(engine: Engine, *, reset: bool = False, seed: int = DEMO_SEED) -> dict[str, int | str]:
    """Create schema and seed one deterministic demo dataset, safely idempotent by default."""
    tables_before = set(inspect(engine).get_table_names())
    if "dataset_info" in tables_before:
        with Session(engine) as session:
            prior_info = session.get(DatasetInfo, DEMO_DATASET_ID)
        if prior_info is not None and not prior_info.is_synthetic:
            raise ValueError("Refusing to seed or reset a dataset labeled as non-synthetic.")
        if reset and prior_info is None:
            raise ValueError("Refusing to reset a database without synthetic dataset metadata.")
    elif reset and tables_before:
        raise ValueError("Refusing to reset a non-empty database without synthetic dataset metadata.")
    migrate_schema(engine)
    rng = random.Random(seed)

    with Session(engine) as session, session.begin():
        existing = session.get(DatasetInfo, DEMO_DATASET_ID)
        if existing and not reset:
            return {"dataset_id": DEMO_DATASET_ID, "status": "already_seeded"}
        if reset:
            # Delete leaf tables first so this works consistently across SQLite/PostgreSQL.
            for model in (SyntheticEvent, OrderItem, Order, Customer, Employee, Product, SalesTarget, MarketingExpense, Region, DatasetInfo):
                session.execute(delete(model))

        session.add_all([
            Region(id=i + 1, name=name) for i, name in enumerate(REGION_NAMES)
        ])
        session.flush()

        customer_rows = []
        for i in range(1, 501):
            customer_rows.append({
                "id": i,
                "customer_code": f"CUS-{i:05d}",
                "name": f"Demo Customer {i:04d}",
                "segment": rng.choice(SEGMENTS),
                "region_id": rng.randint(1, len(REGION_NAMES)),
                "created_at": date(2022, 1, 1) + timedelta(days=rng.randrange(0, 1500)),
            })
        session.execute(Customer.__table__.insert(), customer_rows)

        product_rows = [
            {"id": i + 1, "sku": f"SKU-{i + 1:04d}", "name": name, "category": category,
             "unit_price": Decimal(str(price)), "unit_cost": Decimal(str(round(price * rng.uniform(0.38, 0.68), 2)))}
            for i, (name, category, price) in enumerate(PRODUCT_CATALOG)
        ]
        session.execute(Product.__table__.insert(), product_rows)

        employee_rows = [
            {"id": i, "employee_code": f"EMP-{i:04d}", "name": f"Demo Employee {i:03d}",
             "title": rng.choice(("Account Executive", "Sales Manager", "Account Manager")),
             "region_id": (i - 1) % len(REGION_NAMES) + 1}
            for i in range(1, 41)
        ]
        session.execute(Employee.__table__.insert(), employee_rows)

        order_rows = []
        line_rows = []
        first_day = date(2024, 1, 1)
        day_span = (date(2026, 10, 31) - first_day).days + 1
        for order_id in range(1, 5001):
            customer_id = rng.randint(1, 500)
            order_date = first_day + timedelta(days=rng.randrange(day_span))
            status = rng.choices(("Completed", "Processing", "Cancelled", "Returned"), (88, 6, 4, 2))[0]
            customer_region_id = customer_rows[customer_id - 1]["region_id"]
            if customer_region_id == 4 and order_date.year == 2025 and order_date.month == 6:
                status = rng.choices(("Completed", "Processing", "Cancelled", "Returned"), (12, 3, 80, 5))[0]
            order_rows.append({
                "id": order_id,
                "order_number": f"ORD-{order_id:07d}",
                "customer_id": customer_id,
                "employee_id": rng.randint(1, 40),
                "order_date": order_date,
                "status": status,
            })
            line_count = rng.randint(1, 4)
            chosen_products = rng.sample(range(1, len(PRODUCT_CATALOG) + 1), line_count)
            for line_number, product_id in enumerate(chosen_products, start=1):
                base_price = PRODUCT_CATALOG[product_id - 1][2]
                quantity = rng.randint(1, 8)
                if product_id == 1 and order_date.year == 2025 and order_date.month == 8:
                    quantity *= 4
                line_rows.append({
                    "order_id": order_id,
                    "line_number": line_number,
                    "product_id": product_id,
                    "quantity": quantity,
                    "unit_price": Decimal(str(base_price)),
                    "discount_pct": Decimal(str(rng.choice((0, 0, 0, 0.05, 0.1, 0.15)))),
                })
        session.execute(Order.__table__.insert(), order_rows)
        session.execute(OrderItem.__table__.insert(), line_rows)

        customer_region = customer_rows[41]["region_id"]
        burst_orders = []
        burst_items = []
        for offset, order_id in enumerate(range(5001, 5016)):
            product_id = rng.randint(1, len(PRODUCT_CATALOG))
            burst_orders.append({
                "id": order_id, "order_number": f"ORD-{order_id:07d}", "customer_id": 42,
                "employee_id": rng.randint(1, 40), "order_date": date(2025, 9, 1) + timedelta(days=offset),
                "status": "Completed",
            })
            burst_items.append({
                "order_id": order_id, "line_number": 1, "product_id": product_id,
                "quantity": rng.randint(1, 5), "unit_price": Decimal(str(PRODUCT_CATALOG[product_id - 1][2])),
                "discount_pct": Decimal("0"),
            })
        session.execute(Order.__table__.insert(), burst_orders)
        session.execute(OrderItem.__table__.insert(), burst_items)

        target_rows = []
        marketing_rows = []
        for year in (2024, 2025, 2026):
            for month in range(1, 13 if year < 2026 else 11):
                month_date = _month_start(year, month)
                for region_id in range(1, len(REGION_NAMES) + 1):
                    revenue_target = Decimal(str(rng.randrange(180_000, 420_001, 1000)))
                    target_rows.append({
                        "region_id": region_id, "month_start": month_date,
                        "revenue_target": revenue_target,
                        "profit_target": (revenue_target * Decimal("0.28")).quantize(Decimal("0.01")),
                    })
                    for channel in CHANNELS:
                        amount = rng.randrange(4_000, 35_001, 100)
                        if region_id == 4 and year == 2025 and month == 9 and channel == "Social":
                            amount = 150_000
                        marketing_rows.append({
                            "region_id": region_id, "month_start": month_date, "channel": channel,
                            "amount": Decimal(str(amount)),
                        })
        session.execute(SalesTarget.__table__.insert(), target_rows)
        session.execute(MarketingExpense.__table__.insert(), marketing_rows)
        session.execute(SyntheticEvent.__table__.insert(), [
            {
                "event_type": "revenue_drop", "metric": "completed_sales_revenue",
                "period_start": date(2025, 6, 1), "period_end": date(2025, 6, 30),
                "region_id": 4, "product_id": None,
                "customer_id": None,
                "description": "Injected increase in cancellations for West-region orders during June 2025.",
            },
            {
                "event_type": "expense_spike", "metric": "marketing_expense",
                "period_start": date(2025, 9, 1), "period_end": date(2025, 9, 30),
                "region_id": 4, "product_id": None,
                "customer_id": None,
                "description": "Injected sixfold Social marketing expense for West in September 2025.",
            },
            {
                "event_type": "product_surge", "metric": "product_revenue",
                "period_start": date(2025, 8, 1), "period_end": date(2025, 8, 31),
                "region_id": None, "product_id": 1, "customer_id": None,
                "description": "Injected fourfold quantity for Analytics Suite line items during August 2025.",
            },
            {
                "event_type": "customer_burst", "metric": "customer_order_count",
                "period_start": date(2025, 9, 1), "period_end": date(2025, 9, 30),
                "region_id": customer_region, "product_id": None, "customer_id": 42,
                "description": "Injected fifteen completed orders for one synthetic customer during September 2025.",
            },
        ])
        session.add(DatasetInfo(
            dataset_id=DEMO_DATASET_ID,
            label=DEMO_DATASET_LABEL,
            is_synthetic=True,
            random_seed=seed,
            description=DEMO_DESCRIPTION,
            generated_at=datetime.now(timezone.utc).replace(tzinfo=None),
        ))

    with Session(engine) as session:
        counts: dict[str, int | str] = {"dataset_id": DEMO_DATASET_ID, "status": "seeded"}
        for table_name, model in (
            ("regions", Region), ("customers", Customer), ("products", Product),
            ("employees", Employee), ("orders", Order), ("order_items", OrderItem),
            ("sales_targets", SalesTarget), ("marketing_expenses", MarketingExpense),
            ("synthetic_events", SyntheticEvent),
        ):
            counts[table_name] = int(session.scalar(select(func.count()).select_from(model)) or 0)
        return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed NEXORA's reproducible synthetic enterprise demo dataset.")
    parser.add_argument("--reset", action="store_true", help="replace the existing demo rows")
    parser.add_argument("--seed", type=int, default=DEMO_SEED, help="random seed (default: %(default)s)")
    args = parser.parse_args()
    engine = make_database_engine(dataset_id=DEMO_DATASET_ID)
    try:
        result = seed_demo_dataset(engine, reset=args.reset, seed=args.seed)
        for key, value in result.items():
            print(f"{key}: {value}")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
