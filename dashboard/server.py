import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

import threading

# Telegram: per-chat agent selection (chat_id -> agent_id)
telegram_chat_agents: dict[int, str] = {}

# Reference to the main uvicorn event loop, set at startup
_main_loop: Optional[asyncio.AbstractEventLoop] = None
# Telegram bot app reference for shutdown
_tg_bot_app = None
_tg_bot_loop: Optional[asyncio.AbstractEventLoop] = None

# Projects stored one directory up from dashboard/
PROJECTS_DIR = Path(__file__).parent.parent / "projects"
ROUTINES_DIR = Path(__file__).parent.parent / "routines"

scheduler = AsyncIOScheduler()


# ── Filesystem helpers ──

def slugify(name: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", name.lower())
    slug = re.sub(r"[\s_-]+", "-", slug)
    return slug.strip("-") or "project"


def _find_project_dir(project_id: str) -> Optional[Path]:
    if not PROJECTS_DIR.exists():
        return None
    for d in PROJECTS_DIR.iterdir():
        if not d.is_dir():
            continue
        pjson = d / "project.json"
        if pjson.exists():
            try:
                data = json.loads(pjson.read_text())
                if data.get("id") == project_id:
                    return d
            except Exception:
                pass
    return None


def _get_project_dir(project: dict) -> Path:
    existing = _find_project_dir(project["id"])
    if existing:
        return existing
    slug = slugify(project["name"])
    pdir = PROJECTS_DIR / slug
    if pdir.exists():
        pdir = PROJECTS_DIR / f"{slug}-{project['id'][:4]}"
    return pdir


def save_project(project: dict):
    pdir = _get_project_dir(project)
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "agents").mkdir(exist_ok=True)
    (pdir / "tasks" / "planned").mkdir(parents=True, exist_ok=True)
    (pdir / "tasks" / "completed").mkdir(parents=True, exist_ok=True)

    meta = {k: v for k, v in project.items() if k not in ("planned_tasks", "completed_tasks")}
    (pdir / "project.json").write_text(json.dumps(meta, indent=2))

    planned_dir = pdir / "tasks" / "planned"
    completed_dir = pdir / "tasks" / "completed"

    for f in planned_dir.glob("*.json"):
        f.unlink()
    for f in completed_dir.glob("*.json"):
        f.unlink()

    for i, task in enumerate(project.get("planned_tasks", [])):
        (planned_dir / f"{i:04d}-{task['id']}.json").write_text(json.dumps(task, indent=2))
    for i, task in enumerate(project.get("completed_tasks", [])):
        (completed_dir / f"{i:04d}-{task['id']}.json").write_text(json.dumps(task, indent=2))


def delete_project_dir(project_id: str):
    pdir = _find_project_dir(project_id)
    if pdir and pdir.exists():
        shutil.rmtree(pdir)


def get_agent_dir(agent: dict) -> Path:
    project_id = agent.get("project_id")
    if project_id:
        pdir = _find_project_dir(project_id)
        if pdir:
            return pdir / "agents" / agent["id"]
    return PROJECTS_DIR / "_orphans" / "agents" / agent["id"]


def save_agent(agent: dict):
    agent_dir = get_agent_dir(agent)
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "agent.json").write_text(json.dumps(agent, indent=2))


def delete_agent_dir(agent: dict):
    agent_dir = get_agent_dir(agent)
    if agent_dir.exists():
        shutil.rmtree(agent_dir)


def get_session_id(agent_dir: Path) -> Optional[str]:
    f = agent_dir / ".session_id"
    if f.exists():
        return f.read_text().strip() or None
    return None


def save_session_id(agent_dir: Path, session_id: str):
    (agent_dir / ".session_id").write_text(session_id)


def delete_session_id(agent_dir: Path):
    f = agent_dir / ".session_id"
    if f.exists():
        f.unlink()


def write_claude_md(agent_dir: Path, agent: dict, project: dict):
    lines = [f"# {agent['name']}\n\n"]
    lines.append(f"You are {agent['name']}, an AI agent in the Reguard Ops system.\n\n")

    if agent.get("skillset"):
        lines.append(f"## Skills\n{', '.join(agent['skillset'])}\n\n")

    if agent.get("agenda"):
        lines.append(f"## Agenda\n{agent['agenda']}\n\n")

    if project:
        pname = project.get("name", "")
        if pname:
            lines.append(f"## Project: {pname}\n\n")
        if project.get("description"):
            lines.append(f"{project['description']}\n\n")
        if project.get("goal"):
            lines.append(f"**Goal:** {project['goal']}\n\n")
        if project.get("tech_stack"):
            lines.append(f"**Tech Stack:** {project['tech_stack']}\n\n")
        planned = project.get("planned_tasks", [])
        if planned:
            lines.append("## Planned Tasks\n\n")
            for t in planned:
                lines.append(f"- {t['text']}\n")
            lines.append("\n")

    lines.append("## Dashboard API\n\n")
    lines.append("The Reguard dashboard runs at `http://localhost:6969`. You can call it with `curl` or Python `requests`.\n\n")
    lines.append("### Routines (scheduled jobs)\n\n")
    lines.append("Create a routine (runs a shell command on a cron schedule):\n")
    lines.append("```bash\n")
    lines.append('curl -s -X POST http://localhost:6969/api/routines \\\n')
    lines.append('  -H "Content-Type: application/json" \\\n')
    lines.append('  -d \'{"name": "My Job", "cron": "0 12 * * *", "command": "python /path/to/script.py", "enabled": true}\'\n')
    lines.append("```\n\n")
    lines.append("Cron expressions are UTC. Toronto is UTC-4 (EDT) or UTC-5 (EST).\n\n")
    lines.append("List routines: `curl http://localhost:6969/api/routines`\n\n")
    lines.append("Run a routine now: `curl -X POST http://localhost:6969/api/routines/{id}/run`\n\n")
    lines.append("Update a routine: `curl -X PATCH http://localhost:6969/api/routines/{id} -H 'Content-Type: application/json' -d '{\"enabled\": false}'`\n\n")
    lines.append("Delete a routine: `curl -X DELETE http://localhost:6969/api/routines/{id}`\n\n")

    lines.append("## Instructions\n\n")
    lines.append("- Be concise and technically precise\n")
    lines.append("- When writing code use appropriate code blocks\n")
    lines.append("- You are operating as part of a live algorithmic trading system\n")
    lines.append("- You can create and manage routines via the dashboard API above — use this when asked to schedule anything\n")

    (agent_dir / "CLAUDE.md").write_text("".join(lines))


def _load_md_task(f: Path, status: str) -> dict:
    """Parse an orchestrator-written .md task file into a task dict."""
    text = f.read_text()
    title = f.stem  # fallback
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("# "):
            title = line[2:].strip()
            break
    task_id = re.sub(r"^\d+[_-]?", "", f.stem) or f.stem
    return {"id": task_id, "text": title, "status": status}


def _load_tasks_from_dir(task_dir: Path, status: str) -> list:
    tasks = []
    if not task_dir.exists():
        return tasks
    seen_ids = set()
    for f in sorted(task_dir.iterdir()):
        if f.suffix == ".json":
            try:
                t = json.loads(f.read_text())
                tasks.append(t)
                seen_ids.add(t.get("id"))
            except Exception as e:
                print(f"Error reading task {f}: {e}")
        elif f.suffix == ".md":
            try:
                t = _load_md_task(f, status)
                if t["id"] not in seen_ids:
                    tasks.append(t)
                    seen_ids.add(t["id"])
                    # migrate to .json so it shows up consistently
                    idx = len(tasks) - 1
                    out = task_dir / f"{idx:04d}-{t['id']}.json"
                    out.write_text(json.dumps(t, indent=2))
                    f.unlink()
                    print(f"Migrated task {f.name} → {out.name}")
            except Exception as e:
                print(f"Error migrating task {f}: {e}")
    return tasks


def load_all(agents_store: dict, projects_store: dict):
    if not PROJECTS_DIR.exists():
        return

    for d in sorted(PROJECTS_DIR.iterdir()):
        if not d.is_dir() or d.name.startswith("_"):
            continue
        pjson = d / "project.json"
        if not pjson.exists():
            continue
        try:
            project = json.loads(pjson.read_text())

            # Backfill slug for older projects that predate slug support
            if not project.get("slug"):
                project["slug"] = slugify(project.get("name", d.name))
                pjson.write_text(json.dumps({k: v for k, v in project.items() if k not in ("planned_tasks", "completed_tasks")}, indent=2))

            project["planned_tasks"] = _load_tasks_from_dir(d / "tasks" / "planned", "planned")
            project["completed_tasks"] = _load_tasks_from_dir(d / "tasks" / "completed", "completed")

            projects_store[project["id"]] = project

            agents_dir = d / "agents"
            if agents_dir.exists():
                for item in sorted(agents_dir.iterdir()):
                    if item.is_dir():
                        # New structure: agents/{id}/agent.json
                        agent_json = item / "agent.json"
                        if agent_json.exists():
                            try:
                                agent = json.loads(agent_json.read_text())
                                agents_store[agent["id"]] = agent
                            except Exception as e:
                                print(f"Error loading agent {agent_json}: {e}")
                    elif item.suffix == ".json":
                        # Old flat structure: agents/{id}.json — migrate to directory
                        try:
                            agent = json.loads(item.read_text())
                            agent_id = agent["id"]
                            new_dir = agents_dir / agent_id
                            new_dir.mkdir(exist_ok=True)
                            (new_dir / "agent.json").write_text(json.dumps(agent, indent=2))
                            item.unlink()
                            agents_store[agent_id] = agent
                            print(f"Migrated agent {agent_id} to directory structure")
                        except Exception as e:
                            print(f"Error migrating agent {item}: {e}")

        except Exception as e:
            print(f"Error loading project from {d}: {e}")

    orphan_agents = PROJECTS_DIR / "_orphans" / "agents"
    if orphan_agents.exists():
        for item in sorted(orphan_agents.iterdir()):
            if item.is_dir():
                agent_json = item / "agent.json"
                if agent_json.exists():
                    try:
                        agent = json.loads(agent_json.read_text())
                        agents_store[agent["id"]] = agent
                    except Exception as e:
                        print(f"Error loading orphan agent {agent_json}: {e}")


# ── Routines helpers ──

routines_store: dict[str, dict] = {}
running_procs: dict[str, asyncio.subprocess.Process] = {}


def _kill_proc_group(proc: asyncio.subprocess.Process):
    """Kill the process group so child processes don't get orphaned."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass


def save_routine(routine: dict):
    ROUTINES_DIR.mkdir(parents=True, exist_ok=True)
    (ROUTINES_DIR / f"{routine['id']}.json").write_text(json.dumps(routine, indent=2))


def delete_routine_file(routine_id: str):
    f = ROUTINES_DIR / f"{routine_id}.json"
    if f.exists():
        f.unlink()


def load_routines(store: dict):
    if not ROUTINES_DIR.exists():
        return
    for f in sorted(ROUTINES_DIR.glob("*.json")):
        try:
            r = json.loads(f.read_text())
            store[r["id"]] = r
        except Exception as e:
            print(f"Error loading routine {f}: {e}")


def _schedule_routine(routine: dict):
    """Register or re-register a routine with the scheduler."""
    rid = routine["id"]
    if scheduler.get_job(rid):
        scheduler.remove_job(rid)
    if routine.get("enabled"):
        try:
            scheduler.add_job(
                _run_routine_job,
                CronTrigger.from_crontab(routine["cron"]),
                id=rid,
                args=[rid],
                replace_existing=True,
                misfire_grace_time=300,
            )
        except Exception as e:
            print(f"Error scheduling routine {rid}: {e}")


async def _run_routine_job(routine_id: str):
    routine = routines_store.get(routine_id)
    if not routine or not routine.get("enabled"):
        return
    await _execute_routine(routine)


async def _execute_routine(routine: dict):
    rid = routine["id"]
    if rid in running_procs:
        return  # already running

    started = datetime.now(timezone.utc).isoformat()
    routine["last_run"] = started
    routine["last_status"] = "running"
    routine["last_output"] = ""
    save_routine(routine)
    await broadcast({"type": "routine_updated", "routine": routine})

    try:
        proc = await asyncio.create_subprocess_shell(
            routine["command"],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(Path(__file__).parent.parent),
            start_new_session=True,
        )
        running_procs[rid] = proc

        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3600)
            output = stdout.decode(errors="replace").strip()
            # Negative returncode means killed by signal — treat as stopped
            if proc.returncode is not None and proc.returncode < 0:
                routine["last_status"] = "stopped"
                routine["last_output"] = output[-4000:] if output else "Stopped"
            else:
                routine["last_status"] = "success" if proc.returncode == 0 else "error"
                routine["last_output"] = output[-4000:] if output else ""
        except asyncio.TimeoutError:
            _kill_proc_group(proc)
            await proc.wait()
            routine["last_status"] = "error"
            routine["last_output"] = "Timed out after 1 hour"
    except Exception as e:
        routine["last_status"] = "error"
        routine["last_output"] = str(e)
    finally:
        running_procs.pop(rid, None)

    save_routine(routine)
    await broadcast({"type": "routine_updated", "routine": routine})


# ── App setup ──

agents_store: dict[str, dict] = {}
projects_store: dict[str, dict] = {}
connections: set[WebSocket] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_all(agents_store, projects_store)
    print(f"Loaded {len(projects_store)} project(s), {len(agents_store)} agent(s) from disk")

    load_routines(routines_store)
    scheduler.start()
    for r in routines_store.values():
        _schedule_routine(r)
    print(f"Loaded {len(routines_store)} routine(s), scheduler started")

    global _main_loop
    _main_loop = asyncio.get_event_loop()

    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    tg_thread = None
    if tg_token:
        tg_thread = threading.Thread(
            target=_run_telegram_bot_thread,
            args=(tg_token,),
            daemon=True,
            name="telegram-bot",
        )
        tg_thread.start()
        print("Telegram bot started")
    else:
        print("TELEGRAM_BOT_TOKEN not set — Telegram bot disabled")

    yield

    scheduler.shutdown(wait=False)

    # tg_thread is a daemon so it dies with the process


app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


async def broadcast(message: dict):
    dead = set()
    for ws in connections:
        try:
            await ws.send_json(message)
        except Exception:
            dead.add(ws)
    connections.difference_update(dead)


# ── Pydantic models ──

class CreateAgent(BaseModel):
    name: str
    skillset: list[str] = []
    agenda: str = ""
    animal: int = 0
    project_id: Optional[str] = None
    is_main: bool = False

class UpdateAgent(BaseModel):
    status: Optional[str] = None
    progress: Optional[str] = None
    agenda: Optional[str] = None
    token_count: Optional[int] = None

class AddPermission(BaseModel):
    tool: str
    description: str

class AddAction(BaseModel):
    text: str

class ChatMsg(BaseModel):
    content: str

class CreateProject(BaseModel):
    name: str
    description: str = ""

class UpdateProject(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    goal: Optional[str] = None
    tech_stack: Optional[str] = None
    status: Optional[str] = None
    main_agent_id: Optional[str] = None

class AddTask(BaseModel):
    text: str

class CreateRoutine(BaseModel):
    name: str
    cron: str
    command: str
    enabled: bool = True

class UpdateRoutine(BaseModel):
    name: Optional[str] = None
    cron: Optional[str] = None
    command: Optional[str] = None
    enabled: Optional[bool] = None


# ── Helpers ──

def _ts():
    return datetime.utcnow().strftime("%H:%M:%S")


# ── Routes ──

_html_cache = None

def _serve_html():
    return (Path(__file__).parent / "index.html").read_text()

@app.get("/agents", response_class=HTMLResponse)
async def dashboard():
    return _serve_html()

@app.get("/projects", response_class=HTMLResponse)
async def projects_root():
    return _serve_html()

@app.get("/projects/{slug}", response_class=HTMLResponse)
async def project_by_slug(slug: str):
    return _serve_html()

@app.get("/routines", response_class=HTMLResponse)
async def routines_page():
    return _serve_html()


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    connections.add(ws)
    await ws.send_json({
        "type": "init",
        "agents": list(agents_store.values()),
        "projects": list(projects_store.values()),
        "routines": list(routines_store.values()),
    })
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        connections.discard(ws)


# ── Projects ──

@app.get("/api/projects")
async def list_projects():
    return list(projects_store.values())


@app.post("/api/projects")
async def create_project(req: CreateProject):
    pid = str(uuid.uuid4())[:8]
    slug = slugify(req.name)
    # ensure slug uniqueness
    existing_slugs = {p.get("slug") for p in projects_store.values()}
    if slug in existing_slugs:
        slug = f"{slug}-{pid[:4]}"
    project = {
        "id": pid,
        "slug": slug,
        "name": req.name,
        "description": req.description,
        "goal": "",
        "tech_stack": "",
        "status": "active",
        "planned_tasks": [],
        "completed_tasks": [],
        "main_agent_id": None,
        "created_at": datetime.utcnow().isoformat(),
    }
    projects_store[project["id"]] = project
    save_project(project)
    await broadcast({"type": "project_added", "project": project})
    return project


@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str):
    if project_id not in projects_store:
        raise HTTPException(404, "Not found")
    to_remove = [aid for aid, a in agents_store.items() if a.get("project_id") == project_id]
    for aid in to_remove:
        del agents_store[aid]
    del projects_store[project_id]
    delete_project_dir(project_id)
    await broadcast({"type": "project_removed", "project_id": project_id})
    return {"ok": True}


@app.patch("/api/projects/{project_id}")
async def update_project(project_id: str, req: UpdateProject):
    if project_id not in projects_store:
        raise HTTPException(404, "Not found")
    p = projects_store[project_id]
    for field in ("name", "description", "goal", "tech_stack", "status", "main_agent_id"):
        val = getattr(req, field)
        if val is not None:
            p[field] = val
    save_project(p)
    await broadcast({"type": "project_updated", "project": p})
    return p


@app.post("/api/projects/{project_id}/tasks")
async def add_task(project_id: str, req: AddTask):
    if project_id not in projects_store:
        raise HTTPException(404, "Not found")
    task = {"id": str(uuid.uuid4())[:8], "text": req.text, "status": "planned"}
    projects_store[project_id]["planned_tasks"].append(task)
    save_project(projects_store[project_id])
    await broadcast({"type": "project_updated", "project": projects_store[project_id]})
    return task


@app.post("/api/projects/{project_id}/tasks/{task_id}/complete")
async def complete_task(project_id: str, task_id: str):
    if project_id not in projects_store:
        raise HTTPException(404, "Not found")
    p = projects_store[project_id]
    task = next((t for t in p["planned_tasks"] if t["id"] == task_id), None)
    if not task:
        raise HTTPException(404, "Task not found")
    p["planned_tasks"] = [t for t in p["planned_tasks"] if t["id"] != task_id]
    task["status"] = "completed"
    p["completed_tasks"].append(task)
    save_project(p)
    await broadcast({"type": "project_updated", "project": p})
    return task


@app.delete("/api/projects/{project_id}/tasks/{task_id}")
async def delete_task(project_id: str, task_id: str):
    if project_id not in projects_store:
        raise HTTPException(404, "Not found")
    p = projects_store[project_id]
    p["planned_tasks"] = [t for t in p["planned_tasks"] if t["id"] != task_id]
    p["completed_tasks"] = [t for t in p["completed_tasks"] if t["id"] != task_id]
    save_project(p)
    await broadcast({"type": "project_updated", "project": p})
    return {"ok": True}


# ── Agents ──

@app.get("/api/agents")
async def list_agents():
    return list(agents_store.values())


@app.post("/api/agents")
async def create_agent(req: CreateAgent):
    agent = {
        "id": str(uuid.uuid4())[:8],
        "name": req.name,
        "skillset": req.skillset,
        "agenda": req.agenda,
        "animal": req.animal,
        "project_id": req.project_id,
        "is_main": req.is_main,
        "status": "idle",
        "progress": "Waiting to start...",
        "permissions": [],
        "action_log": [],
        "chat_messages": [],
        "token_count": 0,
        "token_max": 200000,
        "created_at": datetime.utcnow().isoformat(),
    }
    agents_store[agent["id"]] = agent
    if req.is_main and req.project_id and req.project_id in projects_store:
        projects_store[req.project_id]["main_agent_id"] = agent["id"]
        save_project(projects_store[req.project_id])
        await broadcast({"type": "project_updated", "project": projects_store[req.project_id]})
    save_agent(agent)
    await broadcast({"type": "agent_added", "agent": agent})
    return agent


@app.delete("/api/agents/{agent_id}")
async def delete_agent(agent_id: str):
    if agent_id not in agents_store:
        raise HTTPException(404, "Not found")
    a = agents_store.pop(agent_id)
    delete_agent_dir(a)
    if a.get("is_main") and a.get("project_id") and a["project_id"] in projects_store:
        projects_store[a["project_id"]]["main_agent_id"] = None
        save_project(projects_store[a["project_id"]])
        await broadcast({"type": "project_updated", "project": projects_store[a["project_id"]]})
    await broadcast({"type": "agent_removed", "agent_id": agent_id})
    return {"ok": True}


@app.patch("/api/agents/{agent_id}")
async def update_agent(agent_id: str, req: UpdateAgent):
    if agent_id not in agents_store:
        raise HTTPException(404, "Not found")
    a = agents_store[agent_id]
    if req.status is not None:
        a["status"] = req.status
    if req.progress is not None:
        a["progress"] = req.progress
    if req.agenda is not None:
        a["agenda"] = req.agenda
    if req.token_count is not None:
        a["token_count"] = req.token_count
    save_agent(a)
    await broadcast({"type": "agent_updated", "agent": a})
    return a


@app.post("/api/agents/{agent_id}/action")
async def add_action(agent_id: str, req: AddAction):
    if agent_id not in agents_store:
        raise HTTPException(404, "Not found")
    entry = {"ts": _ts(), "text": req.text}
    a = agents_store[agent_id]
    a["action_log"].append(entry)
    a["action_log"] = a["action_log"][-100:]
    save_agent(a)
    await broadcast({"type": "agent_updated", "agent": a})
    return entry


@app.post("/api/agents/{agent_id}/compact")
async def compact_context(agent_id: str):
    if agent_id not in agents_store:
        raise HTTPException(404, "Not found")
    a = agents_store[agent_id]
    old = a["token_count"]
    a["token_count"] = int(old * 0.15)
    a["action_log"].append({"ts": _ts(), "text": f"[COMPACT] {old:,} → {a['token_count']:,} tokens"})
    save_agent(a)
    await broadcast({"type": "agent_updated", "agent": a})
    return {"ok": True, "token_count": a["token_count"]}


# ── Chat (streaming SSE via Claude Code CLI) ──

@app.post("/api/agents/{agent_id}/chat")
async def chat(agent_id: str, req: ChatMsg):
    if agent_id not in agents_store:
        raise HTTPException(404, "Not found")

    a = agents_store[agent_id]
    project = projects_store.get(a.get("project_id") or "", {})

    user_msg = {"id": str(uuid.uuid4())[:8], "role": "user", "content": req.content, "ts": _ts()}
    a["chat_messages"].append(user_msg)
    await broadcast({"type": "agent_updated", "agent": a})

    async def generate():
        agent_dir = get_agent_dir(a)
        agent_dir.mkdir(parents=True, exist_ok=True)
        write_claude_md(agent_dir, a, project)
        session_id = get_session_id(agent_dir)

        cmd = [
            "claude", "-p", req.content,
            "--output-format", "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
        ]
        if session_id:
            cmd += ["--resume", session_id]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(agent_dir),
            )
        except FileNotFoundError:
            yield f"data: {json.dumps({'type': 'error', 'error': 'claude CLI not found — install Claude Code first.'})}\n\n"
            return

        thinking_buf = []
        text_buf = []

        try:
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode().strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except Exception:
                    continue

                etype = event.get("type")

                if etype == "system" and event.get("subtype") == "init":
                    sid = event.get("session_id")
                    if sid:
                        save_session_id(agent_dir, sid)

                elif etype == "assistant":
                    content = event.get("message", {}).get("content", [])
                    if isinstance(content, str):
                        text_buf.append(content)
                        yield f"data: {json.dumps({'type': 'text_start'})}\n\n"
                        yield f"data: {json.dumps({'type': 'text', 'text': content})}\n\n"
                    else:
                        for block in content:
                            btype = block.get("type")
                            if btype == "thinking":
                                chunk = block.get("thinking", "")
                                thinking_buf.append(chunk)
                                yield f"data: {json.dumps({'type': 'thinking_start'})}\n\n"
                                yield f"data: {json.dumps({'type': 'thinking', 'text': chunk})}\n\n"
                            elif btype == "text":
                                chunk = block.get("text", "")
                                text_buf.append(chunk)
                                yield f"data: {json.dumps({'type': 'text_start'})}\n\n"
                                yield f"data: {json.dumps({'type': 'text', 'text': chunk})}\n\n"

                elif etype == "result":
                    usage = event.get("usage") or {}
                    tokens = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
                    if tokens:
                        a["token_count"] = tokens

        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"
            proc.kill()
            await proc.wait()
            return

        await proc.wait()

        if proc.returncode != 0 and not text_buf:
            stderr_data = await proc.stderr.read()
            err = stderr_data.decode().strip() or f"claude exited with code {proc.returncode}"
            yield f"data: {json.dumps({'type': 'error', 'error': err})}\n\n"
            return

        asst_msg = {
            "id": str(uuid.uuid4())[:8],
            "role": "assistant",
            "content": "".join(text_buf),
            "thinking": "".join(thinking_buf),
            "ts": _ts(),
        }
        a["chat_messages"].append(asst_msg)
        a["action_log"].append({"ts": _ts(), "text": f"[CHAT] {req.content[:60]}"})
        a["action_log"] = a["action_log"][-100:]
        save_agent(a)
        await broadcast({"type": "agent_updated", "agent": a})
        yield f"data: {json.dumps({'type': 'done', 'message': asst_msg})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/agents/{agent_id}/chat")
async def get_chat(agent_id: str):
    if agent_id not in agents_store:
        raise HTTPException(404, "Not found")
    return agents_store[agent_id]["chat_messages"]


@app.delete("/api/agents/{agent_id}/chat")
async def clear_chat(agent_id: str):
    if agent_id not in agents_store:
        raise HTTPException(404, "Not found")
    a = agents_store[agent_id]
    a["chat_messages"] = []
    delete_session_id(get_agent_dir(a))
    save_agent(a)
    await broadcast({"type": "agent_updated", "agent": a})
    return {"ok": True}


# ── Permissions ──

@app.post("/api/agents/{agent_id}/permission")
async def add_permission(agent_id: str, req: AddPermission):
    if agent_id not in agents_store:
        raise HTTPException(404, "Not found")
    perm = {"id": str(uuid.uuid4())[:8], "tool": req.tool, "description": req.description, "status": "pending"}
    agents_store[agent_id]["permissions"].append(perm)
    save_agent(agents_store[agent_id])
    await broadcast({"type": "agent_updated", "agent": agents_store[agent_id]})
    return perm


@app.post("/api/agents/{agent_id}/permissions/{perm_id}/approve")
async def approve(agent_id: str, perm_id: str):
    if agent_id not in agents_store:
        raise HTTPException(404, "Not found")
    for p in agents_store[agent_id]["permissions"]:
        if p["id"] == perm_id:
            p["status"] = "approved"
    save_agent(agents_store[agent_id])
    await broadcast({"type": "agent_updated", "agent": agents_store[agent_id]})
    return {"ok": True}


@app.post("/api/agents/{agent_id}/permissions/{perm_id}/deny")
async def deny(agent_id: str, perm_id: str):
    if agent_id not in agents_store:
        raise HTTPException(404, "Not found")
    for p in agents_store[agent_id]["permissions"]:
        if p["id"] == perm_id:
            p["status"] = "denied"
    save_agent(agents_store[agent_id])
    await broadcast({"type": "agent_updated", "agent": agents_store[agent_id]})
    return {"ok": True}


# ── Routines ──

@app.get("/api/routines")
async def list_routines():
    return list(routines_store.values())


@app.post("/api/routines")
async def create_routine(req: CreateRoutine):
    try:
        CronTrigger.from_crontab(req.cron)
    except Exception:
        raise HTTPException(400, f"Invalid cron expression: {req.cron}")
    routine = {
        "id": str(uuid.uuid4())[:8],
        "name": req.name,
        "cron": req.cron,
        "command": req.command,
        "enabled": req.enabled,
        "last_run": None,
        "last_status": None,
        "last_output": "",
        "created_at": datetime.utcnow().isoformat(),
    }
    routines_store[routine["id"]] = routine
    save_routine(routine)
    _schedule_routine(routine)
    await broadcast({"type": "routine_added", "routine": routine})
    return routine


@app.patch("/api/routines/{routine_id}")
async def update_routine(routine_id: str, req: UpdateRoutine):
    if routine_id not in routines_store:
        raise HTTPException(404, "Not found")
    r = routines_store[routine_id]
    if req.name is not None:
        r["name"] = req.name
    if req.cron is not None:
        try:
            CronTrigger.from_crontab(req.cron)
        except Exception:
            raise HTTPException(400, f"Invalid cron expression: {req.cron}")
        r["cron"] = req.cron
    if req.command is not None:
        r["command"] = req.command
    if req.enabled is not None:
        r["enabled"] = req.enabled
    save_routine(r)
    _schedule_routine(r)
    await broadcast({"type": "routine_updated", "routine": r})
    return r


@app.delete("/api/routines/{routine_id}")
async def delete_routine(routine_id: str):
    if routine_id not in routines_store:
        raise HTTPException(404, "Not found")
    if scheduler.get_job(routine_id):
        scheduler.remove_job(routine_id)
    del routines_store[routine_id]
    delete_routine_file(routine_id)
    await broadcast({"type": "routine_removed", "routine_id": routine_id})
    return {"ok": True}


@app.post("/api/routines/{routine_id}/run")
async def run_routine_now(routine_id: str):
    if routine_id not in routines_store:
        raise HTTPException(404, "Not found")
    if routine_id in running_procs:
        raise HTTPException(409, "Routine is already running")
    asyncio.create_task(_execute_routine(routines_store[routine_id]))
    return {"ok": True}


@app.post("/api/routines/{routine_id}/stop")
async def stop_routine(routine_id: str):
    if routine_id not in routines_store:
        raise HTTPException(404, "Not found")
    proc = running_procs.get(routine_id)
    if not proc:
        raise HTTPException(409, "Routine is not running")
    _kill_proc_group(proc)
    return {"ok": True}


# ── Shared agent chat helper (used by Telegram bot) ──

async def run_agent_chat_full(a: dict, project: dict, content: str) -> dict:
    agent_dir = get_agent_dir(a)
    agent_dir.mkdir(parents=True, exist_ok=True)
    write_claude_md(agent_dir, a, project)
    session_id = get_session_id(agent_dir)

    cmd = [
        "claude", "-p", content,
        "--output-format", "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
    ]
    if session_id:
        cmd += ["--resume", session_id]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(agent_dir),
    )

    thinking_buf = []
    text_buf = []

    while True:
        raw = await proc.stdout.readline()
        if not raw:
            break
        line = raw.decode().strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        etype = event.get("type")
        if etype == "system" and event.get("subtype") == "init":
            sid = event.get("session_id")
            if sid:
                save_session_id(agent_dir, sid)
        elif etype == "assistant":
            blocks = event.get("message", {}).get("content", [])
            if isinstance(blocks, str):
                text_buf.append(blocks)
            else:
                for block in blocks:
                    btype = block.get("type")
                    if btype == "thinking":
                        thinking_buf.append(block.get("thinking", ""))
                    elif btype == "text":
                        text_buf.append(block.get("text", ""))
        elif etype == "result":
            usage = event.get("usage") or {}
            tokens = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
            if tokens and a:
                a["token_count"] = tokens

    await proc.wait()

    if proc.returncode != 0 and not text_buf:
        stderr_data = await proc.stderr.read()
        err = stderr_data.decode().strip() or f"claude exited with code {proc.returncode}"
        raise RuntimeError(err)

    return {
        "id": str(uuid.uuid4())[:8],
        "role": "assistant",
        "content": "".join(text_buf),
        "thinking": "".join(thinking_buf),
        "ts": _ts(),
    }


# ── Telegram bot ──

def _tg_get_target_agent(chat_id: int) -> Optional[dict]:
    agent_id = telegram_chat_agents.get(chat_id)
    if agent_id and agent_id in agents_store:
        return agents_store[agent_id]
    for p in projects_store.values():
        mid = p.get("main_agent_id")
        if mid and mid in agents_store:
            return agents_store[mid]
    if agents_store:
        return next(iter(agents_store.values()))
    return None


def _tg_broadcast(msg: dict):
    """Fire-and-forget broadcast from the bot thread to the main event loop."""
    if _main_loop:
        asyncio.run_coroutine_threadsafe(broadcast(msg), _main_loop)


def build_telegram_app(token: str):

    async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        text = update.message.text
        agent = _tg_get_target_agent(chat_id)
        if not agent:
            await update.message.reply_text("No agents available. Create one in the dashboard first.")
            return

        thinking_msg = await update.message.reply_text(f"⏳ {agent['name']} is thinking...")

        user_msg = {"id": str(uuid.uuid4())[:8], "role": "user", "content": text, "ts": _ts()}
        agent["chat_messages"].append(user_msg)
        if _main_loop:
            asyncio.run_coroutine_threadsafe(broadcast({"type": "agent_updated", "agent": agent}), _main_loop)

        project = projects_store.get(agent.get("project_id") or "", {})
        try:
            asst_msg = await run_agent_chat_full(agent, project, text)
        except Exception as e:
            await thinking_msg.edit_text(f"Error: {e}")
            return

        agent["chat_messages"].append(asst_msg)
        agent["action_log"].append({"ts": _ts(), "text": f"[TG] {text[:60]}"})
        agent["action_log"] = agent["action_log"][-100:]
        save_agent(agent)
        if _main_loop:
            asyncio.run_coroutine_threadsafe(broadcast({"type": "agent_updated", "agent": agent}), _main_loop)

        response = asst_msg["content"] or "(no response)"
        # Telegram message limit is 4096 chars
        if len(response) <= 4096:
            await thinking_msg.edit_text(response)
        else:
            await thinking_msg.edit_text(response[:4096])
            for i in range(4096, len(response), 4096):
                await update.message.reply_text(response[i:i + 4096])

    async def cmd_agents(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if not agents_store:
            await update.message.reply_text("No agents found.")
            return
        current = _tg_get_target_agent(chat_id)
        lines = ["Agents:"]
        for a in agents_store.values():
            marker = " <-- current" if current and a["id"] == current["id"] else ""
            project = projects_store.get(a.get("project_id") or "")
            project_tag = f" [{project['name']}]" if project else ""
            tokens = a.get("token_count", 0)
            token_max = a.get("token_max", 200000)
            pct = int(tokens / token_max * 100) if token_max else 0
            ctx_bar = f"{tokens:,}/{token_max:,} ({pct}%)"
            lines.append(f"  {a['name']}{project_tag} ({a['id']}){marker}")
            lines.append(f"    context: {ctx_bar}")
        lines.append("\nUse /use <name or id> to switch.")
        lines.append("Use /compact <id> to compact an agent's context.")
        await update.message.reply_text("\n".join(lines))

    async def cmd_use(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if not ctx.args:
            await update.message.reply_text("Usage: /use <agent name or id>")
            return
        query = " ".join(ctx.args).lower()
        match = None
        for a in agents_store.values():
            if a["id"].lower() == query or a["name"].lower() == query:
                match = a
                break
        if not match:
            await update.message.reply_text(f"Agent not found: {query}\nUse /agents to list available agents.")
            return
        telegram_chat_agents[chat_id] = match["id"]
        project = projects_store.get(match.get("project_id") or "")
        project_tag = f" [{project['name']}]" if project else ""
        await update.message.reply_text(f"Switched to: {match['name']}{project_tag}")

    async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        agent = _tg_get_target_agent(chat_id)
        if not agent:
            await update.message.reply_text("No active agent.")
            return
        agent["chat_messages"] = []
        delete_session_id(get_agent_dir(agent))
        save_agent(agent)
        _tg_broadcast({"type": "agent_updated", "agent": agent})
        await update.message.reply_text(f"Cleared chat for {agent['name']}.")

    async def cmd_compact(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args:
            await update.message.reply_text("Usage: /compact <agent id>")
            return
        agent_id = ctx.args[0]
        agent = agents_store.get(agent_id)
        if not agent:
            # Try partial match on id prefix or name
            q = agent_id.lower()
            agent = next((a for a in agents_store.values()
                          if a["id"].lower().startswith(q) or a["name"].lower() == q), None)
        if not agent:
            await update.message.reply_text(f"Agent not found: {agent_id}\nUse /agents to list agent IDs.")
            return
        old = agent.get("token_count", 0)
        agent["token_count"] = int(old * 0.15)
        agent["action_log"].append({"ts": _ts(), "text": f"[COMPACT] {old:,} → {agent['token_count']:,} tokens"})
        agent["action_log"] = agent["action_log"][-100:]
        save_agent(agent)
        _tg_broadcast({"type": "agent_updated", "agent": agent})
        await update.message.reply_text(
            f"Compacted {agent['name']} ({agent['id']})\n"
            f"Context: {old:,} → {agent['token_count']:,} tokens"
        )

    async def cmd_tasks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        agent = _tg_get_target_agent(chat_id)
        project = None
        if agent and agent.get("project_id"):
            project = projects_store.get(agent["project_id"])
        if not project:
            project = next(iter(projects_store.values()), None)
        if not project:
            await update.message.reply_text("No projects found.")
            return

        args = ctx.args or []
        subcommand = args[0].lower() if args else None

        if subcommand == "add":
            text = " ".join(args[1:]).strip().strip('"').strip("'")
            if not text:
                await update.message.reply_text('Usage: /tasks add "task description"')
                return
            task = {"id": str(uuid.uuid4())[:8], "text": text, "status": "planned"}
            project.setdefault("planned_tasks", []).append(task)
            save_project(project)
            _tg_broadcast({"type": "project_updated", "project": project})
            await update.message.reply_text(f"Added: {text}")
            return

        if subcommand == "complete":
            query = " ".join(args[1:]).strip().strip('"').strip("'").lower()
            if not query:
                await update.message.reply_text('Usage: /tasks complete "task description"')
                return
            planned = project.get("planned_tasks", [])
            match = next((t for t in planned if query in t["text"].lower()), None)
            if not match:
                await update.message.reply_text(f'No planned task matching: "{query}"')
                return
            project["planned_tasks"] = [t for t in planned if t["id"] != match["id"]]
            match["status"] = "completed"
            project.setdefault("completed_tasks", []).append(match)
            save_project(project)
            _tg_broadcast({"type": "project_updated", "project": project})
            await update.message.reply_text(f"Completed: {match['text']}")
            return

        planned = project.get("planned_tasks", [])
        completed = project.get("completed_tasks", [])
        lines = [f"Project: {project['name']}"]
        if planned:
            lines.append(f"\nPlanned ({len(planned)}):")
            for t in planned:
                lines.append(f"  • {t['text']}")
        else:
            lines.append("\nNo planned tasks.")
        if completed:
            lines.append(f"\nCompleted ({len(completed)}):")
            for t in completed:
                lines.append(f"  ✓ {t['text']}")
        lines.append('\n/tasks add "task" — add a task')
        lines.append('/tasks complete "task" — complete a task')
        await update.message.reply_text("\n".join(lines))

    async def cmd_projects(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not projects_store:
            await update.message.reply_text("No projects found.")
            return
        lines = [f"Projects ({len(projects_store)}):"]
        for p in projects_store.values():
            planned = len(p.get("planned_tasks", []))
            completed = len(p.get("completed_tasks", []))
            lines.append(f"\n• {p['name']} [{p.get('status', 'unknown')}]")
            lines.append(f"  Planned: {planned}  Completed: {completed}")
        await update.message.reply_text("\n".join(lines))

    async def cmd_newproject(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args:
            await update.message.reply_text("Usage: /newproject <name>")
            return
        name = " ".join(ctx.args)
        project = {
            "id": str(uuid.uuid4())[:8],
            "name": name,
            "description": "",
            "goal": "",
            "tech_stack": "",
            "status": "active",
            "planned_tasks": [],
            "completed_tasks": [],
            "main_agent_id": None,
            "created_at": datetime.utcnow().isoformat(),
        }
        save_project(project)
        projects_store[project["id"]] = project
        _tg_broadcast({"type": "project_added", "project": project})
        await update.message.reply_text(f"Created project: {name} (id: {project['id']})")

    async def cmd_newagent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args:
            await update.message.reply_text("Usage: /newagent <name> [project name or id]")
            return
        name = ctx.args[0]
        project_query = " ".join(ctx.args[1:]).strip() if len(ctx.args) > 1 else ""
        project = None
        if project_query:
            q = project_query.lower()
            for p in projects_store.values():
                if p["id"] == project_query or q in p["name"].lower():
                    project = p
                    break
            if not project:
                await update.message.reply_text(f"Project not found: {project_query}")
                return
        agent = {
            "id": str(uuid.uuid4())[:8],
            "name": name,
            "skillset": [],
            "agenda": "",
            "animal": 0,
            "project_id": project["id"] if project else None,
            "is_main": False,
            "status": "idle",
            "progress": "Waiting to start...",
            "permissions": [],
            "action_log": [],
            "chat_messages": [],
            "token_count": 0,
            "token_max": 200000,
            "created_at": datetime.utcnow().isoformat(),
        }
        save_agent(agent)
        agents_store[agent["id"]] = agent
        _tg_broadcast({"type": "agent_added", "agent": agent})
        if project and not project.get("main_agent_id"):
            project["main_agent_id"] = agent["id"]
            save_project(project)
            _tg_broadcast({"type": "project_updated", "project": project})
        reply = f"Created agent: {name} (id: {agent['id']})"
        if project:
            reply += f"\nLinked to project: {project['name']}"
        await update.message.reply_text(reply)

    async def cmd_chatid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        await update.message.reply_text(
            f"Your chat ID: {chat_id}\n"
            f"Add this to your .env as:\n"
            f"TELEGRAM_ALERT_CHAT_ID={chat_id}"
        )

    async def cmd_routines(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        args = ctx.args or []
        subcommand = args[0].lower() if args else None

        if subcommand == "run":
            query = " ".join(args[1:]).strip().lower()
            if not query:
                await update.message.reply_text('Usage: /routines run <name>')
                return
            match = next((r for r in routines_store.values() if query in r["name"].lower()), None)
            if not match:
                await update.message.reply_text(f'No routine matching: "{query}"')
                return
            if match["id"] in running_procs:
                await update.message.reply_text(f'Already running: {match["name"]}')
                return
            asyncio.create_task(_execute_routine(match))
            await update.message.reply_text(f"▶ Running: {match['name']}")
            return

        if subcommand == "stop":
            query = " ".join(args[1:]).strip().lower()
            if not query:
                await update.message.reply_text('Usage: /routines stop <name>')
                return
            match = next((r for r in routines_store.values() if query in r["name"].lower()), None)
            if not match:
                await update.message.reply_text(f'No routine matching: "{query}"')
                return
            proc = running_procs.get(match["id"])
            if not proc:
                await update.message.reply_text(f'Not running: {match["name"]}')
                return
            _kill_proc_group(proc)
            await update.message.reply_text(f"⏹ Stopped: {match['name']}")
            return

        if subcommand in ("enable", "disable"):
            query = " ".join(args[1:]).strip().lower()
            match = next((r for r in routines_store.values() if query in r["name"].lower()), None)
            if not match:
                await update.message.reply_text(f'No routine matching: "{query}"')
                return
            match["enabled"] = (subcommand == "enable")
            save_routine(match)
            _schedule_routine(match)
            _tg_broadcast({"type": "routine_updated", "routine": match})
            await update.message.reply_text(f"{'✓ Enabled' if match['enabled'] else '✗ Disabled'}: {match['name']}")
            return

        if not routines_store:
            await update.message.reply_text("No routines configured. Create them in the dashboard.")
            return

        lines = [f"Routines ({len(routines_store)}):"]
        for r in routines_store.values():
            status = "✓" if r.get("enabled") else "✗"
            last = r.get("last_run", "never")
            if last and last != "never":
                try:
                    last = last[:16].replace("T", " ")
                except Exception:
                    pass
            running_indicator = " [RUNNING]" if r["id"] in running_procs else ""
            lines.append(f"\n{status} {r['name']}{running_indicator}")
            lines.append(f"  {r['cron']}  |  last: {last}")
            if r.get("last_status"):
                lines.append(f"  status: {r['last_status']}")
        lines.append('\n/routines run <name> — run now')
        lines.append('/routines stop <name> — stop running process')
        lines.append('/routines enable/disable <name>')
        await update.message.reply_text("\n".join(lines))

    async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "Available commands:\n\n"
            "/help — show this message\n"
            "/agents — list all agents\n"
            "/use <name> — switch active agent\n"
            "/tasks — show planned and completed tasks\n"
            '/tasks add "task" — add a task\n'
            '/tasks complete "task" — complete a task\n'
            "/clear — clear current agent's chat history\n"
            "/compact <id> — compact an agent's context window\n"
            "/projects — list all projects\n"
            "/newproject <name> — create a new project\n"
            "/newagent <name> [project] — create a new agent, optionally linked to a project\n"
            "/routines — list scheduled routines\n"
            "/routines run <name> — run a routine immediately\n"
            "/routines stop <name> — stop a running routine\n"
            "/routines enable/disable <name> — toggle a routine\n"
            "/chatid — get this chat's ID for alert configuration\n\n"
            "Send any other message to chat with the active agent."
        )

    async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        agent = _tg_get_target_agent(update.effective_chat.id)
        if agent:
            project = projects_store.get(agent.get("project_id") or "")
            project_tag = f" [{project['name']}]" if project else ""
            name = f"{agent['name']}{project_tag}"
        else:
            name = "no agent"
        await update.message.reply_text(
            f"Reguard Ops bot ready.\nCurrent agent: {name}\n\n"
            "Use /help to see all commands."
        )

    bot_app = Application.builder().token(token).build()
    bot_app.add_handler(CommandHandler("start", cmd_start))
    bot_app.add_handler(CommandHandler("help", cmd_help))
    bot_app.add_handler(CommandHandler("agents", cmd_agents))
    bot_app.add_handler(CommandHandler("use", cmd_use))
    bot_app.add_handler(CommandHandler("tasks", cmd_tasks))
    bot_app.add_handler(CommandHandler("clear", cmd_clear))
    bot_app.add_handler(CommandHandler("compact", cmd_compact))
    bot_app.add_handler(CommandHandler("projects", cmd_projects))
    bot_app.add_handler(CommandHandler("newproject", cmd_newproject))
    bot_app.add_handler(CommandHandler("newagent", cmd_newagent))
    bot_app.add_handler(CommandHandler("routines", cmd_routines))
    bot_app.add_handler(CommandHandler("chatid", cmd_chatid))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return bot_app


def _run_telegram_bot_thread(token: str):
    global _tg_bot_app
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bot_app = build_telegram_app(token)
    _tg_bot_app = bot_app
    try:
        bot_app.run_polling(stop_signals=None, close_loop=False)
        print("Telegram bot polling stopped", flush=True)
    except Exception as e:
        import traceback
        print(f"Telegram bot error: {e}")
        traceback.print_exc()
    finally:
        _tg_bot_app = None
        loop.close()
        print("Telegram bot thread exited")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=6969)
