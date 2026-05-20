"""Extract Python code + tests from a bot-build message thread.

Bots post code in fenced markdown blocks. This module pulls the
relevant blocks out so downstream Code Review can run on a single
artifact.

Heuristics (in priority order):
  1. The LAST fenced ```python block whose body contains `def test_`
     or starts with `import pytest`/`import unittest` is the tests.
  2. The LAST non-test ```python block is the code.
  3. If multiple non-test blocks exist (e.g., workshop mode where
     bot-a posts v0 and bot-b posts v1), prefer the latest by
     message created_at.
  4. If only one block exists and it has no test markers, treat it
     as code with empty tests.

The bot personas are instructed to post full file bodies (not
diffs) so we can rely on grabbing the latest block verbatim.
"""

from __future__ import annotations

import re
from typing import Any


_FENCED_PYTHON = re.compile(
    r"```(?:python|py)\s*\n(?P<body>.*?)\n```",
    flags=re.DOTALL | re.IGNORECASE,
)

_TEST_MARKERS = (
    "def test_",
    "import pytest",
    "from pytest",
    "import unittest",
    "from unittest",
)


def _is_test_block(body: str) -> bool:
    head = body.lstrip()
    if any(head.startswith(marker) for marker in _TEST_MARKERS):
        return True
    # `def test_` anywhere in the file marks it as tests even if
    # imports come first.
    if "\ndef test_" in body or body.startswith("def test_"):
        return True
    return False


def extract_code(messages: list[dict[str, Any]]) -> dict[str, str]:
    """Return {"code": str, "tests": str}. Either may be empty.

    `messages` is the get_messages() output — a list of dicts with at
    minimum a `content` field. Order does not need to be pre-sorted;
    we sort by `created_at` defensively.
    """
    sorted_msgs = sorted(messages, key=lambda m: m.get("created_at", ""))

    code_blocks: list[str] = []
    test_blocks: list[str] = []

    for m in sorted_msgs:
        content = m.get("content") or ""
        for match in _FENCED_PYTHON.finditer(content):
            body = match.group("body")
            if _is_test_block(body):
                test_blocks.append(body)
            else:
                code_blocks.append(body)

    code = code_blocks[-1] if code_blocks else ""
    tests = test_blocks[-1] if test_blocks else ""
    return {"code": code, "tests": tests}


def extract_code_from_string(text: str) -> dict[str, str]:
    """Same heuristics, but operates on a single string (e.g., the
    iterator's v2 message body). Useful when bypassing the message
    thread.
    """
    return extract_code([{"content": text, "created_at": ""}])
