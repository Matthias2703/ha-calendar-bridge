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

# Texts the code fills in at runtime, with exactly the placeholders it supplies.
# Braces anywhere else leave the frontend without a value for them.
_SUPPLIED_PLACEHOLDERS = {
    "application_credentials.description": {
        "oauth_consent_url",
        "oauth_creds_url",
        "redirect_url",
        "more_info_url",
    },
    "config.progress.wait_for_code": {"url", "user_code"},
    "exceptions.calendar_not_found.message": {"device_id"},
    "exceptions.calendar_not_loaded.message": {"device_id"},
    "exceptions.calendar_unavailable.message": {"device_id"},
    "exceptions.device_not_found.message": {"device_id"},
    "exceptions.event_not_found.message": {"uid"},
    "issues.stale_calendar.description": {"name"},
    "issues.stale_calendar.title": {"name"},
}


def _walk(node: object, path: str = ""):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _walk(value, f"{path}.{key}" if path else key)
    elif isinstance(node, str):
        yield path, node


@pytest.mark.parametrize("file", _FILES, ids=lambda f: f.name)
def test_braces_only_name_supplied_placeholders(file: Path) -> None:
    """A literal `{summary}` makes the frontend raise MISSING_VALUE (issues 26 and 28).

    hassfest also forbids quoting it, so help texts name template placeholders
    in words instead.
    """
    texts = dict(_walk(json.loads(file.read_text("utf-8"))))
    assert texts

    unexpected = {}
    for path, text in texts.items():
        remainder = text
        for name in _SUPPLIED_PLACEHOLDERS.get(path, set()):
            remainder = remainder.replace(f"{{{name}}}", "")
        if "{" in remainder or "}" in remainder:
            unexpected[path] = text
    assert not unexpected
