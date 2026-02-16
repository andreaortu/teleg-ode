# claude-remote

A self-hosted Telegram bot that lets you interact with [Claude Code](https://docs.anthropic.com/en/docs/claude-code) from your phone. Messages from Telegram are piped to the `claude` CLI running on your machine, and responses are sent back.

```
Your Phone (Telegram)
        │
        ▼
  Telegram Bot API (polling)
        │
        ▼
  claude-remote (Python, on your machine)
        │
        ▼
  claude CLI  (claude -p --output-format stream-json)
```

## Features

- **Multi-project support** — browse all your Claude Code projects and switch between them
- **Session continuity** — resume any existing Claude Code session from Telegram, or start from the terminal and pick up on your phone
- **Permission handling** — when Claude needs to write files or run commands, you get Approve/Deny buttons on Telegram
- **Conversation memory** — each chat maintains its own session; use `/new` to start fresh
- **Message splitting** — long responses are automatically split at paragraph boundaries

## Setup

### 1. Prerequisites

- Python 3.11+
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated (`claude` on your PATH)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

### 2. Install

```bash
git clone <repo-url> && cd claude-remote
pip install -r requirements.txt
cp .env.example .env
```

### 3. Configure

Edit `.env` and set your bot token:

```
TELEGRAM_BOT_TOKEN=your-token-from-botfather
```

Optional settings:

| Variable | Default | Description |
|---|---|---|
| `ALLOWED_USER_IDS` | *(empty = allow all)* | Comma-separated Telegram user IDs |
| `CLAUDE_MODEL` | `sonnet` | Claude model to use |
| `CLAUDE_MAX_BUDGET` | *(none)* | Max spend per session in USD |
| `CLAUDE_ALLOWED_TOOLS` | *(all)* | Restrict available tools |
| `CLAUDE_PROJECTS_DIR` | `~/.claude/projects` | Where Claude stores project data |
| `DEFAULT_WORKING_DIRECTORY` | `~` | Default cwd for new conversations |

### 4. Run

```bash
python main.py
```

The bot uses **polling** (outbound HTTPS only) — no need for ngrok or public URLs.

## Commands

| Command | Description |
|---|---|
| `/start` | Welcome message and command list |
| `/projects` | List all Claude Code projects on your machine |
| `/cd <number\|path>` | Switch active project |
| `/sessions` | List recent sessions in the current project |
| `/resume <id\|number>` | Resume an existing session |
| `/new` | Start a fresh conversation |
| `/model <name>` | Switch Claude model (e.g. `/model opus`) |
| `/budget <amount>` | Set spending cap in USD |
| `/status` | Show current project, session, and settings |

Or just send any text message to chat with Claude.

## How sessions work

- Each Telegram chat gets its own Claude session
- Sessions are stored by Claude Code in `~/.claude/projects/`
- You can start a session from the terminal and `/resume` it from Telegram (or vice versa)
- Only one process can use a session at a time — exit the terminal session before resuming from Telegram

## Permissions

When Claude needs to write a file or run a command, the bot sends a message showing what Claude wants to do, with **Approve** and **Deny** buttons. If you approve, the operation is re-run with the appropriate permission mode.

## Security

- Set `ALLOWED_USER_IDS` to restrict who can use your bot
- The bot runs locally — only you have access to your machine
- Claude runs with default permission mode; destructive operations require your approval via Telegram
- The `.env` file contains your bot token — keep it private

## Project structure

```
claude-remote/
├── main.py               # Entry point
├── config.py             # Configuration from .env
├── claude_executor.py    # Subprocess wrapper for claude CLI
├── sessions.py           # Project/session discovery from ~/.claude
├── platforms/
│   ├── telegram_bot.py   # Telegram bot handlers
│   └── whatsapp_bot.py   # WhatsApp (planned)
├── utils.py              # Message splitting, markdown escaping
├── requirements.txt
└── .env.example
```

## License

MIT
