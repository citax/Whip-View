# Whip-View

Tiny live dashboard for watching local [Codex CLI](https://github.com/openai/codex)
task runs driven by an orchestrator (e.g. Claude Code). Python stdlib only.

## Run

```bash
python whipview.py <project-root>            # watches <project-root>/.codex-tasks
python whipview.py . --tasks-dir path/to/tasks --port 8765
```

Open http://127.0.0.1:8765

## Task folder convention

| File | Meaning |
|------|---------|
| `<ID>-<slug>.md` | task prompt given to Codex |
| `<ID>-log.txt` | Codex output log (`codex exec ... > <ID>-log.txt`) |
| `<ID>-report.md` | Codex final message (`codex exec -o <ID>-report.md`) |
| `status.json` | orchestrator status: `{updated, claude_step, next: [...], notes}` |

Status per task: `done` (report exists) · `running` (log updated < 10 min ago) ·
`stalled` · `queued` (prompt only).

The page shows the orchestrator's current step, all tasks with model / effort /
tokens / elapsed, a live log tail per task, and the last git commits.
