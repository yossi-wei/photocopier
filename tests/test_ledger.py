from __future__ import annotations

from pathlib import Path

import pytest

from photocopier.ledger import Ledger, State
from photocopier.rclone import RemoteItem


def item(path: str, *, size: int = 100, digest: str = "abc") -> RemoteItem:
    return RemoteItem(
        path=path, name=Path(path).name, size=size, mod_time="2026-06-01T00:00:00Z", hash=digest
    )


@pytest.fixture
def ledger(tmp_path: Path):
    with Ledger(tmp_path / "ledger.db") as ledger:
        yield ledger


def test_filter_new_returns_everything_when_empty(ledger: Ledger) -> None:
    items = [item("a.jpg"), item("b.jpg")]
    assert ledger.filter_new("phone-1", items) == items


def test_recorded_items_are_not_new_again(ledger: Ledger, tmp_path: Path) -> None:
    first, second = item("a.jpg"), item("b.jpg")
    ledger.record_ingested("phone-1", first, tmp_path / "a.jpg")
    assert ledger.filter_new("phone-1", [first, second]) == [second]


def test_sources_are_isolated(ledger: Ledger, tmp_path: Path) -> None:
    """Two phones can hold identical filenames; they must not mask each other."""
    shared = item("2026/06/PXL_20260601_120000.jpg")
    ledger.record_ingested("phone-1", shared, tmp_path / "a.jpg")
    assert ledger.filter_new("phone-2", [shared]) == [shared]


def test_same_path_different_content_is_a_new_item(ledger: Ledger, tmp_path: Path) -> None:
    """D6: identity includes the hash, so a re-upload cannot silently overwrite."""
    original = item("a.jpg", digest="hash-one")
    replaced = item("a.jpg", digest="hash-two")
    ledger.record_ingested("phone-1", original, tmp_path / "a.jpg")
    assert ledger.filter_new("phone-1", [replaced]) == [replaced]


def test_missing_hashes_do_not_duplicate(ledger: Ledger, tmp_path: Path) -> None:
    """SQLite treats NULLs as distinct in UNIQUE constraints.

    If an absent hash were stored as NULL, the same file would insert repeatedly and
    re-download forever — the exact failure the ledger exists to prevent. It is
    normalised to '' instead.
    """
    unhashed = item("a.jpg", digest="")
    ledger.record_ingested("phone-1", unhashed, tmp_path / "a.jpg")
    ledger.record_ingested("phone-1", unhashed, tmp_path / "a.jpg")

    assert ledger.filter_new("phone-1", [unhashed]) == []
    assert ledger.counts()[State.INGESTED.value] == 1


def test_record_ingested_is_idempotent(ledger: Ledger, tmp_path: Path) -> None:
    single = item("a.jpg")
    for _ in range(3):
        ledger.record_ingested("phone-1", single, tmp_path / "a.jpg")
    assert ledger.counts() == {State.INGESTED.value: 1}


def test_counts_group_by_state(ledger: Ledger, tmp_path: Path) -> None:
    ledger.record_ingested("phone-1", item("a.jpg"), tmp_path / "a.jpg")
    ledger.record_ingested("phone-1", item("b.jpg", digest="two"), tmp_path / "b.jpg")
    ledger.record_failure("phone-1", item("c.jpg", digest="three"), "network died")

    counts = ledger.counts()
    assert counts[State.INGESTED.value] == 2
    assert counts[State.FAILED.value] == 1


def test_failed_items_are_offered_again(ledger: Ledger) -> None:
    """A failed item has not been obtained, so it must not count as known.

    Treating it as known would skip it permanently on every later run — silent data
    loss of exactly the kind D1 rejected --max-age for.
    """
    flaky = item("a.jpg")
    ledger.record_failure("phone-1", flaky, "network died mid-transfer")
    assert ledger.filter_new("phone-1", [flaky]) == [flaky]


def test_failure_then_success_clears_the_error(ledger: Ledger, tmp_path: Path) -> None:
    flaky = item("a.jpg")
    ledger.record_failure("phone-1", flaky, "timeout")
    ledger.record_ingested("phone-1", flaky, tmp_path / "a.jpg")

    assert ledger.count_in_state(State.INGESTED) == 1
    assert ledger.count_in_state(State.FAILED) == 0


def test_repeated_failures_increment_attempts(ledger: Ledger) -> None:
    flaky = item("a.jpg")
    ledger.record_failure("phone-1", flaky, "timeout")
    ledger.record_failure("phone-1", flaky, "timeout again")

    row = ledger.conn.execute("SELECT attempts, last_error FROM items").fetchone()
    assert row["attempts"] == 2
    assert row["last_error"] == "timeout again"


def test_total_bytes_in_state(ledger: Ledger, tmp_path: Path) -> None:
    ledger.record_ingested("phone-1", item("a.jpg", size=1000), tmp_path / "a.jpg")
    ledger.record_ingested("phone-1", item("b.jpg", size=2500, digest="two"), tmp_path / "b.jpg")
    assert ledger.total_bytes_in_state(State.INGESTED) == 3500


def test_forget_allows_reingestion(ledger: Ledger, tmp_path: Path) -> None:
    single = item("a.jpg")
    ledger.record_ingested("phone-1", single, tmp_path / "a.jpg")
    ledger.forget("phone-1", "a.jpg", "abc")
    assert ledger.filter_new("phone-1", [single]) == [single]


def test_survives_reopening(tmp_path: Path) -> None:
    """The ledger is a file, not a session. It has to outlive the process."""
    path = tmp_path / "ledger.db"
    with Ledger(path) as first:
        first.record_ingested("phone-1", item("a.jpg"), tmp_path / "a.jpg")
    with Ledger(path) as second:
        assert second.filter_new("phone-1", [item("a.jpg")]) == []
