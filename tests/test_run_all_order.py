"""run-all: poradie fáz podľa modelov, preskočenie krokov s ľuďmi bez LIA, rozpočet dokumentov."""

from types import SimpleNamespace

from radar.config import Config, PipelineConfig, PrivacyConfig
from radar.pipeline import ACCURATE_STAGES, FAST_STAGES, run_all


class FakeApp(SimpleNamespace):
    pass


def make(tmp_path, lia):
    from radar.db import DB
    from radar.models import ModelRouter

    cfg = Config(db_path=str(tmp_path / "r.sqlite"), privacy=PrivacyConfig(lia_completed=lia),
                 pipeline=PipelineConfig(max_new_docs_per_run=10))
    db = DB(cfg.db_path)
    return FakeApp(cfg=cfg, db=db, router=ModelRouter(cfg, db, legacy_client=object()), collect_budget=None)


def test_run_all_order_and_lia_gate(tmp_path):
    app = make(tmp_path, lia=False)
    calls = []
    failed = run_all(app, 5, lambda a, stage, dl: calls.append(stage) or {})
    assert not failed
    people = {"orgs", "people", "people-rank", "drafts"}
    expected = [s for s in FAST_STAGES if s not in people] + ["embed"] + [s for s in ACCURATE_STAGES if s not in people] + ["report", "purge"]
    assert calls == expected
    assert app.collect_budget == 7  # 30 % z 10 dokumentov ide na objavovanie


def test_run_all_with_lia_runs_people_before_verify_and_continues_after_failure(tmp_path):
    app = make(tmp_path, lia=True)
    calls = []

    def stage(a, name, dl):
        calls.append(name)
        if name == "people":
            raise RuntimeError("web nedostupný")
        return {}

    assert run_all(app, 5, stage) is True  # zlyhanie jedného kroku nezastaví ostatné
    assert calls.index("orgs") < calls.index("people") < calls.index("embed") < calls.index("people-rank")
    assert calls.index("people-rank") < calls.index("drafts") < calls.index("verify") < calls.index("cluster")
    assert calls[-2:] == ["report", "purge"]
