"""Whip-View: small, dependency-free live dashboard for local Codex task runs."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import threading
import time
import webbrowser
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
USAGE_TAIL_BYTES = 512 * 1024


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
        paths = list(TASK_DIR.iterdir())  # iterdir is lazy: list() surfaces a missing dir here
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


def usage_window(value: object, percent_key: str) -> dict[str, float | int | None]:
    source = value if isinstance(value, dict) else {}
    percent = source.get(percent_key)
    resets_at = source.get("resets_at")
    try:
        percent = float(percent) if percent is not None else None
    except (TypeError, ValueError):
        percent = None
    try:
        resets_at = float(resets_at) if resets_at is not None else None
    except (TypeError, ValueError):
        resets_at = None
    reset = resets_at is not None and resets_at <= time.time()
    if reset:
        # The window rolled over after this sample was written: usage is 0 now,
        # not the (possibly 100 %) value recorded before the reset.
        percent, resets_at = 0.0, None
    return {
        "used_percent": percent,
        "resets_at": resets_at,
        "resets_in_seconds": max(0, int(resets_at - time.time())) if resets_at is not None else None,
        "reset": reset,
    }


def empty_usage(error: str) -> dict:
    return {"five_hour": usage_window({}, "used_percent"), "weekly": usage_window({}, "used_percent"), "updated": None, "error": error}


def claude_usage() -> dict:
    configured = os.environ.get("WHIPVIEW_CLAUDE_USAGE")
    path = Path(configured).expanduser() if configured else Path.home() / ".claude" / "whipview-usage.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("usage data is not a JSON object")
        limits = data.get("rate_limits")
        if not isinstance(limits, dict):
            raise ValueError("rate_limits is missing")
        return {"five_hour": usage_window(limits.get("five_hour"), "used_percentage"), "weekly": usage_window(limits.get("seven_day"), "used_percentage"), "updated": data.get("updated"), "error": None}
    except FileNotFoundError:
        return empty_usage("no data yet — open Claude Code with the statusline enabled")
    except (OSError, ValueError) as error:
        return empty_usage(f"could not read Claude usage: {error}")


def tail_text(path: Path, size: int = USAGE_TAIL_BYTES) -> str:
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        stream.seek(max(0, stream.tell() - size))
        return stream.read().decode("utf-8", errors="replace")


def codex_usage() -> dict:
    configured = os.environ.get("WHIPVIEW_CODEX_HOME")
    home = Path(configured).expanduser() if configured else Path.home() / ".codex"
    try:
        files = sorted((home / "sessions").rglob("rollout-*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError as error:
        return empty_usage(f"could not scan Codex sessions: {error}")
    if not files:
        return empty_usage("no Codex usage data found")
    read_errors = []
    for path in files:
        try:
            for line in reversed(tail_text(path).splitlines()):
                if '"rate_limits"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = event.get("payload") if isinstance(event, dict) else None
                limits = payload.get("rate_limits") if isinstance(payload, dict) else None
                # When the limit is hit Codex appends a record with
                # primary/secondary = null; skip it and keep the last real
                # sample (which shows 100 %).
                if isinstance(limits, dict) and (
                    isinstance(limits.get("primary"), dict) or isinstance(limits.get("secondary"), dict)
                ):
                    return {"five_hour": usage_window(limits.get("primary"), "used_percent"), "weekly": usage_window(limits.get("secondary"), "used_percent"), "updated": path.stat().st_mtime, "error": None}
        except OSError as error:
            read_errors.append(str(error))
    error = "no rate limit data found in Codex sessions"
    if read_errors:
        error += f" ({read_errors[0]})"
    return empty_usage(error)


def usage_state() -> dict:
    return {"claude": claude_usage(), "codex": codex_usage()}


HTML = r'''<!doctype html>
<html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Whip-View</title><style>
:root{color-scheme:dark;--bg:#0d1117;--panel:#161b22;--line:#30363d;--muted:#8b949e;--text:#e6edf3;--blue:#58a6ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 system-ui,-apple-system,sans-serif}
main{max-width:1250px;margin:auto;padding:24px}h1,h2,h3{margin:.2em 0 .6em}h1{font-size:22px}h2{font-size:17px}.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:16px;margin-bottom:16px}
.usage-row{display:grid;grid-template-columns:90px 1fr 1fr;gap:14px;align-items:center;padding:10px 0;border-top:1px solid var(--line)}.usage-row:first-of-type{border-top:0}.usage-name{font-weight:700}.usage-window{min-width:0}.usage-meta{display:flex;justify-content:space-between;gap:8px;margin-bottom:5px;font-size:12px}.usage-track{height:9px;background:#30363d;border-radius:99px;overflow:hidden}.usage-fill{height:100%;width:0;background:#3fb950;transition:width .25s}.usage-fill.amber{background:#d29922}.usage-fill.red{background:#f85149}.usage-status{grid-column:2/-1;font-size:12px;color:var(--muted)}.stale{color:#d29922}.usage-error{color:#ff7b72}
#next{margin:.4em 0 0;padding-left:20px}.muted{color:var(--muted)}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line)}th{color:var(--muted);font-size:12px;text-transform:uppercase}tbody tr{cursor:pointer}tbody tr:hover{background:#1f2630}
.badge{display:inline-block;padding:2px 8px;border-radius:99px;font-weight:650}.done{background:#183d27;color:#56d364}.running{background:#153b55;color:#79c0ff;animation:pulse 1.5s infinite}.stalled{background:#4b3515;color:#e3b341}.queued{background:#30363d;color:#b1bac4}@keyframes pulse{50%{opacity:.48}}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.wide{grid-column:1/-1}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#0b0f14;border:1px solid var(--line);border-radius:6px;padding:12px;max-height:360px;overflow:auto;margin:0;font:12px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace}.hidden{display:none}.commit{padding:7px 0;border-bottom:1px solid var(--line)}.hash{color:var(--blue);font-family:monospace}.error{color:#ff7b72}@media(max-width:760px){main{padding:12px}.usage-row{grid-template-columns:70px 1fr}.usage-window,.usage-status{grid-column:2}.grid{grid-template-columns:1fr}.wide{grid-column:auto}.optional{display:none}table{font-size:12px}th,td{padding:7px 5px}}
</style><style>
:root{color-scheme:dark;--bg:#0b0d10;--surface:#12151a;--surface-2:#181c22;--border:#232830;--text:#e6e8eb;--muted:#8b93a1;--accent:#7c9cff;--ok:#3fb950;--warn:#d29922;--bad:#f85149;--run:#58a6ff}
@media(prefers-color-scheme:light){:root{color-scheme:light;--bg:#f7f8fa;--surface:#fff;--surface-2:#f1f3f6;--border:#dfe3e8;--text:#20242b;--muted:#697181;--accent:#526fd6}}
*{box-sizing:border-box}html,body{min-width:0}body{background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;font-variant-numeric:tabular-nums}main{max-width:1200px;padding:0 24px 32px}.topbar{height:64px;display:flex;align-items:center;gap:10px;margin-bottom:18px;border-bottom:1px solid var(--border)}.brand{font-size:16px;font-weight:680;letter-spacing:-.02em}.folder{color:var(--muted);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.live-dot{width:7px;height:7px;border-radius:50%;background:var(--ok);margin-left:auto;box-shadow:0 0 0 0 color-mix(in srgb,var(--ok) 35%,transparent);animation:live 2s infinite}.live-dot.offline{background:var(--bad);animation:none}@keyframes live{50%{box-shadow:0 0 0 5px transparent}}h2{font-size:13px;letter-spacing:.01em;margin:0 0 14px}.card{background:var(--surface);border-color:var(--border);border-radius:10px;padding:16px;margin-bottom:14px;box-shadow:none}.usage-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:1px;background:var(--border);border:1px solid var(--border);border-radius:8px;overflow:hidden}.usage-cell{background:var(--surface);padding:13px}.usage-head{display:flex;justify-content:space-between;color:var(--muted);font-size:12px}.usage-pct{font:600 24px/1.25 ui-monospace,"Cascadia Code",Consolas,monospace;margin:5px 0 8px}.usage-track{height:6px;background:var(--surface-2)}.usage-fill{background:var(--ok)}.usage-fill.amber{background:var(--warn)}.usage-fill.red{background:var(--bad)}.usage-reset,.usage-message{color:var(--muted);font-size:11px;margin-top:7px}.usage-cell.stale .usage-fill{background:var(--muted)}.usage-cell.stale .usage-pct{color:var(--muted)}.orch-head{display:flex;justify-content:space-between;gap:16px}.eyebrow{color:var(--muted);font-size:11px;margin-bottom:3px}.current-step{font-size:18px;font-weight:620;letter-spacing:-.02em}.updated{color:var(--muted);font-size:11px;text-align:right;white-space:nowrap}#next{counter-reset:item;list-style:none;padding:0;margin:14px 0 0;border-top:1px solid var(--border)}#next li{counter-increment:item;padding:7px 0;color:var(--muted)}#next li:before{content:counter(item);display:inline-grid;place-items:center;width:18px;height:18px;margin-right:8px;border:1px solid var(--border);border-radius:5px;font:10px ui-monospace,monospace;color:var(--text)}#notes{margin-top:8px;color:var(--muted)}.table-wrap{overflow:hidden}table{table-layout:fixed}th,td{padding:9px 8px;border-color:var(--border);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}th{font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.06em}tbody tr:hover{background:var(--surface-2)}.mono,td:first-child,td:nth-child(6),td:nth-child(7){font-family:ui-monospace,"Cascadia Code",Consolas,monospace}.badge{font-size:11px;padding:2px 7px}.done{background:color-mix(in srgb,var(--ok) 16%,transparent);color:var(--ok)}.running{background:color-mix(in srgb,var(--run) 16%,transparent);color:var(--run)}.stalled{background:color-mix(in srgb,var(--warn) 16%,transparent);color:var(--warn)}.queued{background:var(--surface-2);color:var(--muted)}.empty{color:var(--muted);padding:5px 0}.drawer-backdrop{position:fixed;inset:0;background:#0008;z-index:9}.drawer{position:fixed;z-index:10;inset:0 0 0 auto;width:min(680px,92vw);background:var(--surface);border-left:1px solid var(--border);padding:18px;display:flex;flex-direction:column;box-shadow:-16px 0 50px #0004}.drawer.hidden,.drawer-backdrop.hidden{display:none}.drawer-head{display:flex;align-items:center;gap:10px}.drawer-head h2{margin:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.close{margin-left:auto;border:0;background:transparent;color:var(--muted);font-size:24px;cursor:pointer}.tabs{display:flex;gap:3px;border-bottom:1px solid var(--border);margin:15px 0}.tab{border:0;background:transparent;color:var(--muted);padding:8px 10px;cursor:pointer}.tab.active{color:var(--text);box-shadow:inset 0 -2px var(--accent)}.pane{min-height:0;flex:1;overflow:auto}.pane pre{height:100%;max-height:none;background:var(--bg);border-color:var(--border);border-radius:8px}.commit{display:grid;grid-template-columns:70px minmax(0,1fr) auto;gap:10px;border-color:var(--border);padding:7px 0}.commit-subject{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.commit-age{color:var(--muted);font-size:12px}.hash{color:var(--accent)}
html,body,main,.card,.usage-grid,.usage-cell,.orch-head,.drawer,.pane,table{max-width:100%;min-width:0}.usage-grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;background:transparent;border:0;overflow:visible}.usage-cell{min-width:0;background:var(--surface-2);border-radius:8px}.usage-head{align-items:baseline;gap:10px}.usage-head span:first-child{font-size:11px;font-weight:650}.usage-head span:last-child{font-size:11px}.usage-main{display:flex;align-items:baseline;justify-content:space-between;gap:8px}.usage-pct{margin:5px 0}.usage-updated{color:var(--muted);font-size:10px;text-align:right;overflow-wrap:anywhere}.usage-track{background:color-mix(in srgb,var(--muted) 18%,transparent);border-radius:99px;overflow:hidden}.usage-fill{height:100%;border-radius:inherit}.current-step,#notes,#next li,.commit-subject,.pane pre{overflow-wrap:anywhere}.task-title{min-width:0;overflow:hidden;text-overflow:ellipsis}.folder{max-width:40vw}
@media(max-width:640px){body{overflow-x:hidden}main{width:100%;padding:0 16px 24px;overflow:hidden}.topbar{height:56px;min-width:0}.usage-grid{grid-template-columns:minmax(0,1fr)}.usage-cell{padding:12px}.usage-pct{font-size:21px}table,tbody{display:block;width:100%}thead{display:none}tbody tr{display:grid;grid-template-columns:auto minmax(0,1fr) auto auto;width:100%;max-width:100%;padding:10px 4px;border-bottom:1px solid var(--border);align-items:center}td{display:block;min-width:0;border:0;padding:2px 5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}td:before{content:none}td:nth-child(1){grid-column:1}td:nth-child(2){grid-column:2/4}.task-title{font-weight:550}td:nth-child(3){grid-column:4}td:nth-child(n+4){grid-row:2;color:var(--muted);font-size:11px}td:nth-child(4){grid-column:1}td:nth-child(5){grid-column:2}td:nth-child(6){grid-column:3}td:nth-child(7){grid-column:4}td:nth-child(n+4):not(:last-child):after{content:' ·';color:var(--muted)}.drawer{width:100%;border-left:0}.commit{grid-template-columns:58px minmax(0,1fr)}.commit-age{grid-column:2}.orch-head{display:block}.updated{text-align:left;margin-top:8px}}
</style></head><body><main>
<header class="topbar"><span class="brand">Whip-View</span><span class="folder">Whip-View</span><span id="live" class="live-dot" title="Canlı"></span></header>
<section class="card"><h2>Kullanım</h2><div id="usage" class="usage-grid"></div></section>
<section class="card"><h2>Claude: <span id="step">—</span></h2><div class="muted">Updated <span id="updated">—</span></div><ul id="next"></ul><div id="notes"></div></section>
<section class="card table-wrap"><h2>Görevler</h2><table><thead><tr><th style="width:8%">ID</th><th>Başlık</th><th style="width:11%">Durum</th><th style="width:14%">Model</th><th style="width:9%">Efor</th><th style="width:10%">Token</th><th style="width:10%">Süre</th></tr></thead><tbody id="tasks"></tbody></table></section>
<div id="backdrop" class="drawer-backdrop hidden"></div><aside id="detail" class="drawer hidden" aria-label="Görev detayı"><div class="drawer-head"><h2 id="detail-title">Görev</h2><button id="close" class="close" aria-label="Kapat">×</button></div><div class="tabs"><button class="tab active" data-pane="prompt">Prompt</button><button class="tab" data-pane="report">Rapor</button><button class="tab" data-pane="log">Log</button></div><div class="pane"><pre id="prompt"></pre><pre id="report" class="hidden"></pre><pre id="log" class="hidden"></pre></div></aside>
<section class="card"><h2>Recent commits</h2><div id="commits" class="muted">Loading…</div></section>
</main><script>
let selected=null,stateSignature='';
const $=id=>document.getElementById(id);
const setText=(id,value)=>{$(id).textContent=value??''};
const labels=['ID','Başlık','Durum','Model','Efor','Token','Süre'];
const stepCard=$('step').closest('.card');
stepCard.innerHTML='<div class="orch-head"><div><div class="eyebrow">Şu an</div><div id="step" class="current-step">—</div></div><div class="updated">Güncellendi <span id="updated">—</span></div></div><div class="eyebrow" style="margin-top:14px">Sıradaki</div><ol id="next"></ol><div id="notes"></div>';
$('commits').previousElementSibling.textContent='Son commitler';
$('commits').textContent='Yükleniyor…';
async function json(url){const response=await fetch(url,{cache:'no-store'});if(!response.ok)throw new Error(response.status+' '+response.statusText);return response.json()}
function duration(seconds){if(seconds==null)return '—';seconds=Math.max(0,Math.floor(seconds));const d=Math.floor(seconds/86400),h=Math.floor(seconds%86400/3600),m=Math.floor(seconds%3600/60),s=seconds%60;if(d)return d+'g '+h+'s';if(h)return h+'s '+m+'dk';if(m)return m+'dk '+s+'sn';return s+'sn'}
function ago(timestamp){if(timestamp==null)return '';const seconds=Math.max(0,Date.now()/1000-Number(timestamp));return seconds<60?'şimdi güncellendi':Math.floor(seconds/60)+' dk önce güncellendi'}
function fmtTime(value){if(!value)return '—';const d=new Date(value);return Number.isNaN(d.valueOf())?value:d.toLocaleString('tr-TR')}
function formatTokens(n){return n==null?'—':n>=1e6?(n/1e6).toFixed(1).replace('.0','')+'m':n>=1e3?(n/1e3).toFixed(1).replace('.0','')+'k':String(n)}
function trDuration(value){return String(value||'—').replace(/(\d+)d/g,'$1g').replace(/(\d+)h/g,'$1s').replace(/(\d+)m/g,'$1dk')}
function stripTaskTitle(title,id){const safe=String(id).replace(/[.*+?^${}()|[\]\\]/g,'\\$&');return String(title||'').replace(new RegExp('^Task\\s+'+safe+'\\s+[—-]\\s*','i'),'')}
function updateUsageTimes(){document.querySelectorAll('[data-reset-at]').forEach(node=>{const at=Number(node.dataset.resetAt);node.textContent='sıfırlanma: '+(Number.isFinite(at)?duration(at-Date.now()/1000):'—')});document.querySelectorAll('[data-updated]').forEach(node=>{const at=Number(node.dataset.updated);node.textContent=Number.isFinite(at)?ago(at):''})}
function renderUsage(data){const root=$('usage');root.replaceChildren();for(const [key,name] of [['claude','Claude'],['codex','Codex']]){const source=data[key]||{};for(const [windowKey,label] of [['five_hour','5 saat'],['weekly','Haftalık']]){const value=source[windowKey]||{},pct=value.used_percent==null?NaN:Number(value.used_percent),age=source.updated==null?Infinity:Date.now()/1000-Number(source.updated),stale=age>1800;const cell=document.createElement('div');cell.className='usage-cell'+(stale?' stale':'');const head=document.createElement('div');head.className='usage-head';head.innerHTML='<span>'+name+'</span><span>'+label+'</span>';const main=document.createElement('div');main.className='usage-main';const percent=document.createElement('div');percent.className='usage-pct';percent.textContent=Number.isFinite(pct)?pct.toFixed(1).replace('.0','')+'%':'—';const updated=document.createElement('span');updated.className='usage-updated';if(source.updated!=null)updated.dataset.updated=source.updated;main.append(percent,updated);const track=document.createElement('div');track.className='usage-track';const fill=document.createElement('div');fill.className='usage-fill'+(pct>85?' red':pct>=60?' amber':'');fill.style.width=(Number.isFinite(pct)?Math.max(0,Math.min(100,pct)):0)+'%';track.append(fill);const reset=document.createElement('div');reset.className='usage-reset';if(value.resets_at!=null)reset.dataset.resetAt=value.resets_at;else reset.textContent=value.reset?'sıfırlandı':'sıfırlanma: —';cell.append(head,main,track,reset);if(source.error||stale){const msg=document.createElement('div');msg.className='usage-message';msg.textContent=source.error||'eski veri';cell.append(msg)}root.append(cell)}}updateUsageTimes()}
async function loadUsage(){try{renderUsage(await json('/api/usage'));$('live').classList.remove('offline')}catch(error){renderUsage({claude:{error:'Kullanım hatası: '+error.message},codex:{error:'Kullanım hatası: '+error.message}});$('live').classList.add('offline')}}
function renderState(data){const o=data.orchestrator||{};setText('step',o.claude_step||'—');setText('updated',fmtTime(o.updated));setText('notes',o.notes||'');const next=$('next');next.replaceChildren();for(const item of Array.isArray(o.next)?o.next:[]){const li=document.createElement('li');li.textContent=item;next.append(li)}if(!next.children.length){const li=document.createElement('li');li.className='empty';li.textContent='Planlanan adım yok.';next.append(li)}const body=$('tasks');body.replaceChildren();for(const t of data.tasks||[]){const title=stripTaskTitle(t.title,t.id),tr=document.createElement('tr');tr.addEventListener('click',()=>openTask(t.id,title));const values=[t.id,title,t.status,t.model||'—',t.effort||'—',formatTokens(t.tokens),trDuration(t.elapsed)];values.forEach((value,i)=>{const td=document.createElement('td');td.dataset.label=labels[i];if(i===1){td.className='task-title';td.title=title}if(i===2){const badge=document.createElement('span');badge.className='badge '+t.status;badge.textContent=t.status;td.append(badge)}else td.textContent=value;tr.append(td)});body.append(tr)}if(!body.children.length){const tr=document.createElement('tr'),td=document.createElement('td');td.colSpan=7;td.className='empty';td.textContent='Henüz görev yok.';tr.append(td);body.append(tr)}const commits=$('commits');commits.replaceChildren();if(!(data.commits||[]).length){commits.className='empty';commits.textContent='Git geçmişi bulunamadı.'}for(const c of data.commits||[]){const div=document.createElement('div');div.className='commit';const hash=document.createElement('span');hash.className='hash mono';hash.textContent=c.hash;const subject=document.createElement('span');subject.className='commit-subject';subject.textContent=c.subject;const age=document.createElement('span');age.className='commit-age';age.textContent=c.age;div.append(hash,subject,age);commits.append(div)}}
async function loadState(){try{const data=await json('/api/state'),signature=JSON.stringify(data);$('live').classList.remove('offline');if(signature!==stateSignature){stateSignature=signature;renderState(data)}}catch(error){$('live').classList.add('offline');setText('step','Bağlantı hatası: '+error.message)}}
async function openTask(id,title){selected=id;$('detail').classList.remove('hidden');$('backdrop').classList.remove('hidden');setText('detail-title',id+' — '+(title||''));try{const data=await json('/api/task/'+encodeURIComponent(id));setText('prompt',data.prompt||'İçerik yok.');setText('report',data.report||'Henüz rapor yok.');const log=$('log'),atBottom=log.scrollHeight-log.scrollTop-log.clientHeight<30;log.textContent=data.log||'Henüz log yok.';if(atBottom||!log.dataset.loaded)log.scrollTop=log.scrollHeight;log.dataset.loaded='1'}catch(error){setText('log','Bağlantı hatası: '+error.message)}}
const closeDrawer=()=>{selected=null;$('detail').classList.add('hidden');$('backdrop').classList.add('hidden')};
$('close').onclick=closeDrawer;$('backdrop').onclick=closeDrawer;document.addEventListener('keydown',event=>{if(event.key==='Escape')closeDrawer()});document.querySelectorAll('.tab').forEach(tab=>tab.onclick=()=>{document.querySelectorAll('.tab').forEach(item=>item.classList.toggle('active',item===tab));['prompt','report','log'].forEach(id=>$(id).classList.toggle('hidden',id!==tab.dataset.pane));if(tab.dataset.pane==='log')$('log').scrollTop=$('log').scrollHeight});
loadUsage();loadState();
setInterval(loadUsage,10000);
setInterval(loadState,3000);
setInterval(()=>{if(selected)openTask(selected,$('detail-title').textContent.replace(/^.*? — /,''))},3000);
setInterval(updateUsageTimes,1000);
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
            page = HTML.replace(
                '<span class="folder">Whip-View</span>',
                f'<span class="folder">{html.escape(REPO_ROOT.name)}</span>',
                1,
            )
            self.send_bytes(page.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/state":
            self.send_json(
                {
                    "orchestrator": load_orchestrator(),
                    "tasks": task_state(),
                    "commits": git_commits(),
                }
            )
        elif path == "/api/usage":
            self.send_json(usage_state())
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


def _local_project() -> str | None:
    """First line of whipview.local next to this script, if it names a folder.

    Lets a double-clicked whipview.py (no arguments, cwd = this folder) watch
    the configured project, like the launcher scripts do.
    """
    try:
        lines = (Path(__file__).resolve().parent / "whipview.local").read_text(
            encoding="utf-8-sig").splitlines()
    except OSError:
        return None
    first = lines[0].strip().strip('"') if lines else ""
    return first if first and Path(first).is_dir() else None


def main() -> None:
    global REPO_ROOT, TASK_DIR, PORT
    ap = argparse.ArgumentParser(description="Live dashboard for Codex task runs.")
    ap.add_argument("repo", nargs="?", default=None,
                    help="project root (default: first line of whipview.local, else cwd)")
    ap.add_argument("--tasks-dir", help="task folder (default: <repo>/.codex-tasks)")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--no-browser", action="store_true", help="do not open the browser")
    args = ap.parse_args()
    REPO_ROOT = Path(args.repo or _local_project() or ".").resolve()
    TASK_DIR = Path(args.tasks_dir).resolve() if args.tasks_dir else REPO_ROOT / ".codex-tasks"
    PORT = args.port
    server = ThreadingHTTPServer((HOST, PORT), DashboardHandler)
    url = f"http://{HOST}:{PORT}"
    print(f"Whip-View watching {TASK_DIR}", flush=True)
    print(f"{url}   (Ctrl+C to stop)", flush=True)
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
