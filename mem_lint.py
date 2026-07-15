#!/usr/bin/env python3

from __future__ import annotations
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
import re

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

def read_lines(path: Path) ->list[str]:
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def parse_frontmatter(lines: list[str]) -> tuple[dict, int]:
    """ Parse leading `--- ... ---` frontmatter. """
    if not lines or lines[0].strip() != "---":
        return {}, 1
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, 1
    
    fm: dict = {}
    parent: str | None = None
    for raw in lines[1:end]:
        if not raw.strip():
            continue
        m = re.match(r"^(\s*)([A-Za-z0-9_]+):\s*(.*)$", raw)
        if not m:
            continue
        indent, key, val = m.group(1), m.group(2), m.group(3).strip()
        if len(val) >=2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if not indent:
            if val == "":
                fm[key] = {}
                parent = key
            else:
                fm[key] = val
                parent = None
        elif parent and isinstance(fm.get(parent), dict):
            fm[parent][key] = val
    return fm, end + 2

def classify_frontmatter(fm: dict) -> tuple[str, str | None]:
    """decide fm_style ('none'|'top'|'nested'|'mixed') and effective type."""
    has_top_type = "type" in fm and not isinstance(fm.get("type"), dict)
    meta = fm.get("metadata") if isinstance(fm.get("metadata"), dict) else {}
    has_nested_type = "type" in meta

    if not fm:
        style = "none"
    elif has_nested_type and has_top_type:
        style = "mixed"
    elif has_nested_type:
        style = "nested"
    elif has_top_type:
        style = "top"
    else:
        style = "none"

    if has_nested_type:
        effective_type = meta.get("type")
    elif has_top_type:
        effective_type = fm.get("type")
    else:
        effective_type = None

    return style, effective_type

def load_document(path: Path) -> Document:
    lines = read_lines(path)
    _fm, body_start = parse_frontmatter(lines)
    return Document(path=path, lines=lines, body_start_line=body_start)

def load_note(path: Path) -> Note:
    lines = read_lines(path)
    fm, body_start = parse_frontmatter(lines)
    fm_style, effective_type = classify_frontmatter(fm)
    body = "\n".join(lines[body_start - 1:]) if body_start - 1 < len(lines) else ""
    return Note(
        path=path,
        lines=lines,
        body_start_line=body_start,
        slug=path.stem,
        frontmatter=fm,
        fm_style=fm_style,
        effective_type=effective_type,
        body=body,
        mtime=path.stat().st_mtime,
    )