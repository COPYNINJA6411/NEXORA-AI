"""CSV, Excel, and PDF report generation from current analytics outputs."""

from __future__ import annotations

from datetime import date, datetime, timezone
import csv
from io import BytesIO, StringIO

import pandas as pd

from app.services.analytics import dashboard_data, dataset_metadata, detect_anomalies, forecast_revenue


def report_payload(
    start_date: date,
    end_date: date,
    *, region: str | None = None,
    category: str | None = None,
    segment: str | None = None,
    dataset_id: str | None = None,
) -> dict[str, object]:
    dashboard = dashboard_data(start_date, end_date, region=region, category=category, segment=segment, dataset_id=dataset_id)
    return {
        "title": "NEXORA AI Business Intelligence Report",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset_id": dataset_id or "demo",
        "data_label": dataset_metadata(dataset_id)["label"],
        "dashboard": dashboard,
        "anomalies": detect_anomalies(dataset_id),
        "forecast": forecast_revenue(dataset_id),
    }


def export_csv(payload: dict[str, object]) -> bytes:
    output = StringIO()
    writer = csv.writer(output)
    dashboard = payload["dashboard"]
    writer.writerow([payload["title"]])
    writer.writerow(["Generated at", payload["generated_at"]])
    writer.writerow(["Dataset", payload["dataset_id"], payload["data_label"]])
    writer.writerow([])
    writer.writerow(["KPI", "Value"])
    for key, value in dashboard["kpis"].items():
        writer.writerow([key, value])
    writer.writerow([])
    writer.writerow(["Monthly actuals"])
    monthly = dashboard["monthly"]
    if monthly:
        writer.writerow(monthly[0].keys())
        for row in monthly:
            writer.writerow(row.values())
    writer.writerow([])
    writer.writerow(["Anomaly flags"])
    anomalies = payload["anomalies"]["anomalies"]
    if anomalies:
        writer.writerow(anomalies[0].keys())
        for row in anomalies:
            writer.writerow(row.values())
    return output.getvalue().encode("utf-8-sig")


def export_xlsx(payload: dict[str, object]) -> bytes:
    output = BytesIO()
    dashboard = payload["dashboard"]
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        pd.DataFrame([{"title": payload["title"], "generated_at": payload["generated_at"], "dataset_id": payload["dataset_id"], "data_label": payload["data_label"]}]).to_excel(writer, sheet_name="Report", index=False)
        pd.DataFrame([dashboard["kpis"]]).to_excel(writer, sheet_name="KPIs", index=False)
        for sheet, key in (("Monthly", "monthly"), ("Regions", "regions"), ("Products", "products"), ("Segments", "segments"), ("Customers", "customers")):
            pd.DataFrame(dashboard[key]).to_excel(writer, sheet_name=sheet, index=False)
        pd.DataFrame(payload["anomalies"]["anomalies"]).to_excel(writer, sheet_name="Anomalies", index=False)
        pd.DataFrame(payload["forecast"]["history"] + payload["forecast"]["forecast"]).to_excel(writer, sheet_name="Forecast", index=False)
    return output.getvalue()


def export_pdf(payload: dict[str, object]) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

    output = BytesIO()
    doc = SimpleDocTemplate(output, pagesize=letter, rightMargin=0.6 * inch, leftMargin=0.6 * inch)
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="SubtitleCenter", parent=styles["Normal"], alignment=TA_CENTER, textColor=colors.HexColor("#52677b")))
    dashboard = payload["dashboard"]
    story = [Paragraph(payload["title"], styles["Title"]),
             Paragraph(f"Dataset: {payload['dataset_id']} · {payload['data_label']} · Generated {payload['generated_at']}", styles["SubtitleCenter"]),
             Spacer(1, 0.2 * inch), Paragraph("Executive summary", styles["Heading2"]),
             Paragraph(f"This report summarizes the selected date and dimension filters for {payload['data_label']}.", styles["BodyText"]),
             Spacer(1, 0.16 * inch), Paragraph("Key performance indicators", styles["Heading2"])]
    kpi_rows = [["Metric", "Value"]] + [[str(key).replace("_", " ").title(), str(value)] for key, value in dashboard["kpis"].items()]
    kpi_table = Table(kpi_rows, colWidths=[3.7 * inch, 2.0 * inch], repeatRows=1)
    kpi_table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#16324f")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#d3dde7")), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f1f5f9")]), ("PADDING", (0, 0), (-1, -1), 6)]))
    story.extend([kpi_table, Spacer(1, 0.18 * inch), Paragraph("Monthly revenue", styles["Heading2"])])
    monthly_rows = [["Month", "Revenue", "Profit"]] + [[row["month"], f"{row['revenue']:,.2f}", f"{row['profit']:,.2f}"] for row in dashboard["monthly"]]
    monthly_table = Table(monthly_rows, colWidths=[1.3 * inch, 2.1 * inch, 2.1 * inch], repeatRows=1)
    monthly_table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#16324f")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#d3dde7")), ("FONTSIZE", (0, 0), (-1, -1), 8), ("PADDING", (0, 0), (-1, -1), 5)]))
    story.append(monthly_table)
    story.extend([Spacer(1, 0.18 * inch), Paragraph("Detected anomalies", styles["Heading2"])])
    anomalies = payload["anomalies"]["anomalies"]
    if anomalies:
        for item in anomalies[:12]:
            story.append(Paragraph(f"<b>{item['metric']}</b> · {item.get('region','')} · {item['period']} · {item['change_pct']}% from baseline. Hypothesis: {item['hypothesis']}", styles["BodyText"]))
            story.append(Spacer(1, 3))
    else:
        story.append(Paragraph("No points crossed the configured anomaly thresholds for this dataset.", styles["BodyText"]))
    story.extend([Spacer(1, 0.18 * inch), Paragraph("Forecast", styles["Heading2"]), Paragraph(f"Method: {payload['forecast']['method']}. Backtest MAE: {payload['forecast']['backtest']['mae']}; RMSE: {payload['forecast']['backtest']['rmse']}. This baseline does not model seasonality or external drivers.", styles["BodyText"])])
    doc.build(story)
    return output.getvalue()
