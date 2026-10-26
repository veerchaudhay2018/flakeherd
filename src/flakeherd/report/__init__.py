"""Reporters: turning an AnalysisResult into terminal, GitHub and JSON output."""

from __future__ import annotations

from .github import (
    annotations,
    append_step_summary,
    emit_annotations,
    in_github_actions,
    summary_markdown,
)
from .json_report import build_payload, write_json
from .terminal import render

__all__ = [
    "annotations",
    "append_step_summary",
    "build_payload",
    "emit_annotations",
    "in_github_actions",
    "render",
    "summary_markdown",
    "write_json",
]
