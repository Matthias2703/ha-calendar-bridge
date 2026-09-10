"""Tests for the default-target invariant in device.py."""

from __future__ import annotations

from dataclasses import dataclass, field

from custom_components.calendar_bridge.const import CONF_DEFAULT_TARGET, DOMAIN
from custom_components.calendar_bridge.device import async_clear_other_defaults


@dataclass
class _FakeSubentry:
    data: dict[str, object]


@dataclass
class _FakeEntry:
    entry_id: str
    subentries: dict[str, _FakeSubentry] = field(default_factory=dict)
    domain: str = DOMAIN


class _FakeConfigEntries:
    """Duck-typed stand-in for hass.config_entries."""

    def __init__(self, entries: list[_FakeEntry]) -> None:
        self._entries = entries

    def async_entries(self, domain: str) -> list[_FakeEntry]:
        return [e for e in self._entries if e.domain == domain]

    def async_update_subentry(
        self, entry: _FakeEntry, subentry: _FakeSubentry, *, data: dict[str, object]
    ) -> None:
        subentry.data = data


class _FakeHass:
    def __init__(self, entries: list[_FakeEntry]) -> None:
        self.config_entries = _FakeConfigEntries(entries)


def test_clears_other_default_within_the_same_entry() -> None:
    kept = _FakeSubentry(data={CONF_DEFAULT_TARGET: True})
    other = _FakeSubentry(data={CONF_DEFAULT_TARGET: True})
    entry = _FakeEntry(entry_id="e1", subentries={"kept": kept, "other": other})
    hass = _FakeHass([entry])

    async_clear_other_defaults(hass, entry, "kept")  # type: ignore[arg-type]

    assert kept.data[CONF_DEFAULT_TARGET] is True
    assert other.data[CONF_DEFAULT_TARGET] is False


def test_clears_a_default_marked_on_a_different_account() -> None:
    # async_find_default_device searches across every account -- a default
    # marked on a second, unrelated CalDAV/Google entry must be cleared too,
    # not just siblings of the one just marked.
    kept = _FakeSubentry(data={CONF_DEFAULT_TARGET: True})
    entry_a = _FakeEntry(entry_id="a", subentries={"kept": kept})
    other_on_b = _FakeSubentry(data={CONF_DEFAULT_TARGET: True})
    entry_b = _FakeEntry(entry_id="b", subentries={"other": other_on_b})
    hass = _FakeHass([entry_a, entry_b])

    async_clear_other_defaults(hass, entry_a, "kept")  # type: ignore[arg-type]

    assert other_on_b.data[CONF_DEFAULT_TARGET] is False


def test_keep_entry_none_clears_every_existing_default() -> None:
    # Used before a brand-new account entry is created -- its own subentries
    # don't exist yet, so there's nothing to exclude.
    existing = _FakeSubentry(data={CONF_DEFAULT_TARGET: True})
    entry = _FakeEntry(entry_id="a", subentries={"existing": existing})
    hass = _FakeHass([entry])

    async_clear_other_defaults(hass, None, None)  # type: ignore[arg-type]

    assert existing.data[CONF_DEFAULT_TARGET] is False


def test_does_not_touch_subentries_that_are_not_marked_default() -> None:
    not_default = _FakeSubentry(data={CONF_DEFAULT_TARGET: False, "other_key": "unchanged"})
    entry = _FakeEntry(entry_id="a", subentries={"not_default": not_default})
    hass = _FakeHass([entry])

    async_clear_other_defaults(hass, None, None)  # type: ignore[arg-type]

    assert not_default.data == {CONF_DEFAULT_TARGET: False, "other_key": "unchanged"}
