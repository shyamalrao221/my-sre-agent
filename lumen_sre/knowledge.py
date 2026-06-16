import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DEFAULT_CONTEXT_PATH = Path(__file__).resolve().parents[1] / "runbooks" / "context.md"


def get_developer_context(topic: str = "") -> str:
    """Returns local runbook or architecture context for developer-facing SRE questions."""
    configured_path = os.getenv("SRE_RUNBOOK_PATH")
    context_path = Path(configured_path) if configured_path else DEFAULT_CONTEXT_PATH

    if not context_path.exists():
        return f"Developer context unavailable. Expected runbook at {context_path}."

    content = context_path.read_text(encoding="utf-8")
    topic = topic.strip().lower()
    if not topic:
        return content

    topic_words = [word for word in topic.split() if len(word) > 2]
    matched_blocks = []
    for block in content.split("\n\n"):
        lowered_block = block.lower()
        if any(word in lowered_block for word in topic_words):
            matched_blocks.append(block.strip())

    if matched_blocks:
        return "\n\n".join(matched_blocks)

    return f"No targeted runbook section found for '{topic}'. Full context:\n\n{content}"