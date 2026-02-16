import asyncio
import json
import logging
import os
from dataclasses import dataclass, field

from config import Config

logger = logging.getLogger(__name__)

# Track which session IDs have been created (first message) vs need resuming
_created_sessions: set[str] = set()


@dataclass
class PermissionDenial:
    tool_name: str
    tool_input: dict


@dataclass
class ExecuteResult:
    text: str
    permission_denials: list[PermissionDenial] = field(default_factory=list)


def _build_cmd(
    session_id: str,
    config: Config,
    permission_mode: str | None = None,
    is_resume: bool = False,
) -> list[str]:
    cmd = [
        "claude",
        "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--model", config.claude_model,
    ]

    if is_resume or session_id in _created_sessions:
        cmd.extend(["--resume", session_id])
    else:
        cmd.extend(["--session-id", session_id])

    if permission_mode:
        cmd.extend(["--permission-mode", permission_mode])

    if config.claude_max_budget:
        cmd.extend(["--max-budget-usd", config.claude_max_budget])

    if config.claude_allowed_tools:
        cmd.extend(["--allowedTools", config.claude_allowed_tools])

    return cmd


async def execute(
    prompt: str,
    session_id: str,
    config: Config,
    working_directory: str,
    permission_mode: str | None = None,
    is_resume: bool = False,
) -> ExecuteResult:
    """Run claude CLI and collect the full response.

    Returns an ExecuteResult with the text and any permission denials.
    """
    cmd = _build_cmd(session_id, config, permission_mode, is_resume)

    logger.info("Running: %s (cwd=%s)", " ".join(cmd), working_directory)

    # Strip CLAUDECODE env var so the child process doesn't think
    # it's nested inside another Claude Code session.
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_directory,
            env=env,
        )
    except FileNotFoundError:
        return ExecuteResult(
            text="Error: `claude` CLI not found. Make sure Claude Code is installed and on your PATH."
        )

    # Send the prompt on stdin and close it
    proc.stdin.write(prompt.encode())
    await proc.stdin.drain()
    proc.stdin.close()

    # Read stdout line by line (stream-json produces one JSON object per line)
    collected_text: list[str] = []
    permission_denials: list[PermissionDenial] = []

    try:
        while True:
            line = await asyncio.wait_for(
                proc.stdout.readline(), timeout=300  # 5 min timeout per line
            )
            if not line:
                break

            line_str = line.decode().strip()
            if not line_str:
                continue

            try:
                data = json.loads(line_str)
            except json.JSONDecodeError:
                logger.debug("Non-JSON line from claude: %s", line_str)
                continue

            msg_type = data.get("type")

            if msg_type == "assistant":
                for block in data.get("message", {}).get("content", []):
                    if block.get("type") == "text":
                        text = block.get("text", "")
                        if text:
                            collected_text.append(text)

            elif msg_type == "content_block_delta":
                delta = data.get("delta", {})
                if delta.get("type") == "text_delta":
                    text = delta.get("text", "")
                    if text:
                        collected_text.append(text)

            elif msg_type == "result":
                result_text = data.get("result", "")
                if result_text and not collected_text:
                    collected_text.append(result_text)

                # Capture permission denials
                for denial in data.get("permission_denials", []):
                    permission_denials.append(
                        PermissionDenial(
                            tool_name=denial.get("tool_name", "unknown"),
                            tool_input=denial.get("tool_input", {}),
                        )
                    )

    except asyncio.TimeoutError:
        proc.kill()
        return ExecuteResult(text="[Timed out waiting for Claude response]")

    await proc.wait()

    if proc.returncode == 0 or collected_text:
        _created_sessions.add(session_id)

    if proc.returncode and proc.returncode != 0 and not collected_text:
        stderr_bytes = await proc.stderr.read()
        stderr_text = stderr_bytes.decode().strip()
        if stderr_text:
            logger.error("claude stderr: %s", stderr_text)
        error_msg = f"Error: Claude CLI exited with code {proc.returncode}."
        if stderr_text:
            error_msg += f"\n{stderr_text}"
        return ExecuteResult(text=error_msg)

    return ExecuteResult(
        text="".join(collected_text).strip(),
        permission_denials=permission_denials,
    )
