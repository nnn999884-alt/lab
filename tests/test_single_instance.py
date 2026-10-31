"""중복 실행 처리: 새 인스턴스가 살아남고 옛 인스턴스(그 cmd 창)만 내려가야 한다.

특히 `taskkill /T` 는 부모 cmd 의 자식 전체를 죽이므로, 내 계보(새 창)를 건드리면
새 프로세스가 자기 자신을 죽인다. 여기서 그 경계를 고정한다.
"""
import os

import pytest

from pennybot import __main__ as cli


class Cfg:
    def __init__(self, state_dir):
        self.state_dir = str(state_dir)


def test_takeover_stops_old_instance_and_takes_the_lock(tmp_path, monkeypatch):
    (tmp_path / "trade.pid").write_text("4242", encoding="utf-8")
    monkeypatch.setattr(cli, "_pid_alive", lambda pid: True)
    killed = []
    monkeypatch.setattr(cli, "_stop_other_instance",
                        lambda pid, cmd: killed.append((pid, cmd)) or True)

    cli._single_instance(Cfg(tmp_path), "trade")

    assert killed == [(4242, "trade")]
    assert (tmp_path / "trade.pid").read_text(encoding="utf-8") == str(os.getpid())


def test_does_not_start_a_second_copy_when_old_will_not_die(tmp_path, monkeypatch):
    (tmp_path / "trade.pid").write_text("4242", encoding="utf-8")
    monkeypatch.setattr(cli, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(cli, "_stop_other_instance", lambda pid, cmd: False)

    with pytest.raises(SystemExit) as e:
        cli._single_instance(Cfg(tmp_path), "trade")

    assert e.value.code == 3
    assert (tmp_path / "trade.pid").read_text(encoding="utf-8") == "4242"


def test_stale_pid_starts_normally(tmp_path, monkeypatch):
    (tmp_path / "trade.pid").write_text("4242", encoding="utf-8")
    monkeypatch.setattr(cli, "_pid_alive", lambda pid: False)

    cli._single_instance(Cfg(tmp_path), "trade")

    assert (tmp_path / "trade.pid").read_text(encoding="utf-8") == str(os.getpid())


def _tree():
    """옛 창(100) 트리와 새 창(200) 트리. 102 가 옛 인스턴스, 201 이 나."""
    return {
        100: {"ppid": 900, "name": "cmd.exe", "cmd": ".venv\\Scripts\\python -m pennybot trade"},
        101: {"ppid": 100, "name": "python.exe", "cmd": ".venv\\Scripts\\python -m pennybot trade"},
        102: {"ppid": 101, "name": "python.exe", "cmd": ".venv\\Scripts\\python -m pennybot trade"},
        200: {"ppid": 901, "name": "cmd.exe", "cmd": ".venv\\Scripts\\python -m pennybot trade"},
        201: {"ppid": 200, "name": "python.exe", "cmd": ".venv\\Scripts\\python -m pennybot trade"},
        900: {"ppid": 5, "name": "explorer.exe", "cmd": "C:\\Windows\\explorer.exe"},
        901: {"ppid": 5, "name": "explorer.exe", "cmd": "C:\\Windows\\explorer.exe"},
    }


def test_kill_target_is_the_old_window_only():
    mine = {200, 201}
    assert cli._old_tree_root(102, "trade", _tree(), mine) == 100


def test_never_targets_my_own_lineage():
    # 내가 옛 인스턴스의 자식(= 같은 창)이면 아무것도 죽이지 않는다
    assert cli._old_tree_root(101, "trade", _tree(), {100, 101}) == 0


def test_pid_reused_by_another_command_is_left_alone():
    assert cli._old_tree_root(102, "dashboard", _tree(), {200, 201}) == 0


def test_directly_launched_process_targets_itself_only():
    table = {300: {"ppid": 5, "name": "python.exe", "cmd": "python -m pennybot trade"}}
    assert cli._old_tree_root(300, "trade", table, set()) == 300


def test_unknown_process_table_still_targets_the_pid():
    # PowerShell 표를 못 얻으면 옛 프로세스만이라도 죽인다 (자기 자신은 제외)
    assert cli._old_tree_root(102, "trade", {}, set()) == 102
