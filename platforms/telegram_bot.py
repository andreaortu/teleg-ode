import asyncio
import json
import logging
import uuid
from dataclasses import dataclass

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import claude_executor
from claude_executor import PermissionDenial
from config import Config
from sessions import find_session, list_projects, list_sessions
from utils import escape_markdown_v2, split_message

logger = logging.getLogger(__name__)


# ---- Per-chat state ----------------------------------------------------------

@dataclass
class ChatState:
    """State for a single Telegram chat."""
    session_id: str
    project_dir_name: str | None = None  # e.g. "-Users-foo-Desktop-myproject"
    working_directory: str | None = None  # e.g. "/Users/foo/Desktop/myproject"


_chat_states: dict[int, ChatState] = {}

# Pending permission requests: callback_id -> dict
_pending_permissions: dict[str, dict] = {}


def _get_state(chat_id: int, config: Config) -> ChatState:
    if chat_id not in _chat_states:
        _chat_states[chat_id] = ChatState(
            session_id=str(uuid.uuid4()),
            working_directory=config.default_working_directory,
        )
    return _chat_states[chat_id]


def _is_allowed(user_id: int, config: Config) -> bool:
    if not config.allowed_user_ids:
        return True
    return user_id in config.allowed_user_ids


def _format_denials(denials: list[PermissionDenial]) -> str:
    lines = ["Claude needs permission for:\n"]
    for d in denials:
        tool = d.tool_name
        if tool in ("Write", "Edit", "Read"):
            path = d.tool_input.get("file_path", "unknown")
            lines.append(f"  {tool}: {path}")
        elif tool == "Bash":
            cmd = d.tool_input.get("command", "unknown")
            if len(cmd) > 200:
                cmd = cmd[:200] + "..."
            lines.append(f"  Bash: {cmd}")
        else:
            lines.append(f"  {tool}: {json.dumps(d.tool_input)[:200]}")
    return "\n".join(lines)


# ---- Command handlers -------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    if not _is_allowed(update.effective_user.id, config):
        await update.message.reply_text("You are not authorized to use this bot.")
        return

    await update.message.reply_text(
        "Hello! I'm a bridge to Claude Code on your machine.\n\n"
        "Commands:\n"
        "/projects - List all Claude Code projects\n"
        "/cd <project> - Switch to a project\n"
        "/sessions - List sessions in current project\n"
        "/resume <id> - Resume an existing session\n"
        "/new - Start a new conversation\n"
        "/model <name> - Switch Claude model\n"
        "/budget <amount> - Set max budget in USD\n"
        "/status - Show current state\n\n"
        "Or just send a message to chat with Claude!"
    )


async def cmd_projects(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    if not _is_allowed(update.effective_user.id, config):
        return

    projects = list_projects(config.claude_projects_dir)
    if not projects:
        await update.message.reply_text("No Claude Code projects found.")
        return

    lines = ["Projects:\n"]
    for i, p in enumerate(projects, 1):
        lines.append(f"{i}. {p.real_path}  ({p.session_count} sessions)")

    lines.append("\nUse /cd <number> or /cd <path> to switch.")
    await update.message.reply_text("\n".join(lines))

    # Store project list for /cd by number
    context.chat_data["project_list"] = projects


async def cmd_cd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    if not _is_allowed(update.effective_user.id, config):
        return

    if not context.args:
        state = _get_state(update.effective_chat.id, config)
        cwd = state.working_directory or "not set"
        project = state.project_dir_name or "none"
        await update.message.reply_text(
            f"Current directory: {cwd}\nProject: {project}\n\n"
            "Usage: /cd <number> (from /projects list) or /cd <path>"
        )
        return

    arg = " ".join(context.args)
    projects = list_projects(config.claude_projects_dir)

    # Try as a number (index into project list)
    try:
        idx = int(arg) - 1
        project_list = context.chat_data.get("project_list", projects)
        if 0 <= idx < len(project_list):
            p = project_list[idx]
            state = _get_state(update.effective_chat.id, config)
            state.project_dir_name = p.dir_name
            state.working_directory = p.real_path
            state.session_id = str(uuid.uuid4())  # fresh session for new project
            claude_executor._created_sessions.discard(state.session_id)
            await update.message.reply_text(f"Switched to: {p.real_path}")
            return
        else:
            await update.message.reply_text(f"Invalid number. Use 1-{len(project_list)}.")
            return
    except ValueError:
        pass

    # Try as a path — find matching project
    for p in projects:
        if arg in p.real_path or arg in p.dir_name:
            state = _get_state(update.effective_chat.id, config)
            state.project_dir_name = p.dir_name
            state.working_directory = p.real_path
            state.session_id = str(uuid.uuid4())
            claude_executor._created_sessions.discard(state.session_id)
            await update.message.reply_text(f"Switched to: {p.real_path}")
            return

    await update.message.reply_text(f"Project not found: {arg}")


async def cmd_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    if not _is_allowed(update.effective_user.id, config):
        return

    state = _get_state(update.effective_chat.id, config)

    if not state.project_dir_name:
        await update.message.reply_text(
            "No project selected. Use /projects and /cd first."
        )
        return

    sessions = list_sessions(config.claude_projects_dir, state.project_dir_name)
    if not sessions:
        await update.message.reply_text("No sessions found in this project.")
        return

    lines = [f"Sessions in {state.working_directory}:\n"]
    for i, s in enumerate(sessions, 1):
        ts = s.timestamp[:16].replace("T", " ") if s.timestamp else "?"
        preview = s.first_message[:60]
        if len(s.first_message) > 60:
            preview += "..."
        lines.append(f"{i}. [{ts}] ({s.message_count} msgs)")
        lines.append(f"   \"{preview}\"")
        lines.append(f"   /resume_{s.session_id[:8]}")

    await update.message.reply_text("\n".join(lines))

    # Store for /resume by number
    context.chat_data["session_list"] = sessions


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    if not _is_allowed(update.effective_user.id, config):
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: /resume <session-id> or /resume <number> (from /sessions list)"
        )
        return

    arg = context.args[0]
    state = _get_state(update.effective_chat.id, config)

    # Try as a number (index from /sessions list)
    try:
        idx = int(arg) - 1
        session_list = context.chat_data.get("session_list", [])
        if 0 <= idx < len(session_list):
            s = session_list[idx]
            state.session_id = s.session_id
            claude_executor._created_sessions.add(s.session_id)  # mark as existing
            if s.cwd:
                state.working_directory = s.cwd
            await update.message.reply_text(
                f"Resumed session: {s.session_id[:8]}...\n"
                f"First message: \"{s.first_message[:60]}\"\n"
                f"Working dir: {state.working_directory}"
            )
            return
        else:
            await update.message.reply_text(f"Invalid number. Use 1-{len(session_list)}.")
            return
    except ValueError:
        pass

    # Try as session ID (full or prefix)
    session_id = arg

    # If it's a short prefix, try to find the full ID
    if len(session_id) < 36:
        # Search in current project first
        if state.project_dir_name:
            sessions = list_sessions(config.claude_projects_dir, state.project_dir_name, limit=50)
            for s in sessions:
                if s.session_id.startswith(session_id):
                    session_id = s.session_id
                    break

    # Try to find the session globally
    found = find_session(config.claude_projects_dir, session_id)
    if found:
        project_dir_name, cwd = found
        state.session_id = session_id
        state.project_dir_name = project_dir_name
        state.working_directory = cwd
        claude_executor._created_sessions.add(session_id)  # mark as existing
        await update.message.reply_text(
            f"Resumed session: {session_id[:8]}...\n"
            f"Working dir: {cwd}"
        )
    else:
        await update.message.reply_text(f"Session not found: {session_id}")


async def cmd_resume_shortcut(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /resume_<short_id> shortcuts from /sessions output."""
    config: Config = context.bot_data["config"]
    if not _is_allowed(update.effective_user.id, config):
        return

    # Extract session ID prefix from command like /resume_e520e26e
    cmd_text = update.message.text
    prefix = cmd_text.split("_", 1)[1] if "_" in cmd_text else ""
    if not prefix:
        return

    # Simulate /resume with this prefix
    context.args = [prefix]
    await cmd_resume(update, context)


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    if not _is_allowed(update.effective_user.id, config):
        return

    state = _get_state(update.effective_chat.id, config)
    old_id = state.session_id
    claude_executor._created_sessions.discard(old_id)
    state.session_id = str(uuid.uuid4())
    await update.message.reply_text("Conversation reset. Starting fresh!")


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    if not _is_allowed(update.effective_user.id, config):
        return

    if not context.args:
        await update.message.reply_text(
            f"Current model: {config.claude_model}\n"
            "Usage: /model <name>  (e.g. /model opus)"
        )
        return

    config.claude_model = context.args[0]
    await update.message.reply_text(f"Model set to: {config.claude_model}")


async def cmd_budget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    if not _is_allowed(update.effective_user.id, config):
        return

    if not context.args:
        current = config.claude_max_budget or "not set"
        await update.message.reply_text(
            f"Current budget: {current}\n"
            "Usage: /budget <amount>  (e.g. /budget 5.00)"
        )
        return

    config.claude_max_budget = context.args[0]
    await update.message.reply_text(f"Budget cap set to: ${config.claude_max_budget}")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    if not _is_allowed(update.effective_user.id, config):
        return

    state = _get_state(update.effective_chat.id, config)
    project = state.project_dir_name or "none"
    cwd = state.working_directory or "not set"
    sid = state.session_id[:8] + "..."

    await update.message.reply_text(
        f"Project: {project}\n"
        f"Working dir: {cwd}\n"
        f"Session: {sid}\n"
        f"Model: {config.claude_model}\n"
        f"Budget: {config.claude_max_budget or 'not set'}"
    )


# ---- Message handler ---------------------------------------------------------

async def _send_response(chat_id: int, text: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not text:
        text = "[No response from Claude]"

    chunks = split_message(text)
    for chunk in chunks:
        try:
            escaped = escape_markdown_v2(chunk)
            await context.bot.send_message(
                chat_id=chat_id, text=escaped, parse_mode=ParseMode.MARKDOWN_V2
            )
        except Exception:
            await context.bot.send_message(chat_id=chat_id, text=chunk)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    user_id = update.effective_user.id

    if not _is_allowed(user_id, config):
        await update.message.reply_text("You are not authorized to use this bot.")
        return

    prompt = update.message.text
    if not prompt:
        return

    chat_id = update.effective_chat.id
    state = _get_state(chat_id, config)
    cwd = state.working_directory or config.default_working_directory

    logger.info("User %s (chat %s) sent: %s", user_id, chat_id, prompt[:80])

    # Show typing indicator
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    typing_task = asyncio.ensure_future(_keep_typing(context.bot, chat_id))
    try:
        result = await claude_executor.execute(
            prompt, state.session_id, config, working_directory=cwd
        )
    finally:
        typing_task.cancel()

    if result.text:
        await _send_response(chat_id, result.text, context)

    if result.permission_denials:
        await _send_permission_request(
            chat_id, state.session_id, cwd, result.permission_denials, config, context
        )


async def _send_permission_request(
    chat_id: int,
    session_id: str,
    working_directory: str,
    denials: list[PermissionDenial],
    config: Config,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    callback_id = str(uuid.uuid4())[:8]

    has_bash = any(d.tool_name == "Bash" for d in denials)
    permission_mode = "bypassPermissions" if has_bash else "acceptEdits"

    _pending_permissions[callback_id] = {
        "chat_id": chat_id,
        "session_id": session_id,
        "working_directory": working_directory,
        "denials": denials,
        "config": config,
        "permission_mode": permission_mode,
    }

    text = _format_denials(denials)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Approve", callback_data=f"perm_approve:{callback_id}"),
            InlineKeyboardButton("Deny", callback_data=f"perm_deny:{callback_id}"),
        ]
    ])

    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)


async def handle_permission_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data
    if not data.startswith("perm_"):
        return

    action, callback_id = data.split(":", 1)
    pending = _pending_permissions.pop(callback_id, None)

    if not pending:
        await query.edit_message_text("This permission request has expired.")
        return

    if action == "perm_deny":
        await query.edit_message_text("Permission denied.")
        return

    # Approved
    chat_id = pending["chat_id"]
    session_id = pending["session_id"]
    working_directory = pending["working_directory"]
    config = pending["config"]
    permission_mode = pending["permission_mode"]

    await query.edit_message_text("Permission granted. Resuming...")

    typing_task = asyncio.ensure_future(_keep_typing(context.bot, chat_id))
    try:
        result = await claude_executor.execute(
            "Please proceed with the previously requested operations.",
            session_id,
            config,
            working_directory=working_directory,
            permission_mode=permission_mode,
        )
    finally:
        typing_task.cancel()

    if result.text:
        await _send_response(chat_id, result.text, context)

    if result.permission_denials:
        await _send_permission_request(
            chat_id, session_id, working_directory,
            result.permission_denials, config, context,
        )


async def _keep_typing(bot, chat_id: int) -> None:
    try:
        while True:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            await asyncio.sleep(5)
    except asyncio.CancelledError:
        pass


# ---- Bot setup ---------------------------------------------------------------

def create_app(config: Config) -> Application:
    app = Application.builder().token(config.telegram_bot_token).build()
    app.bot_data["config"] = config

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("projects", cmd_projects))
    app.add_handler(CommandHandler("cd", cmd_cd))
    app.add_handler(CommandHandler("sessions", cmd_sessions))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("budget", cmd_budget))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(handle_permission_callback, pattern=r"^perm_"))
    # Handle /resume_<id> shortcut commands
    app.add_handler(MessageHandler(
        filters.Regex(r"^/resume_[a-f0-9]+"), cmd_resume_shortcut
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    return app
