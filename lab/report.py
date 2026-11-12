"""Self-contained PDF reports for saved backtest runs."""
from __future__ import annotations

import io
import json
import math
from datetime import date, datetime
from typing import Any, Iterable, Sequence
from xml.sax.saxutils import escape

import numpy as np
import pandas as pd
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Flowable,
    LongTable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

_INK = colors.HexColor("#17233b")
_MUTED = colors.HexColor("#56647a")
_TEAL = colors.HexColor("#0d8294")
_GRID = colors.HexColor("#dce3ec")
_GREEN = colors.HexColor("#16804a")
_RED = colors.HexColor("#bc3542")
_PAGE = landscape(A4)
_WIDTH = _PAGE[0] - 22 * mm


def _display(value: Any) -> str:
    if value is None or value is pd.NA:
        return "—"
    if isinstance(value, (date, datetime, pd.Timestamp)):
        return value.isoformat(sep=" ", timespec="seconds") if isinstance(value, (datetime, pd.Timestamp)) else value.isoformat()
    if isinstance(value, (bool, np.bool_)):
        return "Yes" if value else "No"
    if isinstance(value, (int, np.integer)):
        return f"{int(value):,}"
    if isinstance(value, (float, np.floating)):
        number = float(value)
        if math.isnan(number):
            return "—"
        if math.isinf(number):
            return "∞" if number > 0 else "−∞"
        return f"{number:,.4f}".rstrip("0").rstrip(".")
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, default=str, ensure_ascii=True, sort_keys=True)
    return str(value)


class _PerformancePlot(Flowable):
    def __init__(self, title: str, values: Sequence[float], kind: str = "line"):
        super().__init__()
        self.title = title
        self.values = [float(v) if np.isfinite(v) else 0.0 for v in values]
        self.kind = kind
        self.width = _WIDTH
        self.height = 46 * mm

    def draw(self):
        canvas = self.canv
        left, right, top, bottom = 16 * mm, self.width - 4 * mm, self.height - 8 * mm, 8 * mm
        canvas.saveState()
        canvas.setFillColor(_INK)
        canvas.setFont("Helvetica-Bold", 9)
        canvas.drawString(2 * mm, self.height - 3 * mm, self.title)
        canvas.setStrokeColor(_GRID)
        canvas.setLineWidth(0.5)
        for i in range(5):
            y = bottom + i * (top - bottom) / 4
            canvas.line(left, y, right, y)
        if self.values:
            low, high = min(self.values), max(self.values)
            if self.kind == "bar":
                low, high = min(low, 0), max(high, 0)
            if high == low:
                high, low = high + 1, low - 1
            y = lambda v: bottom + (v - low) / (high - low) * (top - bottom)
            zero = y(0) if low <= 0 <= high else bottom
            if self.kind == "bar":
                step = (right - left) / len(self.values)
                for i, value in enumerate(self.values):
                    canvas.setFillColor(_GREEN if value >= 0 else _RED)
                    bar_y = y(value)
                    canvas.rect(left + i * step + step * 0.1, min(bar_y, zero),
                                max(0.4, step * 0.8), max(0.5, abs(bar_y - zero)), stroke=0, fill=1)
            else:
                canvas.setStrokeColor(_TEAL)
                canvas.setLineWidth(1.6)
                path = canvas.beginPath()
                for i, value in enumerate(self.values):
                    x = left + i * (right - left) / max(1, len(self.values) - 1)
                    (path.moveTo if i == 0 else path.lineTo)(x, y(value))
                canvas.drawPath(path, stroke=1, fill=0)
            canvas.setFillColor(_MUTED)
            canvas.setFont("Helvetica", 7)
            canvas.drawRightString(right, bottom - 4 * mm, _display(high))
            canvas.drawRightString(right, top + 1 * mm, _display(low))
        canvas.restoreState()


class _SessionTradeChart(Flowable):
    def __init__(self, chart: dict):
        super().__init__()
        self.chart = chart
        self.width = _WIDTH
        self.height = 54 * mm

    def draw(self):
        canvas = self.canv
        left, right = 18 * mm, self.width - 3 * mm
        bottom, top = 8 * mm, self.height - 8 * mm
        highs, lows = self.chart["high"], self.chart["low"]
        finite = [value for value in highs + lows if np.isfinite(value)]
        if not finite:
            return
        low, high = min(finite), max(finite)
        if high == low:
            high, low = high + 1, low - 1
        x = lambda index: left + index * (right - left) / max(1, len(highs) - 1)
        y = lambda value: bottom + (value - low) / (high - low) * (top - bottom)
        canvas.saveState()
        canvas.setFillColor(_INK)
        canvas.setFont("Helvetica-Bold", 8)
        canvas.drawString(2 * mm, self.height - 3 * mm,
                          f"{self.chart['contract']} · {self.chart['date']} · option premium with entry/exit markers")
        canvas.setStrokeColor(_GRID)
        canvas.setLineWidth(.4)
        for i in range(4):
            grid_y = bottom + i * (top - bottom) / 3
            canvas.line(left, grid_y, right, grid_y)
        canvas.setFont("Helvetica", 6)
        candle_width = max(.5 * mm, min(2 * mm, (right - left) / max(1, len(highs)) * .65))
        for index, (opening, high_value, low_value, closing) in enumerate(
                zip(self.chart["open"], highs, lows, self.chart["close"])):
            if not all(np.isfinite(value) for value in (opening, high_value, low_value, closing)):
                continue
            px = x(index)
            up = closing >= opening
            canvas.setStrokeColor(_GREEN if up else _RED)
            canvas.setFillColor(_GREEN if up else _RED)
            canvas.line(px, y(high_value), px, y(low_value))
            body_bottom, body_top = sorted((y(opening), y(closing)))
            canvas.rect(px - candle_width / 2, body_bottom, candle_width,
                        max(.3 * mm, body_top - body_bottom), stroke=0, fill=1)
        canvas.setFont("Helvetica", 6)
        for event in self.chart["events"]:
            color = _GREEN if event["kind"] == "ENTRY" else _RED
            px, py = x(event["index"]), y(event["price"])
            canvas.setFillColor(color)
            symbol = canvas.beginPath()
            if event["kind"] == "ENTRY":
                symbol.moveTo(px, py + 3 * mm)
                symbol.lineTo(px - 2 * mm, py - 1 * mm)
                symbol.lineTo(px + 2 * mm, py - 1 * mm)
            else:
                symbol.moveTo(px, py - 3 * mm)
                symbol.lineTo(px - 2 * mm, py + 1 * mm)
                symbol.lineTo(px + 2 * mm, py + 1 * mm)
            symbol.close()
            canvas.drawPath(symbol, stroke=0, fill=1)
        canvas.setFillColor(_MUTED)
        canvas.drawString(left, 2 * mm, "09:15")
        canvas.drawRightString(right, 2 * mm, "15:29")
        canvas.restoreState()


def _table(rows: list, widths: Sequence[float], header: bool = True, font_size: float = 7) -> Table:
    table = LongTable(rows, colWidths=widths, repeatRows=1 if header else 0, hAlign="LEFT")
    commands = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.25, _GRID),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("FONTSIZE", (0, 0), (-1, -1), font_size),
        ("TEXTCOLOR", (0, 0), (-1, -1), _INK),
        ("ROWBACKGROUNDS", (0, 1 if header else 0), (-1, -1),
         [colors.white, colors.HexColor("#f4f7fa")]),
    ]
    if header:
        commands.extend([
            ("BACKGROUND", (0, 0), (-1, 0), _INK),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ])
    table.setStyle(TableStyle(commands))
    return table


def _paragraph(value: Any, style: ParagraphStyle) -> Paragraph:
    return Paragraph(escape(_display(value)).replace("\n", "<br/>"), style)


def _metadata_table(title: str, data: dict, styles, max_width: float) -> Table:
    pairs = list(data.items())
    rows = [[_paragraph(title, styles["small_heading"]), ""]]
    for key, value in pairs:
        rows.append([_paragraph(key.replace("_", " "), styles["label"]),
                     _paragraph(value, styles["body"])])
    table = Table(rows, colWidths=[max_width * 0.28, max_width * 0.72], hAlign="LEFT")
    table.setStyle(TableStyle([
        ("SPAN", (0, 0), (-1, 0)), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eaf3f6")),
        ("GRID", (0, 1), (-1, -1), 0.25, _GRID), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return table


def _footer(canvas, document):
    canvas.saveState()
    canvas.setStrokeColor(_GRID)
    canvas.line(11 * mm, 9 * mm, _PAGE[0] - 11 * mm, 9 * mm)
    canvas.setFillColor(_MUTED)
    canvas.setFont("Helvetica", 7)
    canvas.drawString(11 * mm, 5 * mm, "NIFTY Options Lab · private local backtest report")
    canvas.drawRightString(_PAGE[0] - 11 * mm, 5 * mm, f"Page {document.page}")
    canvas.restoreState()


def build_backtest_pdf(run: dict) -> bytes:
    """Build a paginated report containing all run metrics, sessions, and trade fields."""
    output = io.BytesIO()
    document = SimpleDocTemplate(
        output, pagesize=_PAGE, leftMargin=11 * mm, rightMargin=11 * mm,
        topMargin=11 * mm, bottomMargin=14 * mm,
        title=f"Backtest report - {run.get('name', run['id'])}",
        author="NIFTY Options Lab",
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="report_title", parent=styles["Title"], fontName="Helvetica-Bold",
                              fontSize=20, leading=24, textColor=_INK, alignment=TA_LEFT, spaceAfter=2 * mm))
    styles.add(ParagraphStyle(name="small_heading", parent=styles["Heading3"], fontSize=9, leading=11,
                              textColor=_TEAL, spaceBefore=3 * mm, spaceAfter=1 * mm))
    styles.add(ParagraphStyle(name="label", parent=styles["BodyText"], fontName="Helvetica-Bold",
                              fontSize=7, leading=9, textColor=_MUTED))
    styles.add(ParagraphStyle(name="body", parent=styles["BodyText"], fontSize=7, leading=9, textColor=_INK))
    styles.add(ParagraphStyle(name="section", parent=styles["Heading2"], fontSize=13, leading=15,
                              textColor=_INK, spaceBefore=4 * mm, spaceAfter=2 * mm))
    styles.add(ParagraphStyle(name="cell", parent=styles["BodyText"], fontSize=5.5, leading=6.5,
                              textColor=_INK, wordWrap="CJK"))

    metrics = run.get("metrics", {})
    daily = run.get("daily", pd.DataFrame())
    trades = run.get("trades", pd.DataFrame())
    strategy = run.get("strategy", {})
    settings = run.get("settings", {})
    story = [
        Paragraph("Backtest report", styles["report_title"]),
        Paragraph(escape(str(run.get("name", "Untitled"))), styles["Heading2"]),
        Spacer(1, 2 * mm),
    ]
    story.append(_metadata_table("Run details", {
        "Run ID": run.get("id"), "Created": run.get("created"), "Kind": run.get("kind"),
        "Elapsed seconds": run.get("elapsed"),
    }, styles, _WIDTH))
    story.append(Spacer(1, 3 * mm))
    metric_items = list(metrics.items())
    metric_rows = []
    for i in range(0, len(metric_items), 2):
        row = []
        for key, value in metric_items[i:i + 2]:
            row.extend([_paragraph(key.replace("_", " "), styles["label"]),
                        _paragraph(value, styles["body"])])
        if len(row) == 2:
            row.extend(["", ""])
        metric_rows.append(row)
    if metric_rows:
        story.append(Paragraph("All performance metrics", styles["section"]))
        mt = Table(metric_rows, colWidths=[_WIDTH * .24, _WIDTH * .26, _WIDTH * .24, _WIDTH * .26])
        mt.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), .25, _GRID), ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BACKGROUND", (0, 0), (-1, -1), colors.white),
            ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(mt)

    if not daily.empty:
        story.extend([
            Paragraph("Equity and drawdown", styles["section"]),
            _PerformancePlot("Cumulative P&L / equity", daily["equity"].tolist()),
            Spacer(1, 2 * mm),
            _PerformancePlot("Drawdown", daily["drawdown"].tolist()),
            Spacer(1, 2 * mm),
            _PerformancePlot("Daily net P&L", daily["net_pnl"].tolist(), "bar"),
        ])

    story.append(PageBreak())
    story.append(Paragraph("Strategy and backtest settings", styles["section"]))
    story.append(_metadata_table("Strategy", strategy, styles, _WIDTH))
    story.append(Spacer(1, 3 * mm))
    story.append(_metadata_table("Settings", settings, styles, _WIDTH))

    if not daily.empty:
        story.append(PageBreak())
        story.append(Paragraph("Daily P&L and equity", styles["section"]))
        daily_columns = ["date", "traded", "legs", "gross_pnl", "costs", "net_pnl",
                         "cum_pnl", "equity", "drawdown", "drawdown_pct"]
        rows = [[_paragraph(name.replace("_", " "), styles["cell"]) for name in daily_columns]]
        for record in daily[daily_columns].itertuples(index=False, name=None):
            rows.append([_paragraph(value, styles["cell"]) for value in record])
        story.append(_table(rows, [_WIDTH / len(daily_columns)] * len(daily_columns), font_size=6))
        from . import insights
        breakdowns = (
            ("Monthly performance", insights.monthly_rows(daily), ["year", "month", "days", "wins", "pnl"]),
            ("Weekday performance", insights.weekday_rows(daily),
             ["name", "days", "net", "avg", "win"]),
        )
        for title, records, columns in breakdowns:
            if not records:
                continue
            story.append(Paragraph(title, styles["small_heading"]))
            rows = [[_paragraph(name.replace("_", " "), styles["cell"]) for name in columns]]
            rows.extend([[_paragraph(record.get(name), styles["cell"]) for name in columns]
                         for record in records])
            story.append(_table(rows, [_WIDTH / len(columns)] * len(columns), font_size=6))

    if not trades.empty:
        story.append(PageBreak())
        story.append(Paragraph("Complete trade log", styles["section"]))
        groups = [
            ("Execution", ["date", "leg_id", "segment", "signal_direction", "side", "option_type",
                           "strike", "expiry", "lots", "qty", "entry_time", "entry_price",
                           "exit_time", "exit_price", "exit_reason"]),
            ("P&L and excursions", ["points", "gross_pnl", "slippage_cost", "costs", "net_pnl",
                                    "day_mae", "day_mfe"]),
            ("Spot and signal readings", ["signal_value_entry", "signal_ema_entry", "signal_fast_ema_entry",
                                          "signal_slow_ema_entry", "signal_spot_entry", "spot_entry",
                                          "signal_value_exit", "signal_ema_exit", "signal_fast_ema_exit",
                                          "signal_slow_ema_exit", "signal_spot_exit", "spot_exit"]),
            ("Entry Greeks", ["entry_iv", "entry_delta", "entry_gamma", "entry_theta", "entry_vega"]),
        ]
        used = set()
        for title, requested in groups:
            columns = [name for name in requested if name in trades.columns]
            used.update(columns)
            if not columns:
                continue
            story.append(Paragraph(title, styles["small_heading"]))
            rows = [[_paragraph(name.replace("_", " "), styles["cell"]) for name in columns]]
            rows.extend([[_paragraph(value, styles["cell"]) for value in record]
                         for record in trades[columns].itertuples(index=False, name=None)])
            story.append(_table(rows, [_WIDTH / len(columns)] * len(columns), font_size=5.5))
            story.append(Spacer(1, 2 * mm))
        extra_columns = [name for name in trades.columns if name not in used]
        if extra_columns:
            story.append(Paragraph("Additional trade fields", styles["small_heading"]))
            rows = [[_paragraph(name.replace("_", " "), styles["cell"]) for name in extra_columns]]
            rows.extend([[_paragraph(value, styles["cell"]) for value in record]
                         for record in trades[extra_columns].itertuples(index=False, name=None)])
            story.append(_table(rows, [_WIDTH / len(extra_columns)] * len(extra_columns), font_size=5.5))

    trade_charts = run.get("trade_charts", [])
    if trade_charts:
        story.append(PageBreak())
        story.append(Paragraph("Session trade charts", styles["section"]))
        story.append(Paragraph("Option-premium candles with execution fills marked at their trade times; green triangles are entries and red triangles are exits.",
                               styles["body"]))
        for index, chart in enumerate(trade_charts):
            story.append(_SessionTradeChart(chart))
            if index % 3 == 2 and index != len(trade_charts) - 1:
                story.append(PageBreak())

    skipped = run.get("skipped", [])
    if skipped:
        story.append(PageBreak())
        story.append(Paragraph("Skipped and filtered sessions", styles["section"]))
        rows = [[_paragraph("Date", styles["cell"]), _paragraph("Reason", styles["cell"])]]
        rows.extend([[_paragraph(item[0], styles["cell"]), _paragraph(item[1], styles["cell"])]
                     for item in skipped])
        story.append(_table(rows, [_WIDTH * .18, _WIDTH * .82]))

    document.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return output.getvalue()
