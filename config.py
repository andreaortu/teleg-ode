import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


class Config:
    def __init__(self):
        self.telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
        allowed = os.getenv("ALLOWED_USER_IDS", "")
        self.allowed_user_ids: set[int] = (
            {int(uid.strip()) for uid in allowed.split(",") if uid.strip()}
            if allowed
            else set()
        )
        self.claude_model: str = os.getenv("CLAUDE_MODEL", "sonnet")
        self.claude_max_budget: str | None = os.getenv("CLAUDE_MAX_BUDGET")
        self.claude_allowed_tools: str | None = os.getenv("CLAUDE_ALLOWED_TOOLS")
        self.claude_projects_dir: str = os.getenv(
            "CLAUDE_PROJECTS_DIR",
            str(Path.home() / ".claude" / "projects"),
        )
        self.default_working_directory: str = os.getenv(
            "DEFAULT_WORKING_DIRECTORY", str(Path.home())
        )

    def validate(self) -> list[str]:
        errors = []
        if not self.telegram_bot_token:
            errors.append("TELEGRAM_BOT_TOKEN is required")
        if not Path(self.claude_projects_dir).is_dir():
            errors.append(
                f"Claude projects directory not found: {self.claude_projects_dir}"
            )
        return errors
