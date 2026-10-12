"""M2 – GUI: bezpečné spustenie, každá stránka sa načíta, kontrola -> gold label, escapovanie zdroja."""

import tomllib
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from radar.commands import gui_command
from radar.config import Config, load_config
from radar.db import DB

ROOT = Path(__file__).resolve().parent.parent
APP = str(ROOT / "radar" / "gui" / "app.py")
PAGES = ["overview", "actions", "problems", "evidence", "orgs", "people", "outreach", "interviews", "gate0", "gate1", "gate2", "review", "eval", "privacy"]
EVIL = "Úvod. " * 30 + "<script>alert('x')</script> Malé obce nemajú kapacitu na obstarávanie. " + "Záver. " * 30


def fixture_db(tmp_path) -> Path:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "db_path: data/radar.sqlite\nreports_dir: reports\nexports_dir: exports\nprivacy:\n  lia_completed: true\n",
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    db = DB(cfg.db_path)
    doc, _ = db.upsert_document("https://obce.sk/a", "A", "search", "t", "q", "h", EVIL)
    cid = db.insert_claim(doc, 0, "t", "Malé obce nemajú kapacitu.", "malé obce", "pain", "Malé obce nemajú kapacitu na obstarávanie.")
    db.set_claim_verification(cid, "verified", 100, "ok")
    hid = db.create_hypothesis("Kapacita obcí na obstarávanie", "Popis", "malé obce")
    db.assign_claim(hid, cid)
    db.set_people_selection(hid, True)
    org, _ = db.upsert_organization("Obec Fiktívna", "obec fiktivna", "evidence", website="https://obec-fikt.sk/", website_verified=1)
    db.link_org_hypothesis(org, hid, "mentioned_with_problem", cid, "https://obce.sk/a", fit={"dôkaz o probléme": "áno"})
    pid = db.insert_person(org, "Jana Starostová", "jana starostova", "https://obec-fikt.sk/kontakt", "Jana Starostová, starostka", hid,
                           role_title="starostka", gender_hint="female")
    ct = db.add_contact_point(pid, org, "email", "starostka@obec-fikt.sk", "https://obec-fikt.sk/kontakt",
                              "Jana Starostová, starostka, starostka@obec-fikt.sk", "mailto")
    link = db.upsert_link(pid, hid, "org_affected", cid, "https://obce.sk/a")
    db.set_link_priority(link, 0.7, {"role_fit": 0.3, "väzba": 0.5, "kanál": 1.0, "zhoda_org": 1.0})
    db.insert_outreach(pid, hid, "email", "B", "Otázka", "Vážená pani starostka,\ntext\n\n--\npätička", [], {"ok": True, "problems": []},
                       "draft", ct)
    db.enqueue_review("entail", "claim", cid, "truncated", {"votes": ["undecided"]})
    db.close()
    return cfg_path


@pytest.fixture
def gui(tmp_path, monkeypatch):
    monkeypatch.setenv("RADAR_CONFIG", str(fixture_db(tmp_path)))
    # Správca akcií sa pýta Ollamy na načítané modely – v testoch bez siete falošná odpoveď.
    monkeypatch.setattr("radar.jobs.gpu_status", lambda cfg, get=None: [
        {"model": "radar-qwen3.5-9b:latest", "vram_gb": 5.9, "na_gpu": "100%", "do": "21:30"}])
    monkeypatch.setattr("radar.jobs.ollama_models", lambda cfg, get=None: [
        {"model": m, "gb": 6.6, "parametre": "9B", "kvantizácia": "Q4_K_M"}
        for m in ("radar-qwen3.5-9b:latest", "radar-qwen3.6-27b:latest", "qwen3.6:35b-a3b")])
    return tmp_path


def test_gui_listens_only_on_localhost_without_telemetry():
    cmd = " ".join(gui_command(Config()))
    assert "--server.address 127.0.0.1" in cmd and "--browser.gatherUsageStats false" in cmd
    conf = tomllib.loads((ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8"))
    assert conf["server"]["address"] == "127.0.0.1" and conf["browser"]["gatherUsageStats"] is False


@pytest.mark.parametrize("page", PAGES)
def test_every_page_loads(gui, page):
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.switch_page(f"app_pages/{page}.py").run()
    assert not at.exception, at.exception


def test_review_label_creates_gold_label_and_closes_item(gui):
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.switch_page("app_pages/review.py").run()
    at.segmented_control[1].set_value("supported")
    at.text_input[0].set_value("pozrel som zdroj")
    at.button[-1].click().run()
    assert not at.exception, at.exception
    db = DB(load_config(gui / "config.yaml").db_path)
    assert [(l["task"], l["label"]) for l in db.labels_for("entail")] == [("entail", "supported")]
    assert db.open_reviews() == []
    assert db.get_claim(1)["status"] == "verified"


def test_source_text_with_script_is_escaped(gui):
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.switch_page("app_pages/review.py").run()
    html = " ".join(h.proto.body for h in at.get("html"))
    assert "&lt;script&gt;" in html and "<script>" not in html
    assert "<mark>" in html  # zvýraznenie citátu sa vkladá až po escapovaní


def test_dashboard_has_agentic_buttons_and_problems_show_all(gui):
    """M9: tlačidlá agentických workflowov v prehľade; Problémy predvolene ukazujú všetky hypotézy."""
    db = DB(load_config(gui / "config.yaml").db_path)
    db.create_hypothesis("Slabá zamietnutá", "", "x", status="rejected")
    db.close()
    at = AppTest.from_file(APP, default_timeout=30).run()
    labels = [b.label for b in at.button]
    assert {"Hľadať nové hypotézy", "Otestovať všetky hypotézy", "Zbierať ľudí ku overeným hypotézam"} <= set(labels)
    at.switch_page("app_pages/problems.py").run()
    assert not at.exception, at.exception
    assert any(c.value.startswith("2 z 2 hypotéz") for c in at.caption)  # aj slabá a zamietnutá
    assert "Otestovať teraz" in [b.label for b in at.button]


def test_people_overview_shows_linkedin_and_markers(gui):
    db = DB(load_config(gui / "config.yaml").db_path)
    db.add_contact_point(1, 1, "profile_url", "https://www.linkedin.com/in/jana-starostova", "https://obec-fikt.sk/kontakt",
                         "<linkedin: https://www.linkedin.com/in/jana-starostova>", "page_link", False, {"over_ručne": True})
    db.close()
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.switch_page("app_pages/people.py").run()
    assert not at.exception, at.exception
    table = at.dataframe[0].value
    assert table["LinkedIn"].iloc[0] == "https://www.linkedin.com/in/jana-starostova"
    assert "web overený" in table["značky"].iloc[0] and "LinkedIn" in table["značky"].iloc[0]


def test_overview_numbers_refresh_while_job_runs(gui):
    """Počas bežiacej úlohy sa čísla v prehľade obnovujú (fragment), vrátane hypotéz čakajúcich na overenie."""
    import os

    db = DB(load_config(gui / "config.yaml").db_path)
    job = db.create_job("validate", {"minutes": 60})
    db.update_job(job, pid=os.getpid(), status="running")
    db.set_validation_searched(1, {"agent": {"ok": True}})
    db.close()
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception, at.exception
    assert any("obnovujú každých 20 s" in c.value for c in at.caption)
    tested = [m for m in at.metric if m.label == "Otestované hypotézy"][0]
    assert tested.value == "0 / 1" and "1 čaká na overenie" in tested.delta


def test_action_manager_lists_all_actions_queue_and_switch(gui):
    """Správca akcií: všetky akcie na jednom mieste; počas behu „do poradia“ a „zastaviť a spustiť“, poradie so zrušením."""
    import os

    from radar.workflows import ACTIONS

    at = AppTest.from_file(APP, default_timeout=30).run()
    at.switch_page("app_pages/actions.py").run()
    assert not at.exception, at.exception
    labels = [b.label for b in at.button]
    assert all(a.label in labels for a in ACTIONS.values())
    assert any(m.label == "Model v GPU" and "radar-qwen3.5-9b" in m.value for m in at.metric)

    db = DB(load_config(gui / "config.yaml").db_path)
    job = db.create_job("validate", {"minutes": 60})
    db.update_job(job, pid=os.getpid(), status="running")
    db.create_job("scout", {"minutes": 90}, status="queued")
    db.close()
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.switch_page("app_pages/actions.py").run()
    assert not at.exception, at.exception
    labels = [b.label for b in at.button]
    assert "Hľadať nové hypotézy – do poradia" in labels and "Zastaviť bežiacu a spustiť" in labels
    assert "Zastaviť všetko vrátane poradia" in labels and "Zrušiť" in labels
    at.button(key=[b.key for b in at.button if b.label == "Zrušiť"][0]).click().run()
    db = DB(load_config(gui / "config.yaml").db_path)
    assert db.queued_jobs() == [] and db.get_job(job)["status"] == "running"
    db.close()


def test_model_selection_in_action_manager_saves_override(gui):
    """Výber modelov v Správcovi akcií: uloží sa do nastavení a platí pre ďalšie spustené akcie."""
    from radar.models import MODEL_SETTINGS_KEY

    cfg_path = gui / "config.yaml"
    cfg_path.write_text(cfg_path.read_text(encoding="utf-8") + "models:\n  profile: balanced\n", encoding="utf-8")
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.switch_page("app_pages/actions.py").run()
    assert not at.exception, at.exception
    at.selectbox(key="model_agent").set_value("qwen3.6:35b-a3b")
    [b for b in at.button if b.label == "Uložiť výber modelov"][0].click().run()
    assert not at.exception, at.exception
    db = DB(load_config(cfg_path).db_path)
    assert db.get_setting(MODEL_SETTINGS_KEY) == {"roles": {"agent": "qwen3.6:35b-a3b"}, "profile": "balanced"}
    db.close()


def test_gate1_page_shows_readiness_fields_and_info_request(gui):
    from radar import gate1
    from radar.util import Deadline

    cfg = load_config(gui / "config.yaml")
    db = DB(cfg.db_path)
    fid = db.upsert_field(1, "Výdavky obce", "€ za rok", True, "jadro")
    db.update_field(fid, status="missing")
    db.start_gate1(1)
    gate1.run_finish(cfg, db, Deadline(1), [1])
    db.close()
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.switch_page("app_pages/gate1.py").run()
    assert not at.exception, at.exception
    assert any(m.label == "Kľúčové polia nájdené" and m.value == "0 / 1" for m in at.metric)
    assert any("dáta nenájdené" in w.value for w in at.warning)
    assert any("211/2000" in c.value for c in at.code)
