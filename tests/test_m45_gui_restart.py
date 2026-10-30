"""M45: GUI sa po zmene kódu alebo konfigurácie spustí nanovo samo; stránky radar/gui obnovuje Streamlit a reštart nepotrebujú."""

from pathlib import Path

import radar
from radar.commands import gui_sources, serve_gui
from radar.config import Config


class Proc:
    def __init__(self, exit_after=None):
        self.polls, self.exit_after, self.terminated = 0, exit_after, False

    def poll(self):
        self.polls += 1
        if self.terminated:
            return 1
        return 0 if self.exit_after is not None and self.polls > self.exit_after else None

    def terminate(self):
        self.terminated = True

    def wait(self):
        return 1


def run(snapshots, last_proc_exit_after=2, port_free=lambda port: True):
    """Sled stavov zdrojov; druhý proces po pár kolách skončí sám (ukončenie GUI človekom)."""
    procs = []

    def start():
        procs.append(Proc(exit_after=None if not procs else last_proc_exit_after))
        return procs[-1]

    states = iter(snapshots)
    last = {}

    def sources():
        nonlocal last
        last = next(states, last)
        return last

    code = serve_gui(start, sources, 8501, sleep=lambda s: None, port_free=port_free)
    return code, procs


def test_change_restarts_once_after_writes_settle_and_exit_code_is_returned():
    a, b, c = {"x.py": 1.0}, {"x.py": 2.0}, {"x.py": 2.0, "y.py": 3.0}
    code, procs = run([a, a, b, c, c])  # dva zápisy za sebou = jeden reštart až po ich konci
    assert len(procs) == 2 and procs[0].terminated and not procs[1].terminated
    assert code == 0


def test_no_change_means_no_restart():
    procs = [Proc(exit_after=3)]
    code = serve_gui(lambda: procs[0], lambda: {"x.py": 1.0}, 8501, sleep=lambda s: None)
    assert code == 0 and not procs[0].terminated


def test_new_process_waits_until_the_port_is_free():
    answers = iter([False, False, True])
    waits = []

    def port_free(port):
        waits.append(port)
        return next(answers)

    a, b = {"x.py": 1.0}, {"x.py": 2.0}
    _, procs = run([a, b, b], port_free=port_free)
    assert len(procs) == 2 and waits == [8501, 8501, 8501]


def test_ctrl_c_stops_the_gui_process():
    proc = Proc()

    def sleep(_):
        raise KeyboardInterrupt

    assert serve_gui(lambda: proc, lambda: {"x.py": 1.0}, 8501, sleep=sleep) == 0
    assert proc.terminated


def test_sources_cover_package_and_config_but_not_gui_pages(tmp_path):
    (tmp_path / "config.local.yaml").write_text("gui: {}\n", encoding="utf-8")
    files = {Path(p) for p in gui_sources(Config(config_dir=str(tmp_path)))}
    base = Path(radar.__file__).resolve().parent
    assert base / "outcomes.py" in files and base / "db" / "mail.py" in files
    assert tmp_path / "config.local.yaml" in files
    assert not any(base / "gui" in p.parents for p in files)
    assert Config().gui.auto_restart is True
