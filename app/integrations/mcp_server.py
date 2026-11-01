"""MCP adapter exposing selected NEXORA analytics use cases as tools/resources."""

from datetime import date
import json

from mcp.server.fastmcp import FastMCP

from app.services.analytics import describe_schema, get_sales_summary, list_datasets

mcp_server = FastMCP(
    "NEXORA AI Analytics",
    instructions=(
        "Use NEXORA's read-only analytics tools for the selected dataset. "
        "State the dataset label, date range, units, and region filters when reporting metrics. "
        "Do not present synthetic data as real company results."
    ),
)


@mcp_server.tool()
def list_available_datasets() -> str:
    """List configured datasets and their synthetic-data labels."""
    return json.dumps(list_datasets(), ensure_ascii=False)


@mcp_server.tool()
def inspect_business_schema(dataset_id: str = "demo") -> str:
    """Describe available business tables, columns, and foreign-key relationships."""
    return json.dumps(describe_schema(dataset_id), ensure_ascii=False)


@mcp_server.tool()
def summarize_sales(start_date: date, end_date: date, region: str | None = None, dataset_id: str = "demo") -> str:
    """Return completed-order revenue and order count for an inclusive date range; optionally filter by region."""
    return json.dumps(get_sales_summary(start_date, end_date, region, dataset_id=dataset_id), ensure_ascii=False)
