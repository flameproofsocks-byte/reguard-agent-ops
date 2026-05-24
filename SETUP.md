# Reguard Ops — Setup Guide

A general-purpose agent dashboard for spawning and chatting with Claude Code agents, tracking project tasks, and running scheduled routines. Agents are accessible via the web UI and via Telegram.

---

## Prerequisites

- Python 3.11+
- [Claude Code](https://claude.ai/code) installed and authenticated (`claude` must be on your PATH)
- A Telegram bot token (optional but recommended)

---

## 1. Clone and install

```bash
git clone <your-repo-url> reguardtrading
cd reguardtrading

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -r dashboard/requirements.txt
```

---

## 2. Configure credentials

```bash
cp .env.example .env
```

Open `.env` and fill in your values:

```
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
INSIGHT_SENTRY_API_KEY=your_api_key_here        # only needed for the stock scanner
```

### Getting a Telegram bot token

1. Open Telegram and search for **@BotFather**
2. Send `/newbot` and follow the prompts (pick a name and username)
3. BotFather will give you a token like `123456789:AAF...` — paste that into `.env`

### Authenticating Claude Code

Claude Code needs to be logged in on this machine. If you haven't already:

```bash
claude login
```

Follow the browser prompt to authenticate. Once done, `claude -p "hello"` should return a response. The dashboard spawns `claude` as a subprocess for every agent message, so this must work before starting the server.

---

## 3. Start the dashboard

```bash
bash start-dashboard.sh
```

The dashboard will start in the background at **http://localhost:6969/agents**.

Other commands:

```bash
bash restart-dashboard.sh   # restart after code or config changes
bash stop-dashboard.sh      # stop the server
tail -f /tmp/reguard-dashboard.log  # view live logs
```

---

## 4. Your first project and agent

### In the web UI

1. Open **http://localhost:6969/agents** in your browser
2. Click **New Project** — give it a name and optional description
3. Click **New Agent** inside the project — give the agent a name and optionally a skillset or agenda
4. Click on the agent to open its chat panel and start talking to it

### Via Telegram

Once the bot is running, open a chat with your bot in Telegram and use these commands:

| Command | What it does |
|---|---|
| `/start` | Greet the bot and see the active agent |
| `/agents` | List all agents and their context usage |
| `/use <name>` | Switch which agent you're chatting with |
| `/projects` | List all projects |
| `/newproject <name>` | Create a new project |
| `/newagent <name>` | Create a new agent |
| `/tasks` | View planned and completed tasks for the active project |
| `/tasks add "do something"` | Add a task |
| `/tasks complete "do something"` | Mark a task complete |
| `/routines` | List scheduled routines |
| `/compact <id>` | Compact an agent's context window |
| `/clear` | Clear the active agent's chat history |
| `/help` | Show all commands |

Any other message is forwarded directly to the active agent.

---

## 5. Routines (scheduled jobs)

Routines are shell commands that run on a cron schedule. Agents can create them via the dashboard API, or you can create them manually via the web UI.

Agents are aware of the routines API and can set up scheduled tasks when asked. For example:

> *"Set up a routine that runs my scanner script every morning at 9am ET"*

---

## 6. Projects directory

Agent workspaces and chat histories are stored under `projects/`. This directory is excluded from git — each machine maintains its own state. To share agent context between machines, you would need to copy or sync the relevant `projects/<name>/agents/<id>/` directory manually.

---

## Troubleshooting

**Dashboard won't start**
Check the logs: `cat /tmp/reguard-dashboard.log`

**Claude not responding**
Make sure `claude -p "hello"` works in your terminal. If not, run `claude login`.

**Telegram bot not responding**
- Confirm `TELEGRAM_BOT_TOKEN` is set correctly in `.env`
- Restart the dashboard: `bash restart-dashboard.sh`
- Check logs for errors: `tail -50 /tmp/reguard-dashboard.log`

**Port 6969 already in use**
`bash stop-dashboard.sh` then `bash start-dashboard.sh`, or change the port in `start-dashboard.sh`.
