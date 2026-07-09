#!/usr/bin/env python3

from __future__ import annotations
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

# data model
class Severity(IntEnum):
    ERROR = 3
    WARN = 2
    REVIEW = 1

@dataclass
class Document:
    path: Path
    lines: list[str]
    body_start_line: int

@dataclass
class Note(Document):
    slug: str
    frontmatter: dict
    fm_style: str
    effective_type: str | None
    body: str
    mtime: float

@dataclass(frozen=True)
class Finding:
    severity: Severity
    check: str
    file: str
    line: int | None
    message: str

@dataclass(frozen=True)
class LintContext:
    memdir: Path
    notes: list[Note]
    index: Document
    now: float
    stale_days: int