"""Generates docs/architecture_deck.pptx -- the "detailed structural
architecture" submission artifact (spec.md §2.4). Regenerate after any
change to the declared feature count, metrics, or agent roster so the deck
never drifts from spec.md/evals/BENCHMARK_REPORT.md.

Modern PowerPoint format (.pptx / Office Open XML) rather than the legacy
binary .ppt (OLE compound file) -- python-pptx, the only maintained
Python library for this, only writes .pptx. PowerPoint 2007+ opens .pptx
natively; there is no legacy .ppt writer in active use.

Run: .venv/Scripts/python.exe scripts/generate_architecture_deck.py
"""

from __future__ import annotations

from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN
from pptx.util import Emu, Inches, Pt

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "docs" / "architecture_deck.pptx"

NAVY = RGBColor(0x1E, 0x2A, 0x4A)
INDIGO = RGBColor(0x4F, 0x46, 0xE5)
SLATE = RGBColor(0x33, 0x41, 0x55)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
LIGHT_BG = RGBColor(0xF3, 0xF4, 0xF6)


def _set_title_style(title_shape, size=32, color=NAVY):
    title_shape.text_frame.paragraphs[0].font.size = Pt(size)
    title_shape.text_frame.paragraphs[0].font.bold = True
    title_shape.text_frame.paragraphs[0].font.color.rgb = color


def add_title_slide(prs: Presentation) -> None:
    layout = prs.slide_layouts[0]
    slide = prs.slides.add_slide(layout)
    slide.shapes.title.text = "RegAgentX"
    _set_title_style(slide.shapes.title, size=44)
    subtitle = slide.placeholders[1]
    subtitle.text_frame.text = "Autonomous Regulatory Compliance Engine"
    p2 = subtitle.text_frame.add_paragraph()
    p2.text = "ET AI Hackathon: Agentic Edition -- Problem Statement 1 (Banking & Financial Regulations)"
    p2.font.size = Pt(16)
    p3 = subtitle.text_frame.add_paragraph()
    p3.text = "Solution Structure Declaration: F3 / D2"
    p3.font.size = Pt(20)
    p3.font.bold = True
    p3.font.color.rgb = INDIGO


def add_flow_slide(prs: Presentation) -> None:
    layout = prs.slide_layouts[5]
    slide = prs.slides.add_slide(layout)
    slide.shapes.title.text = "End-to-End Architecture: Regulation to Remediation"
    _set_title_style(slide.shapes.title, size=26)

    nodes = [
        "Regulatory\nDocuments",
        "Obligation\nExtraction",
        "Applicability\n(per bank entity)",
        "Control\nMapping",
        "Evidence\nLinking",
        "Gap\nIdentification",
        "Remediation\nPlanning",
        "Ongoing\nMonitoring",
    ]
    top = Inches(1.7)
    box_w, box_h, gap = Inches(1.28), Inches(1.05), Inches(0.14)
    start_left = Inches(0.35)
    for i, label in enumerate(nodes):
        left = Emu(int(start_left) + i * (int(box_w) + int(gap)))
        box = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, left, top, box_w, box_h)
        box.fill.solid()
        box.fill.fore_color.rgb = INDIGO if i % 2 == 0 else NAVY
        box.line.color.rgb = WHITE
        tf = box.text_frame
        tf.word_wrap = True
        tf.paragraphs[0].text = label
        tf.paragraphs[0].font.size = Pt(11)
        tf.paragraphs[0].font.color.rgb = WHITE
        tf.paragraphs[0].alignment = PP_ALIGN.CENTER
        if i < len(nodes) - 1:
            arrow_left = Emu(int(left) + int(box_w))
            arrow = slide.shapes.add_shape(
                MSO_SHAPE.RIGHT_ARROW, arrow_left, Emu(int(top) + int(box_h) // 2 - Emu(90000)), gap, Emu(180000)
            )
            arrow.fill.solid()
            arrow.fill.fore_color.rgb = SLATE
            arrow.line.fill.background()

    # Additive cross-regulation branch, drawn below the main spine.
    branch_top = Inches(3.2)
    branch = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(2.2), branch_top, Inches(4.0), Inches(0.8))
    branch.fill.solid()
    branch.fill.fore_color.rgb = RGBColor(0x0F, 0x76, 0x6E)
    branch.line.color.rgb = WHITE
    tf = branch.text_frame
    tf.word_wrap = True
    tf.paragraphs[0].text = "CrossReg Agent -- compares obligations across regulatory frameworks (overlaps, conflicts, supersessions)"
    tf.paragraphs[0].font.size = Pt(12)
    tf.paragraphs[0].font.color.rgb = WHITE
    tf.paragraphs[0].alignment = PP_ALIGN.CENTER

    connector = slide.shapes.add_connector(
        1, Inches(4.2), Inches(2.75), Inches(4.2), branch_top
    )
    connector.line.color.rgb = SLATE
    connector.line.width = Pt(1.5)

    caption = slide.shapes.add_textbox(Inches(0.35), Inches(4.3), Inches(9.3), Inches(1.4))
    ctf = caption.text_frame
    ctf.word_wrap = True
    ctf.text = (
        "Every stage carries full provenance -- source file, page, and a verified quote -- so any "
        "remediation can be traced back to the exact regulatory clause that required it, and back again."
    )
    ctf.paragraphs[0].font.size = Pt(14)
    ctf.paragraphs[0].font.italic = True
    ctf.paragraphs[0].font.color.rgb = SLATE


def add_table_slide(
    prs: Presentation, title: str, headers: list[str], rows: list[list[str]], col_widths: list[float], note: str | None = None
) -> None:
    layout = prs.slide_layouts[5]
    slide = prs.slides.add_slide(layout)
    slide.shapes.title.text = title
    _set_title_style(slide.shapes.title, size=24)

    n_rows = len(rows) + 1
    n_cols = len(headers)
    table_top = Inches(1.35)
    table_height = Inches(4.6) if not note else Inches(4.1)
    table_shape = slide.shapes.add_table(n_rows, n_cols, Inches(0.35), table_top, Inches(9.3), table_height)
    table = table_shape.table
    for i, w in enumerate(col_widths):
        table.columns[i].width = Inches(w)

    for c, header in enumerate(headers):
        cell = table.cell(0, c)
        cell.text = header
        cell.fill.solid()
        cell.fill.fore_color.rgb = NAVY
        p = cell.text_frame.paragraphs[0]
        p.font.bold = True
        p.font.size = Pt(13)
        p.font.color.rgb = WHITE

    for r, row in enumerate(rows, start=1):
        for c, value in enumerate(row):
            cell = table.cell(r, c)
            cell.text = value
            cell.fill.solid()
            cell.fill.fore_color.rgb = WHITE if r % 2 else LIGHT_BG
            p = cell.text_frame.paragraphs[0]
            p.font.size = Pt(11.5)
            p.font.color.rgb = SLATE

    if note:
        box = slide.shapes.add_textbox(Inches(0.35), Inches(5.55), Inches(9.3), Inches(1.0))
        tf = box.text_frame
        tf.word_wrap = True
        tf.text = note
        tf.paragraphs[0].font.size = Pt(13)
        tf.paragraphs[0].font.italic = True
        tf.paragraphs[0].font.color.rgb = SLATE


def build() -> None:
    prs = Presentation()
    prs.slide_width = Inches(10)
    prs.slide_height = Inches(6.25)

    # Slide 1
    add_title_slide(prs)

    # Slide 2
    add_flow_slide(prs)

    # Slide 3 -- Multi-agent workflow & model usage
    add_table_slide(
        prs,
        "Multi-Agent Workflow & Model Usage",
        ["Agent", "Responsibility", "Model / Role"],
        [
            ["Ingestion", "Extracts obligations from regulatory text with span-level source verification", "GPT-5.1 (Extractor)"],
            ["Applicability", "Determines which obligations apply to a given bank entity", "GPT-5.1 (Extractor)"],
            ["Mapping", "Retrieves candidate controls (embedding + cross-encoder rerank) and adjudicates coverage level", "GPT-5.1 (Extractor) + reranker"],
            ["Audit", "Classifies compliance gaps and computes a deterministic, weighted risk score", "GPT-5.1 (Reasoner)"],
            ["Judge", "Independently reviews every gap finding before it is finalized", "Gemini 3.8 Flash (Judge)"],
            ["Remediation", "Produces the remediation plan for each confirmed gap", "GPT-5.1 (Reasoner)"],
            ["CrossReg", "Compares obligations across regulatory frameworks to detect overlaps, conflicts, supersessions, and implementations", "GPT-5.1 (Extractor)"],
        ],
        col_widths=[1.3, 5.0, 3.0],
        note=(
            "The Judge always runs on a different model family than the agent whose work it is reviewing -- "
            "an architectural control against a single model's blind spots reviewing its own output."
        ),
    )

    # Slide 4 -- Feature coverage
    add_table_slide(
        prs,
        "Feature Coverage -- 11 of 14 Brief Features (F3, floor is 8)",
        ["#", "Feature", "How RegAgentX Delivers It"],
        [
            ["1", "Regulatory intelligence & knowledge ingestion", "Versioned clause store from structured and textual regulatory sources"],
            ["3", "Regulatory obligation extraction", "Span-verified extraction agent with a zero-hallucination gate"],
            ["4", "Bank control-framework understanding", "Structured control objects from policy documents and control registers"],
            ["5", "Regulatory-to-control mapping", "Embedding + rerank retrieval with LLM adjudication and rationale"],
            ["6", "Control effectiveness assessment", "Design vs. operating effectiveness, evidence-weighted"],
            ["7", "Evidence-based compliance assessment", "Text/CSV/PDF evidence linking with freshness and sufficiency rules"],
            ["8", "Gap identification", "Multi-class gap taxonomy: deterministic preconditions plus LLM classification"],
            ["9", "Risk-based gap prioritization", "Deterministic, weighted scoring over LLM-supplied risk factors"],
            ["10", "Remediation recommendation engine", "Control-design delta, owner, effort, target date, test plan"],
            ["13", "Cross-regulation intelligence", "CrossReg agent compares obligations across frameworks on demand"],
            ["14", "Regulatory contradiction detection", "Same agent flags conflicting requirements with severity and resolution guidance"],
        ],
        col_widths=[0.5, 3.0, 5.8],
    )

    # Slide 5 -- Tech stack
    add_table_slide(
        prs,
        "Technology Stack",
        ["Component", "Why We Chose It"],
        [
            ["LangGraph", "Stateful, cyclical multi-agent orchestration with checkpointing -- a run can pause for human review or recover from a transient failure and resume exactly where it left off"],
            ["Supabase (Postgres)", "Managed relational store for the traceability spine -- every obligation, control, gap, and remediation is a row with full referential integrity and audit history"],
            ["ChromaDB", "Vector store powering embedding + cross-encoder rerank retrieval for obligation-to-control matching"],
            ["SQLite response cache", "Deterministic, cost-controlled replay of every model call -- the same run reproduces the same output instantly, and the system runs reliably offline"],
            ["GPT-5.1 + Gemini (dual-model)", "GPT-5.1 handles extraction and reasoning; Gemini independently reviews every finding as the Judge -- two different model families so review is a genuine second opinion, not the same model checking its own work"],
        ],
        col_widths=[2.6, 6.7],
    )

    # Slide 6 -- Reliability & evals
    add_table_slide(
        prs,
        "Demonstrable Reliability (D2)",
        ["Metric", "Result"],
        [
            ["Provenance / span-grounding rate", "100% (10/10) -- every accepted obligation's citation is independently verified against its source text"],
            ["Obligation extraction F1", "0.900 (precision 0.900, recall 0.900)"],
            ["Mapping coverage-level classification agreement", "100% (10/10)"],
            ["Cross-regulation relation classification accuracy", "100% (5/5)"],
            ["Automated test suite", "58/58 passing"],
            ["Risk scoring", "Deterministic weighted arithmetic in code over LLM-supplied factors -- not left to model judgment"],
            ["Autonomy gating", "Confidence-banded auto-accept, with a human-in-the-loop queue for lower-confidence outputs"],
        ],
        col_widths=[3.6, 5.7],
        note="Every reported metric states its denominator -- the same discipline the product itself applies to every number shown in the UI.",
    )

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    prs.save(OUTPUT_PATH)
    print(f"Wrote {OUTPUT_PATH} ({len(prs.slides)} slides)")


if __name__ == "__main__":
    build()
