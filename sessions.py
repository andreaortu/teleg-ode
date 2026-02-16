import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class ProjectInfo:
    """A Claude Code project discovered from ~/.claude/projects/."""
    dir_name: str       # e.g. "-Users-andreaortu-Desktop-myproject"
    real_path: str      # e.g. "/Users/andreaortu/Desktop/myproject"
    session_count: int


@dataclass
class SessionInfo:
    """Summary of a Claude Code session."""
    session_id: str
    cwd: str
    first_message: str
    timestamp: str
    message_count: int


def _dir_name_to_path(dir_name: str) -> str:
    """Convert a Claude projects dir name back to a real path.

    e.g. '-Users-andreaortu-Desktop-myproject' -> '/Users/andreaortu/Desktop/myproject'
    """
    # The dir name is the path with / replaced by - and a leading -
    # We reverse this: strip leading -, replace - with /
    # But this is ambiguous (dirs with dashes). We use a heuristic:
    # try to find the longest existing path.
    parts = dir_name.lstrip("-").split("-")
    # Try progressively joining parts with /
    best_path = "/" + "/".join(parts)

    # Try to find a real existing path by testing from the left
    for i in range(len(parts), 0, -1):
        candidate = "/" + "/".join(parts[:i])
        if Path(candidate).exists():
            # Found a real prefix, the rest might have dashes in folder names
            remaining = parts[i:]
            if remaining:
                best_path = candidate + "/" + "-".join(remaining)
            else:
                best_path = candidate
            break

    return best_path


def list_projects(projects_dir: str) -> list[ProjectInfo]:
    """List all Claude Code projects."""
    projects_path = Path(projects_dir)
    if not projects_path.is_dir():
        return []

    projects = []
    for entry in sorted(projects_path.iterdir()):
        if not entry.is_dir():
            continue
        session_files = list(entry.glob("*.jsonl"))
        if not session_files:
            continue
        projects.append(ProjectInfo(
            dir_name=entry.name,
            real_path=_dir_name_to_path(entry.name),
            session_count=len(session_files),
        ))

    return projects


def list_sessions(projects_dir: str, project_dir_name: str, limit: int = 10) -> list[SessionInfo]:
    """List recent sessions for a project, sorted by most recent first."""
    project_path = Path(projects_dir) / project_dir_name
    if not project_path.is_dir():
        return []

    session_files = sorted(
        project_path.glob("*.jsonl"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )[:limit]

    sessions = []
    for sf in session_files:
        info = _parse_session_summary(sf)
        if info:
            sessions.append(info)

    return sessions


def _parse_session_summary(session_file: Path) -> SessionInfo | None:
    """Parse a session JSONL file to extract a summary."""
    first_message = ""
    timestamp = ""
    cwd = ""
    session_id = session_file.stem
    message_count = 0

    try:
        with open(session_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if data.get("type") == "user":
                    message_count += 1
                    if not first_message:
                        msg = data.get("message", {})
                        content = msg.get("content", "")
                        if isinstance(content, str):
                            first_message = content[:100]
                        elif isinstance(content, list):
                            # Content blocks
                            for block in content:
                                if isinstance(block, dict) and block.get("type") == "text":
                                    first_message = block.get("text", "")[:100]
                                    break
                        timestamp = data.get("timestamp", "")
                        cwd = data.get("cwd", "")
    except Exception as e:
        logger.debug("Error parsing session %s: %s", session_file, e)
        return None

    if not first_message:
        return None

    return SessionInfo(
        session_id=session_id,
        cwd=cwd,
        first_message=first_message,
        timestamp=timestamp,
        message_count=message_count,
    )


def find_session(projects_dir: str, session_id: str) -> tuple[str, str] | None:
    """Find which project a session belongs to.

    Returns (project_dir_name, cwd) or None if not found.
    """
    projects_path = Path(projects_dir)
    if not projects_path.is_dir():
        return None

    for entry in projects_path.iterdir():
        if not entry.is_dir():
            continue
        session_file = entry / f"{session_id}.jsonl"
        if session_file.exists():
            # Extract cwd from the session
            cwd = _dir_name_to_path(entry.name)
            try:
                with open(session_file) as f:
                    for line in f:
                        data = json.loads(line.strip())
                        if data.get("type") == "user" and data.get("cwd"):
                            cwd = data["cwd"]
                            break
            except Exception:
                pass
            return entry.name, cwd

    return None
