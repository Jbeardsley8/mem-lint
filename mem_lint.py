#!/usr/bin/env python3
"""mem-lint — a linter for Claude Code memory directories.

Scans a `.../memory/` directory (MEMORY.md index + per-fact `*.md` notes) and
reports rot that is otherwise invisible until a note misleads you:

  ERROR  dead filesystem paths, dangling index entries, invalid frontmatter
  WARN   index/note disagreement, orphan notes, dangling wikilinks, schema drift
  REVIEW staleness signals (old notes still claiming to be "current")

Pure pipeline: files -> Notes -> Findings -> output. No third-party deps. Python 3.9+.
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import time
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

# checks
WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
VALID_TYPES = {"user", "feedback", "project", "reference"}
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# Absolute/home path tokens (need at least one segment after the root, so a bare
# "~" or lone "/" in prose isn't matched). Stops at spaces/quotes/punctuation.
PATH_RE = re.compile(r"(?:~|/Users|/Applications|/opt|/usr|/etc|/home)(?:/[\w.@+\-]+)+/?")
# Paths under these roots are runtime/system artifacts, not source references, so
# a "missing" one isn't necessarily rot — skip them in the dead-path check.
VOLATILE_PREFIXES = ("~/Library", "~/.Trash", "~/.cache", "/tmp", "/private", "/var")
# A path named on a line that also acknowledges it's gone isn't rot — it's the
# note correctly documenting a move/deletion. Don't flag those.
NEGATION_RE = re.compile(
    r"\b(no longer|deleted|removed|gone|dropped|killed|shelved|abandoned|"
    r"was at|used to|formerly|previously|moved from|renamed from)\b", re.IGNORECASE)
# `- [Title](target.md) — hook` bullet in MEMORY.md.
INDEX_BULLET_RE = re.compile(r"^\s*-\s*\[([^\]]+)\]\(([^)]+)\)\s*(?:[—\-–]\s*)?(.*)$")
# Phrases that assert a note is live/current — the trigger for a staleness check.
CURRENCY_MARKERS = re.compile(
    r"\b(current(?:ly)?|in[\s-]progress|in the middle|now working|WIP|ongoing|next session)\b",
    re.IGNORECASE)
DATE_RE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")

def canon(s: str) -> str:
    """Lowercase slug key: non-alphanumeric runs become '-', edges trimmed."""
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")

def path_exists(token: str) -> bool:
    return Path(os.path.expanduser(token)).exists()

def is_volatile(token: str) -> bool:
    return any(token.startswith(p) for p in VOLATILE_PREFIXES)

def paths_in_text(text: str) -> set[str]:
    return {m.group(0).rstrip("/.,);:") for m in PATH_RE.finditer(text)}

def newest_body_date(text: str) -> float | None:
    """POSIX timestamp of the newest valid YYYY-MM-DD date in the text."""
    best = None
    for m in DATE_RE.finditer(text):
        try:
            ts = time.mktime(time.strptime(m.group(0), "%Y-%m-%d"))
        except ValueError:
            continue
        if best is None or ts > best:
            best = ts
    return best

CHECKS: list = []

def register(fn):
    CHECKS.append(fn)
    return fn

@register
def check_wikilinks(ctx: LintContext) -> list[Finding]:
    slugs = {n.slug for n in ctx.notes}
    names = {n.frontmatter.get("name") for n in ctx.notes if n.frontmatter.get("name")}
    known = {canon(x) for x in (slugs | names)}
    out: list[Finding] = []
    for n in ctx.notes:
        for offset, line in enumerate(n.lines, start=1):
            for m in WIKILINK_RE.finditer(line):
                target = m.group(1).strip()
                if canon(target) not in known:
                    out.append(Finding(
                        severity=Severity.WARN,
                        check="wikilink",
                        file=n.path.name,
                        line=offset,
                        message=f"dangling [[{target}]] (may be intentional - a note to write later.)"
                    ))
    return out

@register
def check_frontmatter(ctx: LintContext) -> list[Finding]:
    out: list[Finding] = []

    def finding(n: Note, severity: Severity, message: str) -> Finding:
        return Finding(
            severity=severity,
            check="frontmatter",
            file=n.path.name,
            line=1,
            message=message,
        )

    for n in ctx.notes:
        if n.fm_style == "none":
            out.append(finding(n, Severity.ERROR, "no 'type' declared (missing or malformed frontmatter)"))
        elif n.fm_style == "mixed":
            out.append(finding(n, Severity.WARN, "schema drift: 'type' set both top-level and under metadata"))
        elif n.fm_style == "top":
            out.append(finding(n, Severity.WARN, "schema drift: top-level 'type:' — canonical is metadata.type"))

        if n.effective_type is not None and n.effective_type not in VALID_TYPES:
            out.append(finding(n, Severity.WARN, f"unknown type '{n.effective_type}'"))

        if not n.frontmatter.get("description"):
            out.append(finding(n, Severity.WARN, "missing 'description:'"))

        name = n.frontmatter.get("name")
        if not name:
            out.append(finding(n, Severity.WARN, "missing 'name:'"))
        elif not SLUG_RE.match(name):
            out.append(finding(n, Severity.WARN, f"non-slug name '{name}' (want kebab-case)"))
        elif canon(name) != canon(n.slug):
            out.append(finding(n, Severity.WARN, f"name '{name}' doesn't match filename '{n.slug}'"))

    return out

def _scan_paths(name: str, lines: list[str], start_line: int) -> list[Finding]:
    out: list[Finding] = []
    in_fence = False
    for offset, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence or NEGATION_RE.search(line):
            continue
        for m in PATH_RE.finditer(line):
            token = m.group(0).rstrip("/.,);:")
            if is_volatile(token):
                continue
            if not path_exists(token):
                out.append(Finding(Severity.ERROR, "dead-path", name, start_line + offset,
                                   f"path does not exist: {token}"))
    return out

@register
def check_dead_paths(ctx: LintContext) -> list[Finding]:
    out: list[Finding] = []
    for n in ctx.notes:
        body_lines = n.lines[n.body_start_line - 1:]
        out += _scan_paths(n.path.name, body_lines, n.body_start_line)
    if ctx.index.path.exists():
        out += _scan_paths(ctx.index.path.name, ctx.index.lines, 1)
    return out

@register
def check_index(ctx: LintContext) -> list[Finding]:
    out: list[Finding] = []
    if not ctx.index.path.exists():
        out.append(Finding(Severity.ERROR, "index", "MEMORY.md", None, "MEMORY.md index not found"))
        return out

    note_by_file = {n.path.name: n for n in ctx.notes}
    indexed: dict[str, int] = {}
    for offset, line in enumerate(ctx.index.lines, start=1):
        m = INDEX_BULLET_RE.match(line)
        if not m:
            continue
        target, hook = m.group(2).strip(), m.group(3).strip()
        if target in indexed:
            out.append(Finding(Severity.WARN, "index", "MEMORY.md", offset,
                               f"duplicate index entry for {target}"))
        indexed[target] = offset

        note = note_by_file.get(target)
        if note is None:
            out.append(Finding(Severity.ERROR, "index", "MEMORY.md", offset,
                               f"index points to missing file: {target}"))
            continue

        # Index/note path disagreement: a path cited in the hook that the note
        # itself never mentions (while the note cites some other path).
        hook_paths = {p for p in paths_in_text(hook) if not is_volatile(p)}
        if hook_paths:
            note_text = note.frontmatter.get("description", "") + "\n" + note.body
            note_paths = {p for p in paths_in_text(note_text) if not is_volatile(p)}
            missing = hook_paths - note_paths
            if missing and note_paths:
                out.append(Finding(Severity.WARN, "index-drift", "MEMORY.md", offset,
                                   f"index cites {sorted(missing)} but note cites {sorted(note_paths)}"))

    for name in note_by_file:
        if name not in indexed:
            out.append(Finding(Severity.WARN, "index", name, None,
                               "note is not listed in MEMORY.md (invisible to always-on context)"))
    return out

@register
def check_staleness(ctx: LintContext) -> list[Finding]:
    out: list[Finding] = []
    for n in ctx.notes:
        m = CURRENCY_MARKERS.search(n.body)
        if not m:
            continue
        # Age from the newest date written in the body when there is one mtime
        # resets on any incidental edit, but a maintained note gets fresh dates.
        # Fall back to mtime for undated notes.
        dated = newest_body_date(n.body)
        anchor = dated if dated is not None else n.mtime
        age_days = (ctx.now - anchor) / 86400
        if age_days <= ctx.stale_days:
            continue
        basis = (f"newest body date {time.strftime('%Y-%m-%d', time.localtime(anchor))}"
                 if dated is not None else "file mtime")
        out.append(Finding(Severity.REVIEW, "staleness", n.path.name, None,
                           f"{int(age_days)}d old ({basis}) but body still says "
                           f"'{m.group(0)}' — verify it's still true"))
    return out

def lint(ctx: LintContext) -> list[Finding]:
    findings: list[Finding] = []
    for check in CHECKS:
        findings.extend(check(ctx))
    return findings

# discovery + reporting
def discover_memory_dir(arg: str | None) -> Path:
    if arg:
        p = Path(os.path.expanduser(arg))
        if not p.is_dir():
            sys.exit(f"mem-lint: not a directory: {p}")
        return p
    matches = [m for m in sorted(Path(os.path.expanduser("~/.claude/projects")).glob("*/memory")) if m.is_dir()]
    if not matches:
        sys.exit("mem-lint: no ~/.claude/projects/*/memory found; pass a path explicitly")
    if len(matches) > 1:
        listing = "\n  ".join(str(m) for m in matches)
        sys.exit("mem-lint: multiple memory dirs found; pass one explicitly:\n  " + listing)
    return matches[0]

def build_context(memdir: Path, now: float, stale_days: int) -> LintContext:
    note_paths = sorted(p for p in memdir.glob("*.md") if p.name != "MEMORY.md")
    notes = [load_note(p) for p in note_paths]
    index_path = memdir / "MEMORY.md"
    index = load_document(index_path) if index_path.exists() else Document(index_path, [], 1)
    return LintContext(memdir=memdir, notes=notes, index=index, now=now, stale_days=stale_days)

SEV_ORDER = (Severity.ERROR, Severity.WARN, Severity.REVIEW)
SEV_COLOR = {Severity.ERROR: "31", Severity.WARN: "33", Severity.REVIEW: "36"}

def color(s: str, code: str, enable: bool) -> str:
    return f"\033[{code}m{s}\033[0m" if enable else s

def report_hook(findings: list[Finding], memdir: Path) -> None:
    """SessionStart-hook mode: surface only ERRORs, as a systemMessage, silent when clean."""
    errs = [f for f in findings if f.severity == Severity.ERROR]
    if not errs:
        return
    lines = [f"⚠ mem-lint: memory rot in {memdir}", ""]
    for f in sorted(errs, key=lambda x: (x.file, x.line or 0)):
        loc = f"{f.file}:{f.line}" if f.line else f.file
        lines.append(f"  [{f.check}] {loc} — {f.message}")
    warn = sum(1 for f in findings if f.severity == Severity.WARN)
    if warn:
        lines += ["", f"  (+{warn} hygiene warnings — run `mem-lint` for the full report)"]
    print(json.dumps({"systemMessage": "\n".join(lines)}))

def report_json(findings: list[Finding], ctx: LintContext) -> None:
    payload = {
        "memory_dir": str(ctx.memdir),
        "note_count": len(ctx.notes),
        "findings": [
            {"severity": f.severity.name, "check": f.check, "file": f.file,
             "line": f.line, "message": f.message}
            for f in findings
        ],
    }
    print(json.dumps(payload, indent=2))

def report_human(findings: list[Finding], ctx: LintContext, use_color: bool) -> None:
    print(color(f"mem-lint  {ctx.memdir}", "1", use_color))
    print(f"{len(ctx.notes)} notes scanned\n")
    by_sev: dict[Severity, list[Finding]] = {s: [] for s in SEV_ORDER}
    for f in findings:
        by_sev.setdefault(f.severity, []).append(f)
    for sev in SEV_ORDER:
        group = by_sev.get(sev, [])
        if not group:
            continue
        print(color(f"── {sev.name} ({len(group)})", SEV_COLOR.get(sev, "0"), use_color))
        for f in sorted(group, key=lambda x: (x.file, x.line or 0)):
            loc = f"{f.file}:{f.line}" if f.line else f.file
            tag = color(f"[{f.check}]", "2", use_color)
            print(f"  {tag} {loc}\n      {f.message}")
        print()
    c = {s: len(by_sev.get(s, [])) for s in SEV_ORDER}
    print(color(f"summary: {c[Severity.ERROR]} error, {c[Severity.WARN]} warn, "
                f"{c[Severity.REVIEW]} review", "1", use_color))

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mem-lint", description=__doc__.splitlines()[0])
    ap.add_argument("path", nargs="?", help="memory directory (default: autodiscover)")
    ap.add_argument("--stale-days", type=int, default=30, help="staleness threshold (default 30)")
    ap.add_argument("--hook", action="store_true",
                    help="SessionStart-hook mode: emit {\"systemMessage\": ...} only when "
                         "ERRORs exist, stay silent when clean, always exit 0")
    ap.add_argument("--json", action="store_true", help="emit findings as JSON")
    ap.add_argument("--no-fail", action="store_true", help="always exit 0")
    args = ap.parse_args(argv)

    use_color = sys.stdout.isatty() and not args.json
    memdir = discover_memory_dir(args.path)
    ctx = build_context(memdir, now=time.time(), stale_days=args.stale_days)
    findings = lint(ctx)

    if args.hook:
        report_hook(findings, memdir)
        return 0

    if args.json:
        report_json(findings, ctx)
    else:
        report_human(findings, ctx, use_color)

    errors = sum(1 for f in findings if f.severity == Severity.ERROR)
    return 0 if args.no_fail else (1 if errors else 0)

if __name__ == "__main__":
    raise SystemExit(main())
