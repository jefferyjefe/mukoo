"""Tests for the refresh decision logic and state round-trip (DB-free)."""

from __future__ import annotations

from mukoo_model.refresh import load_state, save_state, should_refresh


def test_refresh_when_no_previous_state():
    assert should_refresh(None, {"count": 10, "max_id": 10})
    assert should_refresh({}, {"count": 10, "max_id": 10})


def test_no_refresh_when_unchanged():
    state = {"count": 1459, "max_id": 1459}
    assert not should_refresh(state, {"count": 1459, "max_id": 1459})


def test_refresh_on_new_rows():
    prev = {"count": 100, "max_id": 100}
    assert should_refresh(prev, {"count": 150, "max_id": 150})


def test_refresh_on_delete_and_on_replace():
    prev = {"count": 100, "max_id": 100}
    # deletion: count drops
    assert should_refresh(prev, {"count": 90, "max_id": 100})
    # replace: same count, new ids
    assert should_refresh(prev, {"count": 100, "max_id": 120})


def test_state_roundtrip_and_corrupt_state(tmp_path):
    path = tmp_path / "state.json"
    assert load_state(path) is None  # missing -> None, not an exception
    save_state(path, {"count": 5, "max_id": 7, "extra": "kept"})
    assert load_state(path) == {"count": 5, "max_id": 7, "extra": "kept"}
    path.write_text("{ truncated")
    assert load_state(path) is None  # corrupt -> None -> refresh happens
