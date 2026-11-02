"""The progress line: drawn only for a watcher, never into an unattended run's log."""

from __future__ import annotations

import io
from contextlib import contextmanager
from pathlib import Path

import pytest

from photocopier.flush import flush
from photocopier.ingest import ingest, landed_so_far
from photocopier.ledger import Ledger
from photocopier.process import process
from photocopier.progress import CLEAR_LINE, Progress, Task
from photocopier.rclone import Rclone

from .conftest import populate

CAMERA_ROLL = "Camera Roll"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class TtyStream(io.StringIO):
    def isatty(self) -> bool:
        return True


class Recorder:
    """Stands in for Progress in the stages, keeping every task it was asked for."""

    def __init__(self) -> None:
        self.tasks: list[Task] = []

    @contextmanager
    def task(self, label, *, items=None, total_bytes=None, poll=None):
        task = Task(label, items=items, total_bytes=total_bytes, poll=poll, clock=FakeClock())
        self.tasks.append(task)
        yield task


def make_task(clock: FakeClock, **kwargs) -> Task:
    return Task(
        kwargs.pop("label", "flush"),
        items=kwargs.pop("items", None),
        total_bytes=kwargs.pop("total_bytes", None),
        poll=kwargs.pop("poll", None),
        clock=clock,
    )


class TestLine:
    def test_counts_bytes_rate_and_time_left(self) -> None:
        clock = FakeClock()
        task = make_task(clock, items=10, total_bytes=100 * 1024**2)
        task.line(200)  # first sample

        clock.now += 10
        task.advance(items=4, nbytes=40 * 1024**2)
        task.current("PXL_20250714_183022.jpg")
        line = task.line(200)

        assert line.startswith("flush")
        assert "4/10" in line
        assert "40.0 MB / 100.0 MB  40%" in line
        assert "4.0 MB/s" in line
        assert "~15s left" in line
        assert line.endswith("PXL_20250714_183022.jpg")

    def test_no_rate_until_there_is_history(self) -> None:
        clock = FakeClock()
        task = make_task(clock, items=2, total_bytes=1000)
        task.advance(nbytes=500)
        assert "/s" not in task.line(200)

    def test_unknown_size_shows_a_spinner_and_elapsed_time(self) -> None:
        clock = FakeClock()
        task = make_task(clock, label="listing phone-1")
        clock.now += 75
        assert "1:15" in task.line(200)

    def test_fits_the_terminal(self) -> None:
        clock = FakeClock()
        task = make_task(clock, items=1, total_bytes=10)
        task.current("x" * 300)
        assert len(task.line(40)) == 39

    def test_poll_overrides_the_counts(self) -> None:
        task = make_task(FakeClock(), items=5, total_bytes=500, poll=lambda: (3, 300))
        task.sample()
        assert (task.items_done, task.bytes_done) == (3, 300)

    def test_poll_errors_are_ignored(self) -> None:
        def poll() -> tuple[int, int]:
            raise FileNotFoundError("renamed mid-walk")

        task = make_task(FakeClock(), items=5, poll=poll)
        task.sample()
        assert task.items_done == 0


class TestDrawing:
    def test_draws_on_a_terminal_and_clears_after(self) -> None:
        stream = TtyStream()
        with Progress(stream, refresh=0.01).task("flush", items=1) as task:
            task.advance()
        out = stream.getvalue()

        assert "flush" in out
        assert out.endswith(CLEAR_LINE)

    def test_silent_when_not_a_terminal(self) -> None:
        """The launcher captures output into a log; it must stay plain text."""
        stream = io.StringIO()
        with Progress(stream).task("flush", items=1) as task:
            task.advance()
        assert stream.getvalue() == ""

    def test_can_be_switched_off_on_a_terminal(self) -> None:
        stream = TtyStream()
        with Progress(stream, enabled=False).task("flush", items=1):
            pass
        assert stream.getvalue() == ""

    def test_nothing_to_do_draws_nothing(self) -> None:
        stream = TtyStream()
        with Progress(stream, refresh=0.01).task("flush", items=0):
            pass
        assert stream.getvalue() == ""

    def test_clears_the_line_when_interrupted(self) -> None:
        stream = TtyStream()
        with (
            pytest.raises(KeyboardInterrupt),
            Progress(stream, refresh=0.01).task("flush", items=1),
        ):
            raise KeyboardInterrupt
        assert stream.getvalue().endswith(CLEAR_LINE)


class TestLanded:
    def test_partials_count_in_bytes_but_not_files(self, tmp_path: Path) -> None:
        (tmp_path / "2025/07").mkdir(parents=True)
        (tmp_path / "2025/07/a.jpg").write_bytes(b"x" * 10)
        (tmp_path / "2025/07/b.mp4.a1b2c3d4.partial").write_bytes(b"x" * 5)
        assert landed_so_far(tmp_path) == (1, 15)

    def test_missing_folder_is_nothing(self, tmp_path: Path) -> None:
        assert landed_so_far(tmp_path / "nope") == (0, 0)


class TestStagesReport:
    def test_each_stage_reports(self, wired) -> None:
        config, spool, _nas, remote_root = wired
        populate(
            remote_root / CAMERA_ROLL,
            {"2026/06/PXL_20260601_112702768.jpg": 1024, "2026/06/PXL_20260602_091500.mp4": 4096},
        )
        recorder = Recorder()
        with Ledger(spool.ledger_path) as ledger:
            ingest(config, Rclone(config.rclone), ledger, spool, progress=recorder)
            process(config, ledger, spool, progress=recorder)
            flush(config, ledger, spool, progress=recorder)

        labels = [task.label for task in recorder.tasks]
        assert labels == ["listing phone-1", "ingest phone-1", "process", "flush"]

        ingest_task, process_task, flush_task = recorder.tasks[1:]
        assert (ingest_task.items_total, ingest_task.bytes_total) == (2, 5120)
        assert (process_task.items_done, process_task.items_total) == (2, 2)
        assert (flush_task.items_done, flush_task.bytes_done) == (2, flush_task.bytes_total)
