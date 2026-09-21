"""Whip-View: small, dependency-free live dashboard for local Codex task runs."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse


HOST = "127.0.0.1"
PORT = 8765
REPO_ROOT = Path.cwd()
TASK_DIR = REPO_ROOT / ".codex-tasks"
ANSI_RE = re.compile(r"\x1b(?:[@-_][0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
PROMPT_RE = re.compile(r"^(?P<id>[^-]+)-(?P<slug>.+)\.md$", re.IGNORECASE)


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def first_heading(prompt: str, fallback: str) -> str:
    for line in prompt.splitlines():
        match = re.match(r"^\s*#{1,6}\s+(.+?)\s*$", line)
        if match:
            return match.group(1).strip().strip("#").strip()
    return fallback.replace("-", " ").strip().title()


def human_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def iso_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def prompt_files() -> list[tuple[str, str, Path]]:
    found: list[tuple[str, str, Path]] = []
    try:
        paths = TASK_DIR.iterdir()
    except OSError:
        return found
    for path in paths:
        if not path.is_file() or path.name.lower().endswith("-report.md"):
            continue
        match = PROMPT_RE.match(path.name)
        if match:
            found.append((match.group("id"), match.group("slug"), path))
    return found


def parse_log(log_path: Path) -> tuple[str, str, int | None]:
    text = strip_ansi(read_text(log_path))
    model_match = re.search(r"(?im)^\s*model:\s*(.+?)\s*$", text)
    effort_match = re.search(r"(?im)^\s*reasoning effort:\s*(.+?)\s*$", text)
    tokens = None
    token_matches = re.findall(
        r"(?im)^\s*tokens used\s*$\s*^\s*([\d,]+)\s*$", text
    )
    if token_matches:
        try:
            tokens = int(token_matches[-1].replace(",", ""))
        except ValueError:
            pass
    return (
        model_match.group(1).strip() if model_match else "",
        effort_match.group(1).strip() if effort_match else "",
        tokens,
    )


def load_orchestrator() -> dict:
    path = TASK_DIR / "status.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def git_commits() -> list[dict[str, str]]:
    try:
        result = subprocess.run(
            ["git", "log", "-5", "--format=%h|%ar|%s"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode:
        return []
    commits = []
    for line in result.stdout.splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3:
            commits.append({"hash": parts[0], "age": parts[1], "subject": parts[2]})
    return commits


def task_state() -> list[dict]:
    now = time.time()
    tasks = []
    for task_id, slug, prompt_path in prompt_files():
        log_path = TASK_DIR / f"{task_id}-log.txt"
        report_path = TASK_DIR / f"{task_id}-report.md"
        prompt_stat = prompt_path.stat()
        status = "queued"
        relevant_path = prompt_path
        start_time = prompt_stat.st_mtime
        end_time = now
        if report_path.is_file():
            status = "done"
            relevant_path = report_path
            end_time = report_path.stat().st_mtime
        elif log_path.is_file():
            relevant_path = log_path
            log_stat = log_path.stat()
            status = "running" if now - log_stat.st_mtime < 600 else "stalled"
        model, effort, tokens = parse_log(log_path) if log_path.is_file() else ("", "", None)
        log_size = log_path.stat().st_size if log_path.is_file() else 0
        tasks.append(
            {
                "id": task_id,
                "title": first_heading(read_text(prompt_path), slug),
                "status": status,
                "model": model,
                "effort": effort,
                "tokens": tokens,
                "log_size": log_size,
                "last_modified": iso_time(relevant_path.stat().st_mtime),
                "elapsed": human_duration(end_time - start_time),
                "_sort": relevant_path.stat().st_mtime,
            }
        )
    tasks.sort(key=lambda task: task.pop("_sort"), reverse=True)
    return tasks


def log_tail(path: Path, count: int = 80) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            return strip_ansi("".join(deque(stream, maxlen=count)))
    except OSError:
        return ""


def task_detail(task_id: str) -> dict | None:
    for candidate_id, _slug, prompt_path in prompt_files():
        if candidate_id == task_id:
            return {
                "id": candidate_id,
                "prompt": read_text(prompt_path),
                "report": read_text(TASK_DIR / f"{candidate_id}-report.md"),
                "log": log_tail(TASK_DIR / f"{candidate_id}-log.txt"),
            }
    return None


HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Codex Task Dashboard</title><style>
:root{color-scheme:dark;--bg:#0d1117;--panel:#161b22;--line:#30363d;--muted:#8b949e;--text:#e6edf3;--blue:#58a6ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 system-ui,-apple-system,sans-serif}
main{max-width:1250px;margin:auto;padding:24px}h1,h2,h3{margin:.2em 0 .6em}h1{font-size:22px}h2{font-size:17px}.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:16px;margin-bottom:16px}
#next{margin:.4em 0 0;padding-left:20px}.muted{color:var(--muted)}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line)}th{color:var(--muted);font-size:12px;text-transform:uppercase}tbody tr{cursor:pointer}tbody tr:hover{background:#1f2630}
.badge{display:inline-block;padding:2px 8px;border-radius:99px;font-weight:650}.done{background:#183d27;color:#56d364}.running{background:#153b55;color:#79c0ff;animation:pulse 1.5s infinite}.stalled{background:#4b3515;color:#e3b341}.queued{background:#30363d;color:#b1bac4}@keyframes pulse{50%{opacity:.48}}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.wide{grid-column:1/-1}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#0b0f14;border:1px solid var(--line);border-radius:6px;padding:12px;max-height:360px;overflow:auto;margin:0;font:12px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace}.hidden{display:none}.commit{padding:7px 0;border-bottom:1px solid var(--line)}.hash{color:var(--blue);font-family:monospace}.error{color:#ff7b72}@media(max-width:760px){main{padding:12px}.grid{grid-template-columns:1fr}.wide{grid-column:auto}.optional{display:none}table{font-size:12px}th,td{padding:7px 5px}}
</style></head><body><main>
<h1>Codex Task Dashboard</h1>
<section class="card"><h2>Claude: <span id="step">—</span></h2><div class="muted">Updated <span id="updated">—</span></div><ul id="next"></ul><div id="notes"></div></section>
<section class="card"><h2>Tasks</h2><table><thead><tr><th>ID</th><th>Task</th><th>Status</th><th class="optional">Model / effort</th><th>Tokens</th><th class="optional">Log</th><th>Elapsed</th></tr></thead><tbody id="tasks"></tbody></table></section>
<section id="detail" class="card hidden"><h2 id="detail-title">Task</h2><div class="grid"><div><h3>Prompt</h3><pre id="prompt"></pre></div><div id="report-wrap"><h3>Report</h3><pre id="report"></pre></div><div class="wide"><h3>Live log (last 80 lines)</h3><pre id="log"></pre></div></div></section>
<section class="card"><h2>Recent commits</h2><div id="commits" class="muted">Loading…</div></section>
</main><script>
let selected=null;
const $=id=>document.getElementById(id); const setText=(id,value)=>{$(id).textContent=value??''};
function fmtBytes(n){if(n<1024)return n+' B';if(n<1048576)return (n/1024).toFixed(1)+' KB';return (n/1048576).toFixed(1)+' MB'}
function fmtTime(value){if(!value)return '—';const d=new Date(value);return Number.isNaN(d.valueOf())?value:d.toLocaleString()}
async function json(url){const response=await fetch(url,{cache:'no-store'});if(!response.ok)throw new Error(response.status+' '+response.statusText);return response.json()}
async function loadState(){try{const data=await json('/api/state'),o=data.orchestrator||{};setText('step',o.claude_step||'—');setText('updated',fmtTime(o.updated));setText('notes',o.notes||'');
const next=$('next');next.replaceChildren();for(const item of Array.isArray(o.next)?o.next:[]){const li=document.createElement('li');li.textContent=item;next.append(li)}
const body=$('tasks');body.replaceChildren();for(const t of data.tasks||[]){const tr=document.createElement('tr');tr.title='Last modified: '+fmtTime(t.last_modified);tr.addEventListener('click',()=>openTask(t.id,t.title));
const values=[t.id,t.title,t.status,(t.model||'—')+(t.effort?' / '+t.effort:''),t.tokens==null?'—':t.tokens.toLocaleString(),fmtBytes(t.log_size),t.elapsed];values.forEach((value,i)=>{const td=document.createElement('td');if(i===2){const b=document.createElement('span');b.className='badge '+t.status;b.textContent=t.status;td.append(b)}else td.textContent=value;if(i===3||i===5)td.className='optional';tr.append(td)});body.append(tr)}
const commits=$('commits');commits.replaceChildren();if(!(data.commits||[]).length)commits.textContent='No git history available.';for(const c of data.commits||[]){const div=document.createElement('div');div.className='commit';const hash=document.createElement('span');hash.className='hash';hash.textContent=c.hash;div.append(hash,document.createTextNode(' · '+c.age+' · '+c.subject));commits.append(div)}}catch(error){setText('step','Dashboard error: '+error.message);$('step').className='error'}}
async function openTask(id,title){selected=id;$('detail').classList.remove('hidden');setText('detail-title',id+' — '+(title||''));try{const data=await json('/api/task/'+encodeURIComponent(id));setText('prompt',data.prompt);setText('report',data.report);$('report-wrap').style.display=data.report?'block':'none';const log=$('log'),atBottom=log.scrollHeight-log.scrollTop-log.clientHeight<30;log.textContent=data.log||'';if(atBottom||!log.dataset.loaded)log.scrollTop=log.scrollHeight;log.dataset.loaded='1'}catch(error){setText('log','Dashboard error: '+error.message)}}
loadState();setInterval(loadState,3000);setInterval(()=>{if(selected)openTask(selected,$('detail-title').textContent.replace(/^.*? — /,''))},3000);
</script></body></html>'''


class DashboardHandler(BaseHTTPRequestHandler):
    def send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, value: object, status: int = 200) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_bytes(body, "application/json; charset=utf-8", status)

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        path = urlparse(self.path).path
        if path == "/":
            self.send_bytes(HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/state":
            self.send_json(
                {
                    "orchestrator": load_orchestrator(),
                    "tasks": task_state(),
                    "commits": git_commits(),
                }
            )
        elif path.startswith("/api/task/"):
            task_id = unquote(path[len("/api/task/") :])
            detail = task_detail(task_id)
            if detail is None:
                self.send_json({"error": "Task not found"}, 404)
            else:
                self.send_json(detail)
        else:
            self.send_json({"error": "Not found"}, 404)

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    global REPO_ROOT, TASK_DIR, PORT
    ap = argparse.ArgumentParser(description="Live dashboard for Codex task runs.")
    ap.add_argument("repo", nargs="?", default=".", help="project root (default: cwd)")
    ap.add_argument("--tasks-dir", help="task folder (default: <repo>/.codex-tasks)")
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()
    REPO_ROOT = Path(args.repo).resolve()
    TASK_DIR = Path(args.tasks_dir).resolve() if args.tasks_dir else REPO_ROOT / ".codex-tasks"
    PORT = args.port
    server = ThreadingHTTPServer((HOST, PORT), DashboardHandler)
    print(f"http://{HOST}:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
