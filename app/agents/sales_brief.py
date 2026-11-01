"""Primary CrewAI workflow for evidence-grounded sales reporting."""

from __future__ import annotations

import json
import os
from datetime import date

from crewai import Agent, Crew, Process, Task
from crewai.tools import tool

from app.services.analytics import get_sales_summary


@tool("nexora_sales_summary")
def nexora_sales_summary(start_date: str, end_date: str, region: str = "", dataset_id: str = "demo") -> str:
    """Fetch actual completed-order sales, count, and average order value for an inclusive date range and optional region."""
    parsed_region = region.strip() or None
    result = get_sales_summary(
        date.fromisoformat(start_date),
        date.fromisoformat(end_date),
        parsed_region,
        dataset_id=dataset_id,
    )
    return json.dumps(result, ensure_ascii=False)


def build_sales_brief(start_date: date, end_date: date, region: str | None = None, dataset_id: str = "demo") -> tuple[str, dict[str, object]]:
    """Run a two-agent CrewAI workflow and return the deterministic source metrics beside its prose."""
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")
    filters = {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "region": region or "",
        "dataset_id": dataset_id,
    }
    # Preserve the exact service output in the API response, independently of model wording.
    source_metrics = get_sales_summary(start_date, end_date, region, dataset_id=dataset_id)
    analyst = Agent(
        role="Sales Data Analyst",
        goal="Retrieve the requested sales metrics and explain what the measured numbers show",
        backstory="You are a cautious enterprise analyst. You use the NEXORA data tool and never invent figures.",
        tools=[nexora_sales_summary],
        llm=os.getenv("CREWAI_MODEL", "openai/gpt-4o-mini"),
        allow_delegation=False,
        max_iter=3,
        verbose=False,
    )
    editor = Agent(
        role="Executive Brief Editor",
        goal="Turn the analyst's measured findings into a short, readable executive update",
        backstory="You preserve every filter and caveat, and do not add facts that are absent from the analyst's evidence.",
        llm=os.getenv("CREWAI_MODEL", "openai/gpt-4o-mini"),
        allow_delegation=False,
        max_iter=2,
        verbose=False,
    )
    analysis_task = Task(
        description=(
            "Call nexora_sales_summary for start date {start_date}, end date {end_date}, "
            "region filter '{region}' (leave blank for all regions), and dataset '{dataset_id}'. Interpret only the returned data. Report the completed order "
            "count, revenue, average order value, metric definition, filters, and the source data label from the tool result."
        ),
        expected_output="A factual metric summary tied to the tool result, including filters and units.",
        agent=analyst,
    )
    editorial_task = Task(
        description=(
            "Rewrite the analyst's findings as a concise executive sales brief. Preserve the dates, "
            "region, measured values, units, metric definition, and source-data label. "
            "Do not infer causes or invent comparisons."
        ),
        expected_output="A short evidence-based executive brief with the requested scope and caveat.",
        agent=editor,
        context=[analysis_task],
    )
    crew = Crew(
        agents=[analyst, editor],
        tasks=[analysis_task, editorial_task],
        process=Process.sequential,
        verbose=False,
        memory=False,
    )
    result = crew.kickoff(inputs=filters)
    return str(result), source_metrics
