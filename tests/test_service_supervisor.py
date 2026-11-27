"""Behavior of the standalone child-process supervisor."""

import json
import subprocess
import sys
import threading
import time

import pytest

from pennybot.services.supervisor import ProcessSupervisor


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("supervisor did not reach expected state")


def status(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def run_in_thread(supervisor):
    thread = threading.Thread(target=supervisor.run, daemon=True)
    thread.start()
    return thread


def test_starts_named_children_and_publishes_independent_status(tmp_path):
    command = [sys.executable, "-c", "import time; time.sleep(30)"]
    supervisor = ProcessSupervisor(
        {"market-feed": command, "notifier": command}, tmp_path, poll_interval=0.01
    )
    thread = run_in_thread(supervisor)
    try:
        wait_for(lambda: status(tmp_path / "market-feed.json").get("state") == "running")
        wait_for(lambda: status(tmp_path / "notifier.json").get("state") == "running")
        market = status(tmp_path / "market-feed.json")
        notifier = status(tmp_path / "notifier.json")
        assert market["name"] == "market-feed"
        assert market["pid"] != notifier["pid"]
        assert market["restart_count"] == 0
    finally:
        supervisor.stop()
        thread.join(3)
    assert not thread.is_alive()
    assert status(tmp_path / "market-feed.json")["state"] == "stopped"
    assert status(tmp_path / "notifier.json")["state"] == "stopped"


def test_restarts_failed_child_after_bounded_backoff(tmp_path):
    command = [sys.executable, "-c", "import sys; sys.exit(7)"]
    supervisor = ProcessSupervisor(
        {"worker": command}, tmp_path, poll_interval=0.01,
        min_backoff=0.08, max_backoff=0.12,
    )
    thread = run_in_thread(supervisor)
    try:
        wait_for(lambda: status(tmp_path / "worker.json").get("restart_count", 0) >= 2)
        report = status(tmp_path / "worker.json")
        assert report["last_exit_code"] == 7
        assert report["restart_count"] >= 2
        assert supervisor.backoff_delay(1) == 0.08
        assert supervisor.backoff_delay(100) == 0.12
    finally:
        supervisor.stop()
        thread.join(3)
    assert not thread.is_alive()


def test_stop_does_not_touch_unrelated_process(tmp_path):
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    supervisor = ProcessSupervisor(
        {"owned": [sys.executable, "-c", "import time; time.sleep(30)"]},
        tmp_path, poll_interval=0.01,
    )
    thread = run_in_thread(supervisor)
    try:
        wait_for(lambda: status(tmp_path / "owned.json").get("state") == "running")
        supervisor.stop()
        thread.join(3)
        assert not thread.is_alive()
        assert unrelated.poll() is None
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=3)


def test_rejects_unsafe_names_and_invalid_commands(tmp_path):
    with pytest.raises(ValueError):
        ProcessSupervisor({"../outside": [sys.executable]}, tmp_path)
    with pytest.raises(ValueError):
        ProcessSupervisor({"worker": "python -c pass"}, tmp_path)


def test_windows_children_use_hidden_window_flag(tmp_path, monkeypatch):
    from pennybot.services import supervisor as module

    flags = []

    class Child:
        pid = 12345

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    def fake_popen(command, **kwargs):
        flags.append(kwargs["creationflags"])
        return Child()

    monkeypatch.setattr(module, "_is_windows", lambda: True)
    monkeypatch.setattr(module.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)
    supervisor = ProcessSupervisor({"worker": ["ignored"]}, tmp_path, poll_interval=0.01)
    thread = run_in_thread(supervisor)
    try:
        wait_for(lambda: bool(flags))
    finally:
        supervisor.stop()
        thread.join(3)
    assert flags == [0x08000000]


def test_stop_handles_child_exiting_during_termination(tmp_path, monkeypatch):
    from pennybot.services import supervisor as module

    class Child:
        pid = 12345

        def poll(self):
            return None

        def terminate(self):
            raise ProcessLookupError("already exited")

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: Child())
    supervisor = ProcessSupervisor({"worker": ["ignored"]}, tmp_path, poll_interval=0.01)
    thread = run_in_thread(supervisor)
    wait_for(lambda: status(tmp_path / "worker.json").get("state") == "running")
    supervisor.stop()
    thread.join(3)
    assert not thread.is_alive()
    assert status(tmp_path / "worker.json")["state"] == "stopped"


def test_failed_spawn_retries_and_reports_error(tmp_path, monkeypatch):
    from pennybot.services import supervisor as module

    calls = []

    class Child:
        pid = 12345

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    def fake_popen(*args, **kwargs):
        calls.append(time.monotonic())
        if len(calls) == 1:
            raise OSError("temporary launch failure")
        return Child()

    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)
    supervisor = ProcessSupervisor(
        {"worker": ["ignored"]}, tmp_path, poll_interval=0.01,
        min_backoff=0.05, max_backoff=0.05,
    )
    thread = run_in_thread(supervisor)
    try:
        wait_for(lambda: status(tmp_path / "worker.json").get("state") == "backoff")
        assert "temporary launch failure" in status(tmp_path / "worker.json")["last_error"]
        wait_for(lambda: status(tmp_path / "worker.json").get("state") == "running")
        assert status(tmp_path / "worker.json")["restart_count"] == 1
        assert calls[1] - calls[0] >= 0.05
    finally:
        supervisor.stop()
        thread.join(3)
