import uuid


def chat_id_to_uuid(chat_id: int) -> str:
    """Convert a Telegram chat ID to a deterministic UUID5."""
    namespace = uuid.UUID("a1b3c4d5-e6f7-8901-2345-6789abcdef01")
    return str(uuid.uuid5(namespace, str(chat_id)))


def split_message(text: str, max_len: int = 4096) -> list[str]:
    """Split a message into chunks that fit Telegram's limit.

    Tries to split at paragraph boundaries, then line boundaries,
    then hard-cuts as a last resort.
    """
    if len(text) <= max_len:
        return [text]

    chunks = []
    remaining = text

    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break

        # Try to split at a double newline (paragraph boundary)
        split_pos = remaining.rfind("\n\n", 0, max_len)
        if split_pos > max_len // 2:
            chunks.append(remaining[:split_pos])
            remaining = remaining[split_pos + 2:]
            continue

        # Try to split at a single newline
        split_pos = remaining.rfind("\n", 0, max_len)
        if split_pos > max_len // 2:
            chunks.append(remaining[:split_pos])
            remaining = remaining[split_pos + 1:]
            continue

        # Try to split at a space
        split_pos = remaining.rfind(" ", 0, max_len)
        if split_pos > max_len // 2:
            chunks.append(remaining[:split_pos])
            remaining = remaining[split_pos + 1:]
            continue

        # Hard cut
        chunks.append(remaining[:max_len])
        remaining = remaining[max_len:]

    return chunks


_MARKDOWN_V2_SPECIAL = r"_*[]()~`>#+-=|{}.!"


def escape_markdown_v2(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2.

    This does a simple full escape. For messages that contain intentional
    formatting, send as plain text or use HTML parse mode instead.
    """
    result = []
    for ch in text:
        if ch in _MARKDOWN_V2_SPECIAL:
            result.append("\\")
        result.append(ch)
    return "".join(result)
