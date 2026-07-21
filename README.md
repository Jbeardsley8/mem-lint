# mem-lint

A linter for Claude Code memory directories. It scans a `.../memory/`
folder (the `MEMORY.md` index plus per-fact `*.md` notes) and reports
rot that's otherwise invisible until a note misleads you.

## Usage

```bash
mem-lint                    # autodiscover ~/.claude/projects/*/memory
mem-lint path/to/memory     # or point it at a directory
mem-lint --json             # machine-readable output
mem-lint --hook             # SessionStart-hook mode (see below)
```

No dependencies. Python 3.9+.

## What it checks

| Severity | Checks |
|----------|--------|
| **ERROR**  | dead filesystem paths, dangling index entries, invalid frontmatter |
| **WARN**   | index/note disagreement, orphan notes, dangling wikilinks, schema drift |
| **REVIEW** | staleness — old notes still claiming to be "current" |

Exits non-zero when any ERROR is found (unless `--no-fail`).

## As a hook

`--hook` runs it as a SessionStart hook: it surfaces only ERRORs as a
`systemMessage`, stays silent when clean, and always exits 0.

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [{ "type": "command", "command": "mem-lint --hook" }] }
    ]
  }
}
```

## Options

- `--stale-days N` — staleness threshold (default 30)
- `--json` — emit findings as JSON
- `--hook` — hook mode (ERRORs only, systemMessage, always exit 0)
- `--no-fail` — always exit 0
