import logging
import shutil
import sys

from config import Config
from platforms.telegram_bot import create_app

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def main() -> None:
    config = Config()

    errors = config.validate()
    if errors:
        for e in errors:
            logger.error(e)
        sys.exit(1)

    if not shutil.which("claude"):
        logger.error(
            "claude CLI not found on PATH. "
            "Install Claude Code first: https://docs.anthropic.com/en/docs/claude-code"
        )
        sys.exit(1)

    logger.info("Starting teleg-ode Telegram bot...")
    logger.info("Projects dir: %s", config.claude_projects_dir)
    logger.info("Default working dir: %s", config.default_working_directory)
    logger.info("Model: %s", config.claude_model)
    if config.allowed_user_ids:
        logger.info("Restricted to user IDs: %s", config.allowed_user_ids)
    else:
        logger.info("No user restrictions (anyone can use the bot)")

    app = create_app(config)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
