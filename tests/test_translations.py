"""Translation strings must survive the frontend's ICU message formatting."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_DIR = Path(__file__).parent.parent / "custom_components" / "calendar_bridge"
_FILES = [
    _DIR / "strings.json",
    _DIR / "translations" / "en.json",
    _DIR / "translations" / "de.json",
]


def _walk(node: object, path: str = ""):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _walk(value, f"{path}.{key}")
    elif isinstance(node, str):
        yield path, node


@pytest.mark.parametrize("file", _FILES, ids=lambda f: f.name)
def test_notify_message_template_text_has_no_braces(file: Path) -> None:
    """A literal `{summary}` makes the frontend raise MISSING_VALUE (issues 26 and 28).

    hassfest also forbids quoting it, so the help text names the placeholders
    in words instead.
    """
    texts = [
        text
        for path, text in _walk(json.loads(file.read_text("utf-8")))
        if path.endswith("data_description.notify_message_template")
    ]
    assert texts
    for text in texts:
        assert "{" not in text and "}" not in text
