# Whip-View

Tiny live dashboard for watching local [Codex CLI](https://github.com/openai/codex)
task runs driven by an orchestrator (e.g. Claude Code). Python stdlib only.

## Requirements

Python 3.9+ — standard library only, nothing to install (see `requirements.txt`).
Optional: `git` on PATH for the recent-commits panel.

## Run

**Windows:** double-click `whipview.cmd`, or `whipview.cmd <project-root>`.
**Linux / macOS / Git Bash:** `./whipview.sh <project-root>`

The browser opens at http://127.0.0.1:8765 automatically.

Default project: put its path on the first line of `whipview.local` (git-ignored,
per machine) so a plain double-click watches it. Otherwise the current folder is used.

Options: `--port N`, `--tasks-dir path/to/tasks`, `--no-browser`.
Direct: `python whipview.py <project-root> [options]`.

## Task folder convention

| File | Meaning |
|------|---------|
| `<ID>-<slug>.md` | task prompt given to Codex |
| `<ID>-log.txt` | Codex output log (`codex exec ... > <ID>-log.txt`) |
| `<ID>-report.md` | Codex final message (`codex exec -o <ID>-report.md`) |
| `status.json` | orchestrator status: `{updated, claude_step, next: [...], notes}` |

Status per task: `done` (report exists) · `running` (log updated < 10 min ago) ·
`stalled` · `queued` (prompt only).

The page shows the orchestrator's current step, the queued duties in run order,
Claude and Grok sessions for this project, all Codex tasks with model / effort /
tokens / elapsed / start–end clock, a live log tail per task, and the last git commits. Run order is the `next` list in
`status.json` (the first entry runs first). Queued prompts missing from that
list follow afterwards, oldest prompt first.
The clock is the log file's creation time through the report's finish time
(`21.09.2026 14:59–15:14`). A run that crosses midnight repeats the date on the
end (`21.09.2026 23:50–22.09.2026 00:18`). A run that is still open shows the
start only (`21.09.2026 14:59–`). A queued prompt has no clock yet.

## Plan usage

The Usage card shows Claude and Codex plan consumption for the 5-hour and weekly
windows, reset countdowns, and freshness. It refreshes every 10 seconds. Codex
usage is read from the newest local session rollout; Claude usage is supplied by
the included statusline helper.

Enable it by adding this to `~/.claude/settings.json` (replace `<path>` with this
repository's absolute path):

```json
"statusLine": {"type": "command", "command": "python <path>/claude_statusline.py"}
```

The dashboard only reads these files. Override their locations with
`WHIPVIEW_CODEX_HOME` and `WHIPVIEW_CLAUDE_USAGE` if needed.

## Other sessions

Codex task runs come from `.codex-tasks`. The Oturumlar section adds sessions
that other assistants held in this same project folder:

| Assistant | Where it is read |
|-----------|------------------|
| Claude Code | `~/.claude/projects/<project>/*.jsonl` |
| Grok | `~/.grok/sessions/*/*/summary.json` whose `cwd` is this project |

A session updated in the last 10 minutes is `running`; otherwise it is `done`.
Click a row for the prompts and the latest reply or recap. Override the roots
with `WHIPVIEW_CLAUDE_HOME` and `WHIPVIEW_GROK_HOME`.
