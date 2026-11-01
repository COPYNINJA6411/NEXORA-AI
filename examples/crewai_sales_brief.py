"""Tiny CrewAI client that uses NEXORA's MCP analytics tools."""

from __future__ import annotations

import argparse
import os
from datetime import date, timedelta

from dotenv import load_dotenv


def main() -> None:
    load_dotenv()
    try:
        from crewai import Agent, Crew, Task
        from crewai.mcp import MCPServerHTTP
        from crewai.mcp.filters import create_static_tool_filter
    except ImportError as exc:
        raise SystemExit('Install the MCP client integration with: python -m pip install -e ".[mcp]"') from exc

    end_date = min(date.today(), date(2026, 10, 31))
    start_date = end_date - timedelta(days=89)
    parser = argparse.ArgumentParser(description="Create a short sales brief using NEXORA's MCP tools.")
    parser.add_argument("--start", type=date.fromisoformat, default=start_date, help="inclusive ISO date (YYYY-MM-DD)")
    parser.add_argument("--end", type=date.fromisoformat, default=end_date, help="inclusive ISO date (YYYY-MM-DD)")
    parser.add_argument("--region", help="optional region name")
    args = parser.parse_args()

    mcp_url = os.getenv("NEXORA_MCP_URL", "http://127.0.0.1:8000/mcp")
    model = os.getenv("CREWAI_MODEL", "openai/gpt-4o-mini")
    mcp = MCPServerHTTP(
        url=mcp_url,
        streamable=True,
        tool_filter=create_static_tool_filter(allowed_tool_names=["summarize_sales"]),
    )
    analyst = Agent(
        role="Business analyst",
        goal="Summarize measured sales results accurately and clearly",
        backstory="You are a careful analyst. You use tool results as evidence and label synthetic data.",
        llm=model,
        mcps=[mcp],
        verbose=False,
    )
    region_filter = args.region or "all regions"
    brief_task = Task(
        description=(
            f"Use the NEXORA summarize_sales tool for {args.start.isoformat()} through "
            f"{args.end.isoformat()} and {region_filter}. Then write a concise sales brief with "
            "the completed-order count, sales amount, average order value, metric definition, "
            "date range, and a clear note that these are synthetic demo figures. Do not invent results."
        ),
        expected_output="A concise, evidence-based sales brief with filters and synthetic-data caveat.",
        agent=analyst,
    )
    result = Crew(agents=[analyst], tasks=[brief_task], verbose=False).kickoff()
    print(result)


if __name__ == "__main__":
    main()
