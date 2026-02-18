import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass, field

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
    project_dir_name: str | None = None
    working_directory: str | None = None


_chat_states: dict[int, ChatState] = {}

# Pending permission requests: callback_id -> dict
_pending_permissions: dict[str, dict] = {}

# Temporary storage for callback data (projects/sessions lists)
_callback_data: dict[str, dict] = {}


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


def _short_path(path: str) -> str:
    """Shorten a path for button labels: /Users/foo/Desktop/myproject -> ~/Desktop/myproject"""
    home = os.path.expanduser("~")
    if path.startswith(home):
        return "~" + path[len(home):]
    return path


# ---- Onboarding & project/session buttons -----------------------------------

async def _send_onboarding(chat_id: int, config: Config, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send welcome message with project selection buttons."""
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "Welcome to teleg-ode!\n\n"
            "I bridge your Telegram to Claude Code running on your machine. "
            "You can chat with Claude, browse projects, and resume terminal sessions.\n\n"
            "Let's start by picking a project:"
        ),
    )
    await _send_project_buttons(chat_id, config, context)


async def _send_project_buttons(chat_id: int, config: Config, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send inline keyboard with project buttons."""
    projects = list_projects(config.claude_projects_dir)
    if not projects:
        await context.bot.send_message(
            chat_id=chat_id,
            text="No Claude Code projects found. Send a message to start a new conversation.",
        )
        return

    # Store projects for callback resolution
    cb_id = str(uuid.uuid4())[:8]
    _callback_data[cb_id] = {"type": "projects", "projects": projects}

    # Build button grid (1 project per row, max 8)
    buttons = []
    for i, p in enumerate(projects[:8]):
        label = _short_path(p.real_path)
        buttons.append([InlineKeyboardButton(
            f"{label}  ({p.session_count} sessions)",
            callback_data=f"proj:{cb_id}:{i}",
        )])

    if len(projects) > 8:
        buttons.append([InlineKeyboardButton(
            f"Show all ({len(projects)} projects)...",
            callback_data=f"proj:{cb_id}:more",
        )])

    await context.bot.send_message(
        chat_id=chat_id,
        text="Select a project:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _send_session_buttons(
    chat_id: int, config: Config, project_dir_name: str,
    working_directory: str, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Send inline keyboard with session buttons for a project."""
    sessions = list_sessions(config.claude_projects_dir, project_dir_name)

    cb_id = str(uuid.uuid4())[:8]
    _callback_data[cb_id] = {"type": "sessions", "sessions": sessions}

    buttons = []

    # "New conversation" first
    buttons.append([InlineKeyboardButton(
        "New conversation",
        callback_data=f"sess:{cb_id}:new",
    )])

    if sessions:
        for i, s in enumerate(sessions[:6]):
            ts = s.timestamp[:16].replace("T", " ") if s.timestamp else "?"
            preview = s.first_message[:40]
            if len(s.first_message) > 40:
                preview += "..."
            buttons.append([InlineKeyboardButton(
                f"{preview}  [{ts}]",
                callback_data=f"sess:{cb_id}:{i}",
            )])

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"Project: {_short_path(working_directory)}\n\nResume a session or start new:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ---- Callback handlers for buttons ------------------------------------------

async def handle_project_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle project selection button press."""
    query = update.callback_query
    await query.answer()

    config: Config = context.bot_data["config"]
    chat_id = query.message.chat_id

    parts = query.data.split(":")
    if len(parts) != 3:
        return

    _, cb_id, selection = parts
    cb_data = _callback_data.pop(cb_id, None)
    if not cb_data or cb_data["type"] != "projects":
        await query.edit_message_text("This selection has expired. Use /projects.")
        return

    projects = cb_data["projects"]
    state = _get_state(chat_id, config)

    if selection == "more":
        # Show all projects as text list
        _callback_data[cb_id] = cb_data  # re-store
        lines = ["All projects:\n"]
        for i, p in enumerate(projects, 1):
            lines.append(f"{i}. {_short_path(p.real_path)}  ({p.session_count} sessions)")
        lines.append("\nUse /cd <number> to switch.")
        context.chat_data["project_list"] = projects
        await query.edit_message_text("\n".join(lines))
        return

    try:
        idx = int(selection)
    except ValueError:
        return

    if 0 <= idx < len(projects):
        p = projects[idx]
        state.project_dir_name = p.dir_name
        state.working_directory = p.real_path
        state.session_id = str(uuid.uuid4())

        await query.edit_message_text(f"Selected: {_short_path(p.real_path)}")

        # Now show sessions for this project
        await _send_session_buttons(chat_id, config, p.dir_name, p.real_path, context)


async def handle_session_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle session selection button press."""
    query = update.callback_query
    await query.answer()

    config: Config = context.bot_data["config"]
    chat_id = query.message.chat_id

    parts = query.data.split(":")
    if len(parts) != 3:
        return

    _, cb_id, selection = parts
    cb_data = _callback_data.pop(cb_id, None)
    if not cb_data or cb_data["type"] != "sessions":
        await query.edit_message_text("This selection has expired. Use /sessions.")
        return

    sessions = cb_data["sessions"]
    state = _get_state(chat_id, config)

    if selection == "new":
        state.session_id = str(uuid.uuid4())
        claude_executor._created_sessions.discard(state.session_id)
        await query.edit_message_text(
            "New conversation started.\n\n"
            "Send a message to chat with Claude."
        )
        return

    try:
        idx = int(selection)
    except ValueError:
        return

    if 0 <= idx < len(sessions):
        s = sessions[idx]
        state.session_id = s.session_id
        claude_executor._created_sessions.add(s.session_id)
        if s.cwd:
            state.working_directory = s.cwd

        await query.edit_message_text(
            f"Resumed: \"{s.first_message[:60]}\"\n"
            f"Session: {s.session_id[:8]}...\n\n"
            "Send a message to continue."
        )


# ---- Command handlers -------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    if not _is_allowed(update.effective_user.id, config):
        await update.message.reply_text("You are not authorized to use this bot.")
        return

    chat_id = update.effective_chat.id
    await _send_onboarding(chat_id, config, context)


async def cmd_projects(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    if not _is_allowed(update.effective_user.id, config):
        return

    await _send_project_buttons(update.effective_chat.id, config, context)


async def cmd_cd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    if not _is_allowed(update.effective_user.id, config):
        return

    if not context.args:
        state = _get_state(update.effective_chat.id, config)
        cwd = state.working_directory or "not set"
        await update.message.reply_text(f"Current directory: {cwd}")
        await _send_project_buttons(update.effective_chat.id, config, context)
        return

    arg = " ".join(context.args)
    projects = list_projects(config.claude_projects_dir)

    # Try as a number
    try:
        idx = int(arg) - 1
        project_list = context.chat_data.get("project_list", projects)
        if 0 <= idx < len(project_list):
            p = project_list[idx]
            state = _get_state(update.effective_chat.id, config)
            state.project_dir_name = p.dir_name
            state.working_directory = p.real_path
            state.session_id = str(uuid.uuid4())
            await update.message.reply_text(f"Switched to: {_short_path(p.real_path)}")
            await _send_session_buttons(
                update.effective_chat.id, config, p.dir_name, p.real_path, context
            )
            return
        else:
            await update.message.reply_text(f"Invalid number. Use 1-{len(project_list)}.")
            return
    except ValueError:
        pass

    # Try as a path match
    for p in projects:
        if arg in p.real_path or arg in p.dir_name:
            state = _get_state(update.effective_chat.id, config)
            state.project_dir_name = p.dir_name
            state.working_directory = p.real_path
            state.session_id = str(uuid.uuid4())
            await update.message.reply_text(f"Switched to: {_short_path(p.real_path)}")
            await _send_session_buttons(
                update.effective_chat.id, config, p.dir_name, p.real_path, context
            )
            return

    await update.message.reply_text(f"Project not found: {arg}")


async def cmd_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    if not _is_allowed(update.effective_user.id, config):
        return

    state = _get_state(update.effective_chat.id, config)

    if not state.project_dir_name:
        await update.message.reply_text("No project selected.")
        await _send_project_buttons(update.effective_chat.id, config, context)
        return

    await _send_session_buttons(
        update.effective_chat.id, config,
        state.project_dir_name, state.working_directory, context,
    )


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    if not _is_allowed(update.effective_user.id, config):
        return

    if not context.args:
        # No args: show session buttons if project is selected
        state = _get_state(update.effective_chat.id, config)
        if state.project_dir_name:
            await _send_session_buttons(
                update.effective_chat.id, config,
                state.project_dir_name, state.working_directory, context,
            )
        else:
            await update.message.reply_text(
                "Usage: /resume <session-id> or select a project first with /projects"
            )
        return

    arg = context.args[0]
    state = _get_state(update.effective_chat.id, config)

    # Try as a number
    try:
        idx = int(arg) - 1
        session_list = context.chat_data.get("session_list", [])
        if 0 <= idx < len(session_list):
            s = session_list[idx]
            state.session_id = s.session_id
            claude_executor._created_sessions.add(s.session_id)
            if s.cwd:
                state.working_directory = s.cwd
            await update.message.reply_text(
                f"Resumed: \"{s.first_message[:60]}\"\n"
                f"Session: {s.session_id[:8]}...\n"
                f"Working dir: {_short_path(state.working_directory)}"
            )
            return
    except ValueError:
        pass

    # Try as session ID (full or prefix)
    session_id = arg
    if len(session_id) < 36 and state.project_dir_name:
        sessions = list_sessions(config.claude_projects_dir, state.project_dir_name, limit=50)
        for s in sessions:
            if s.session_id.startswith(session_id):
                session_id = s.session_id
                break

    found = find_session(config.claude_projects_dir, session_id)
    if found:
        project_dir_name, cwd = found
        state.session_id = session_id
        state.project_dir_name = project_dir_name
        state.working_directory = cwd
        claude_executor._created_sessions.add(session_id)
        await update.message.reply_text(
            f"Resumed session: {session_id[:8]}...\n"
            f"Working dir: {_short_path(cwd)}"
        )
    else:
        await update.message.reply_text(f"Session not found: {session_id}")


async def cmd_resume_shortcut(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /resume_<short_id> shortcuts."""
    config: Config = context.bot_data["config"]
    if not _is_allowed(update.effective_user.id, config):
        return

    cmd_text = update.message.text
    prefix = cmd_text.split("_", 1)[1] if "_" in cmd_text else ""
    if not prefix:
        return

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
    cwd = _short_path(state.working_directory) if state.working_directory else "not set"
    sid = state.session_id[:8] + "..."

    lines = [
        f"Project: {cwd}",
        f"Session: {sid}",
        f"Model: {config.claude_model}",
        f"Budget: {config.claude_max_budget or 'not set'}",
    ]

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Switch project", callback_data="nav:projects"),
            InlineKeyboardButton("Switch session", callback_data="nav:sessions"),
        ],
        [
            InlineKeyboardButton("New conversation", callback_data="nav:new"),
        ],
    ])

    await update.message.reply_text("\n".join(lines), reply_markup=keyboard)


# ---- Navigation callback (from /status buttons) -----------------------------

async def handle_nav_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    config: Config = context.bot_data["config"]
    chat_id = query.message.chat_id

    action = query.data.split(":")[1]

    if action == "projects":
        await query.edit_message_text("Select a project:")
        await _send_project_buttons(chat_id, config, context)

    elif action == "sessions":
        state = _get_state(chat_id, config)
        if state.project_dir_name:
            await query.edit_message_text("Select a session:")
            await _send_session_buttons(
                chat_id, config, state.project_dir_name, state.working_directory, context
            )
        else:
            await query.edit_message_text("No project selected. Pick one first:")
            await _send_project_buttons(chat_id, config, context)

    elif action == "new":
        state = _get_state(chat_id, config)
        claude_executor._created_sessions.discard(state.session_id)
        state.session_id = str(uuid.uuid4())
        await query.edit_message_text("New conversation started. Send a message!")


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

    # No project selected: show onboarding
    if not state.project_dir_name:
        await _send_onboarding(chat_id, config, context)
        return

    cwd = state.working_directory or config.default_working_directory

    logger.info("User %s (chat %s) sent: %s", user_id, chat_id, prompt[:80])

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

    # Show tool errors that Claude may have glossed over
    if result.tool_errors:
        error_lines = ["Tool errors encountered:"]
        for err in result.tool_errors:
            error_lines.append(f"  {err.message[:300]}")
        await context.bot.send_message(chat_id=chat_id, text="\n".join(error_lines))

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

    if result.tool_errors:
        error_lines = ["Tool errors encountered:"]
        for err in result.tool_errors:
            error_lines.append(f"  {err.message[:300]}")
        await context.bot.send_message(chat_id=chat_id, text="\n".join(error_lines))

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


# ---- Error handler -----------------------------------------------------------

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors and notify the user on Telegram."""
    logger.error("Exception while handling update:", exc_info=context.error)

    # Try to notify the user
    if isinstance(update, Update) and update.effective_chat:
        try:
            error_msg = str(context.error)
            if len(error_msg) > 300:
                error_msg = error_msg[:300] + "..."
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"Error: {error_msg}",
            )
        except Exception:
            pass  # Can't even send the error message


# ---- Bot setup ---------------------------------------------------------------

def create_app(config: Config) -> Application:
    app = Application.builder().token(config.telegram_bot_token).build()
    app.bot_data["config"] = config

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("projects", cmd_projects))
    app.add_handler(CommandHandler("cd", cmd_cd))
    app.add_handler(CommandHandler("sessions", cmd_sessions))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("budget", cmd_budget))
    app.add_handler(CommandHandler("status", cmd_status))

    # Button callbacks
    app.add_handler(CallbackQueryHandler(handle_project_callback, pattern=r"^proj:"))
    app.add_handler(CallbackQueryHandler(handle_session_callback, pattern=r"^sess:"))
    app.add_handler(CallbackQueryHandler(handle_permission_callback, pattern=r"^perm_"))
    app.add_handler(CallbackQueryHandler(handle_nav_callback, pattern=r"^nav:"))

    # /resume_<id> shortcut
    app.add_handler(MessageHandler(
        filters.Regex(r"^/resume_[a-f0-9]+"), cmd_resume_shortcut
    ))

    # Regular messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Global error handler
    app.add_error_handler(error_handler)

    return app
