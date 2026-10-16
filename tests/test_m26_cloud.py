"""M26 – Azure OpenAI vedľa lokálnych modelov (audit 15. 10. 2026, balík D, scenáre 6–10): nasadenia rozlíšené
v GUI a v behu, výber prežije reštart, čítanie nasadení bez oprávnenia, oddelené poverenia a parametre, žiadne
tajomstvá v DB a logoch, triedenie chýb bez falošného JSON fallbacku, obmedzené opakovanie, rozpočet s rezerváciou
a bez drahšieho náhradného modelu. Offline: falošný OpenAI klient a httpx.MockTransport."""

import json
import logging
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from openai import APITimeoutError, BadRequestError, RateLimitError

from radar.cloud import (
    CloudLLM, DeploymentListError, add_manual_deployment, deployment_label, deployments, make_cloud_client,
    record_capability, refresh_deployments, save_cloud_settings, save_profile, set_route, spend_summary,
    test_connection as connection_test,
)
from radar.config import load_config
from radar.db import DB
from radar.models import CLOUD_PAUSE_KEY, ModelRouter
from radar.value import ReviewOut, ensure_brief
from tests.fakes import BoundFake
from tests.test_m18_contacts import setup

ROOT = Path(__file__).resolve().parents[1]
SECRET = "sk-test-TAJNE-123"
ENDPOINT = "https://moj-resource.openai.azure.com/openai/v1/"
RATE = {"input_per_1m": 0.4, "output_per_1m": 1.6, "currency": "EUR", "verified_at": "2026-10-15"}
MSG = [{"role": "user", "content": "Posúď návrh prínosu."}]
OK = json.dumps({"verdict": "ok", "problems": []})
REQ = httpx.Request("POST", ENDPOINT + "chat/completions")


def err(cls, status: int, message: str):
    return cls(message, response=httpx.Response(status, request=REQ), body=None)


def response(content: str, prompt: int = 120, completion: int = 30, reasoning: int | None = None):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=None), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion,
                              completion_tokens_details=SimpleNamespace(reasoning_tokens=reasoning)))


class FakeCompletions:
    """Skript odpovedí: reťazec = obsah odpovede, výnimka = chyba; posledná položka sa opakuje."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []

    def create(self, **params):
        self.calls.append(params)
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        return response(item)


def fake_openai(comp: FakeCompletions, created: list):
    class FakeOpenAI:
        def __init__(self, **kwargs):
            created.append(kwargs)
            self.chat = SimpleNamespace(completions=comp)

    return FakeOpenAI


def arm_dep(name: str, model: str = "gpt-4.1-mini", version: str = "2025-04-14") -> dict:
    return {"name": name, "sku": {"name": "GlobalStandard"},
            "properties": {"model": {"format": "OpenAI", "name": model, "version": version},
                           "provisioningState": "Succeeded"}}


def cloud_env(tmp_path, monkeypatch, script, total=85.87, rates=None, deployment="mini-b", reasoning=False, key=True):
    """Generátor lokálne (falošný Recorder), hodnotiteľ (rola reviewer) v Azure s falošným klientom."""
    if key:
        monkeypatch.setenv("AZ_KEY_TEST", SECRET)
    cfg, db, hid, rec, _ = setup(tmp_path)
    save_profile(db, "azure", {"endpoint": ENDPOINT, "api_key_env": "AZ_KEY_TEST"})
    add_manual_deployment(db, "azure", deployment, "gpt-4.1-mini", "2025-04-14", reasoning)
    set_route(db, "reviewer", "azure", deployment, reasoning)
    save_cloud_settings(db, budget_total_eur=total, budget_daily_eur=5.0, budget_run_eur=5.0,
                        rates={deployment: RATE} if rates is None else rates)
    comp = FakeCompletions(script)
    sleeps: list[float] = []
    holder: dict = {}

    def factory(role, role_cfg, thinking, temperature):
        if role_cfg.provider == "local":
            return BoundFake(rec, role_cfg.model, thinking, temperature)
        r = holder["router"]
        return make_cloud_client(r.models, r.cloud, db, role_cfg, temperature, r.budget,
                                 client=SimpleNamespace(chat=SimpleNamespace(completions=comp)), sleep=sleeps.append)

    router = ModelRouter(cfg, db, client_factory=factory)
    holder["router"] = router
    return SimpleNamespace(cfg=cfg, db=db, hid=hid, rec=rec, router=router, comp=comp, sleeps=sleeps)


# ------------------------------------------------------------------ scenár 6
def test_two_deployments_of_same_model_are_distinct_selection_survives_restart_and_runtime_sends_deployment(
        tmp_path, monkeypatch):
    monkeypatch.setenv("AZ_KEY_TEST", SECRET)
    monkeypatch.setenv("AZ_MGMT_TEST", "mgmt-token")
    cfg, db, hid, rec, _ = setup(tmp_path)
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers.get("authorization")))
        return httpx.Response(200, json={"value": [arm_dep("mini-a"), arm_dep("mini-b")]})

    profile = save_profile(db, "azure", {"endpoint": ENDPOINT, "api_key_env": "AZ_KEY_TEST", "subscription_id": "sub",
                                         "resource_group": "rg", "account_name": "acc", "mgmt_token_env": "AZ_MGMT_TEST"})
    found = refresh_deployments(db, "azure", profile, httpx.Client(transport=httpx.MockTransport(handler)))
    labels = [deployment_label(d) for d in found]
    assert len(set(labels)) == 2 and all(lab.startswith("gpt-4.1-mini 2025-04-14 · nasadenie ") for lab in labels)
    assert seen == [("GET", "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/acc"
                     "/deployments", "Bearer mgmt-token")]  # iba čítanie
    set_route(db, "reviewer", "azure", "mini-b")
    save_cloud_settings(db, budget_total_eur=85.87, rates={"mini-b": RATE})
    db.close()

    db2 = DB(cfg.db_path)  # reštart aplikácie
    comp, created = FakeCompletions([OK]), []
    monkeypatch.setattr("radar.cloud.OpenAI", fake_openai(comp, created))
    router = ModelRouter(cfg, db2)
    assert (router.role_config("reviewer").provider, router.role_config("reviewer").deployment) == ("azure", "mini-b")
    result = router.run_task("value_check", MSG, ReviewOut)
    assert result.value.verdict == "ok"
    assert comp.calls[0]["model"] == "mini-b"  # do Azure ide názov nasadenia
    assert created[0]["base_url"] == ENDPOINT and created[0]["api_key"] == SECRET
    usage = db2.cloud_usage()
    assert usage[0]["status"] == "settled" and usage[0]["cost_eur"] == pytest.approx((120 * 0.4 + 30 * 1.6) / 1e6)
    assert usage[0]["reserved_eur"] >= usage[0]["cost_eur"]


def test_gui_lists_deployments_distinctly_and_keeps_role_selection(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    from tests.test_m2_gui import APP, fixture_db

    cfg_path = fixture_db(tmp_path)
    cfg_path.write_text(cfg_path.read_text(encoding="utf-8") + "models:\n  profile: balanced\n", encoding="utf-8")
    monkeypatch.setenv("RADAR_CONFIG", str(cfg_path))
    monkeypatch.setattr("radar.jobs.gpu_status", lambda cfg, get=None: [])
    monkeypatch.setattr("radar.jobs.ollama_models", lambda cfg, get=None: [])
    cfg = load_config(cfg_path)
    db = DB(cfg.db_path)
    save_profile(db, "azure", {"endpoint": ENDPOINT, "api_key_env": "AZ_KEY_NEEXISTUJE"})
    add_manual_deployment(db, "azure", "mini-a", "gpt-4.1-mini", "2025-04-14")
    add_manual_deployment(db, "azure", "mini-b", "gpt-4.1-mini", "2025-04-14")
    set_route(db, "reviewer", "azure", "mini-b")
    save_cloud_settings(db, budget_total_eur=85.87, rates={"mini-b": RATE})
    db.close()
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.switch_page("app_pages/actions.py").run()
    assert not at.exception, at.exception
    deps = next(df.value for df in at.dataframe if "model a verzia" in df.value.columns)
    assert list(deps["model a verzia"]) == ["gpt-4.1-mini 2025-04-14 · nasadenie mini-a",
                                            "gpt-4.1-mini 2025-04-14 · nasadenie mini-b"]
    assert at.selectbox(key="cloud_route_reviewer").value == ("azure", "mini-b", False)
    profiles = next(df.value for df in at.dataframe if "kľúč nastavený" in df.value.columns)
    assert list(profiles["kľúč nastavený"]) == [False]  # iba príznak, hodnota kľúča sa nezobrazuje
    assert any("nie overený zostatok" in c.value for c in at.caption)
    assert any("Azure for Students" in c.value for c in at.caption)


# ------------------------------------------------------------------ scenár 7
def test_listing_without_permission_is_understandable_manual_add_works_and_nothing_is_created(tmp_path, monkeypatch):
    db = DB(str(tmp_path / "r.sqlite"))
    seen = []

    def forbidden(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        return httpx.Response(403, json={"error": {"code": "AuthorizationFailed"}})

    http = httpx.Client(transport=httpx.MockTransport(forbidden))
    bare = save_profile(db, "bez-resource", {"endpoint": ENDPOINT})
    with pytest.raises(DeploymentListError, match="ručne"):
        refresh_deployments(db, "bez-resource", bare, http)
    profile = save_profile(db, "azure", {"endpoint": ENDPOINT, "subscription_id": "s", "resource_group": "rg",
                                         "account_name": "a", "mgmt_token_env": "AZ_MGMT_TEST"})
    monkeypatch.delenv("AZ_MGMT_TEST", raising=False)
    with pytest.raises(DeploymentListError, match="token"):
        refresh_deployments(db, "azure", profile, http)
    monkeypatch.setenv("AZ_MGMT_TEST", "t")
    with pytest.raises(DeploymentListError) as exc:
        refresh_deployments(db, "azure", profile, http)
    assert "oprávnenia" in str(exc.value) and "ručne" in str(exc.value)

    item = add_manual_deployment(db, "azure", "moje-nasadenie", "gpt-4o", "2024-11-20")
    assert deployments(db, "azure") == [item] and item["overené"] == {} and item["stav"] == "neoverené"
    set_route(db, "accurate", "azure", "moje-nasadenie")
    assert seen == ["GET"]  # výber nič nevytvára ani nemení (žiadny PUT/POST/DELETE)

    record_capability(db, "azure", "moje-nasadenie", {"ok": False, "schopnosť": "json_schema"})
    assert deployments(db, "azure")[0]["overené"] == {}  # neúspech sa ako overený nezapíše
    record_capability(db, "azure", "moje-nasadenie", {"ok": True, "schopnosť": "text", "kedy": "2026-10-15T10:00:00"})
    assert deployments(db, "azure")[0]["overené"] == {"text": "2026-10-15T10:00:00"}

    ok = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"value": [arm_dep("iné")]})))
    merged = refresh_deployments(db, "azure", profile, ok)
    assert [d["deployment"] for d in merged] == ["iné", "moje-nasadenie"]  # ručné nasadenie ostáva


def test_connection_test_checks_text_and_json_schema_separately(tmp_path, monkeypatch):
    env = cloud_env(tmp_path, monkeypatch, [err(BadRequestError, 400, "response_format json_schema is not supported"),
                                            json.dumps({"ok": True})])
    client = env.router.client("reviewer")
    result = connection_test(client, "json_schema")
    assert result["ok"] is False  # odpoveď prišla až v jednoduchšom režime → JSON schéma nie je overená
    env.comp.script = ["OK"]
    assert connection_test(client, "text")["ok"] is True


# ------------------------------------------------------------------ scenár 8
def test_local_generator_and_cloud_evaluator_use_separate_credentials_and_params_without_leaking_secret(
        tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("AZ_KEY_TEST", SECRET)
    caplog.set_level(logging.DEBUG)
    cfg, db, hid, rec, _ = setup(tmp_path)
    save_profile(db, "azure", {"endpoint": ENDPOINT, "api_key_env": "AZ_KEY_TEST"})
    add_manual_deployment(db, "azure", "o4-eval", "o4-mini", "2025-04-16", reasoning=True)
    set_route(db, "reviewer", "azure", "o4-eval", reasoning=True)
    save_cloud_settings(db, budget_total_eur=85.87, rates={"o4-eval": RATE})
    comp, created = FakeCompletions([OK]), []
    monkeypatch.setattr("radar.cloud.OpenAI", fake_openai(comp, created))
    router = ModelRouter(cfg, db)
    local, cloud = router.client("accurate"), router.client("reviewer")
    assert isinstance(cloud, CloudLLM) and not isinstance(local, CloudLLM)
    assert local.cfg.base_url.startswith("http://localhost") and local.cfg.api_key == "local"
    assert router.run_task("value_check", MSG, ReviewOut).value.verdict == "ok"
    params = comp.calls[0]
    assert params["max_completion_tokens"] > 0 and "temperature" not in params and "max_tokens" not in params
    assert "extra_body" not in params and "reasoning_effort" not in params  # lokálne thinking_on/off nejde do Azure

    for bad in ({"api_key": SECRET}, {"api_key_env": SECRET}, {"mgmt_token_env": "eyJ.token-hodnota"}):
        with pytest.raises(ValueError):
            save_profile(db, "zly", bad)
    dump = "\n".join(sqlite3.connect(cfg.db_path).iterdump())
    assert SECRET not in dump and SECRET not in caplog.text
    assert "AZ_KEY_TEST" in dump  # uložený je iba názov premennej prostredia


def test_old_local_config_works_unchanged():
    cfg = load_config(ROOT / "config.yaml")
    assert all(r.provider == "local" for r in cfg.models.roles.values()) and cfg.models.providers == {}
    router = ModelRouter(cfg, None)
    assert not isinstance(router.client("fast"), CloudLLM) and not router.is_cloud("accurate")
    assert cfg.cloud.budget_total_eur == 0  # bez nastaveného rozpočtu sa do cloudu nevolá


# ------------------------------------------------------------------ scenár 9
def test_unsupported_parameter_does_not_downgrade_json_and_item_waits(tmp_path, monkeypatch):
    env = cloud_env(tmp_path, monkeypatch, [err(BadRequestError, 400, "Unsupported parameter: 'temperature' is not "
                                                                      "supported with this model.")])
    result = env.router.run_task("value_check", MSG, ReviewOut, object_type="hypothesis", object_id=env.hid, review=True)
    assert result.undecided and result.reason == "cloud"
    assert len(env.comp.calls) == 1 and env.router.client("reviewer").schema_mode == "json_schema"
    assert env.db.open_reviews() == []  # čaká na ďalší beh, nezahltí kontrolu
    assert env.db.get_setting(CLOUD_PAUSE_KEY)["dôvod"] == "nepodporovaný parameter"
    assert [u["status"] for u in env.db.cloud_usage()] == ["failed"]


def test_confirmed_format_unsupport_falls_back_to_simpler_json_mode(tmp_path, monkeypatch):
    env = cloud_env(tmp_path, monkeypatch, [err(BadRequestError, 400, "Invalid parameter: 'response_format' of type "
                                                                      "'json_schema' is not supported with this model."), OK])
    assert env.router.run_task("value_check", MSG, ReviewOut).value.verdict == "ok"
    assert [c["response_format"]["type"] for c in env.comp.calls] == ["json_schema", "json_object"]


def test_rate_limit_retries_are_bounded_and_quota_stops_without_fallback(tmp_path, monkeypatch):
    env = cloud_env(tmp_path, monkeypatch, [err(RateLimitError, 429, "Rate limit is exceeded. Try again in 2 seconds.")])
    result = env.router.run_task("value_check", MSG, ReviewOut)
    assert result.undecided and result.reason == "cloud"
    assert len(env.comp.calls) == 3 and env.sleeps == [2.0, 4.0]  # cloud.max_retries = 2
    assert [u["status"] for u in env.db.cloud_usage()] == ["failed"] * 3  # každý pokus rezervovaný, potom uvoľnený
    assert env.rec.models_for("ReviewOut") == []  # žiadny náhradný model

    env2 = cloud_env(tmp_path / "q", monkeypatch, [err(RateLimitError, 429, "insufficient_quota: You exceeded your "
                                                                              "current quota.")])
    assert env2.router.run_task("value_check", MSG, ReviewOut).reason == "cloud"
    assert len(env2.comp.calls) == 1 and env2.sleeps == []
    assert env2.db.get_setting(CLOUD_PAUSE_KEY)["dôvod"] == "kvóta"


def test_exhausted_budget_or_missing_rate_never_calls_and_cloud_reviewer_failure_leaves_brief_pending(
        tmp_path, monkeypatch):
    from tests.test_m24_value import brief_response, claim

    env = cloud_env(tmp_path, monkeypatch, [OK], total=0.001)
    assert env.router.run_task("value_check", MSG, ReviewOut).reason == "cloud"
    assert env.comp.calls == [] and env.db.get_setting(CLOUD_PAUSE_KEY)["dôvod"] == "rozpočet"
    c1 = claim(env.db, env.hid, "Dodávky meškajú.", "Dodávky dielcov meškajú o týždne.")
    c2 = claim(env.db, env.hid, "Plánovanie trpí.", "Plánovači ručne urgujú dodávateľov každý deň.")
    env.rec.responses["DomainBriefOut"] = brief_response(c1, c2)
    env.rec.responses["ReviewOut"] = {"verdict": "ok", "problems": []}  # lokálny model by schválil – nesmie sa použiť
    row = env.db.get_brief(ensure_brief(env.cfg, env.db, env.router, env.hid))
    assert row["status"] == "pending_review" and env.comp.calls == [] and env.rec.models_for("ReviewOut") == []

    env2 = cloud_env(tmp_path / "r", monkeypatch, [OK], rates={})
    assert env2.router.run_task("value_check", MSG, ReviewOut).reason == "cloud" and env2.comp.calls == []
    assert "sadzba" in env2.db.get_setting(CLOUD_PAUSE_KEY)["správa"]

    env3 = cloud_env(tmp_path / "k", monkeypatch, [OK], key=False)
    monkeypatch.delenv("AZ_KEY_TEST", raising=False)
    env3.router._clients.clear()
    assert env3.router.run_task("value_check", MSG, ReviewOut).reason == "cloud" and env3.comp.calls == []


def test_personal_data_tasks_do_not_leave_the_computer_unless_allowed(tmp_path, monkeypatch):
    env = cloud_env(tmp_path, monkeypatch, [json.dumps({"verdict": "ok", "problems": []})])
    result = env.router.run_task("draft_check", MSG, ReviewOut, object_type="outreach", object_id=1, review=True)
    assert result.undecided and result.reason == "cloud_not_allowed" and env.comp.calls == []
    assert env.db.open_reviews() == [] and env.db.cloud_usage() == []
    env.cfg.privacy.cloud_personal_data = True
    router = ModelRouter(env.cfg, env.db, client_factory=env.router.client_factory)
    assert router.run_task("draft_check", MSG, ReviewOut).value.verdict == "ok" and len(env.comp.calls) == 1


def test_cache_reuses_answer_only_for_same_deployment_and_prompt(tmp_path, monkeypatch):
    env = cloud_env(tmp_path, monkeypatch, [OK])
    env.router.run_task("value_check", MSG, ReviewOut)
    assert env.router.run_task("value_check", MSG, ReviewOut).value.verdict == "ok"
    assert len(env.comp.calls) == 1 and [u["status"] for u in env.db.cloud_usage()] == ["cached", "settled"]
    env.router.run_task("value_check", MSG + [{"role": "user", "content": "iný vstup"}], ReviewOut)
    assert len(env.comp.calls) == 2
    add_manual_deployment(env.db, "azure", "mini-c", "gpt-4.1-mini", "2025-04-14")
    set_route(env.db, "reviewer", "azure", "mini-c")
    save_cloud_settings(env.db, rates={"mini-b": RATE, "mini-c": RATE})
    router = ModelRouter(env.cfg, env.db, client_factory=env.router.client_factory)
    env.router.models, env.router.cloud, env.router.budget = router.models, router.cloud, router.budget
    router.run_task("value_check", MSG, ReviewOut)
    assert env.comp.calls[-1]["model"] == "mini-c" and len(env.comp.calls) == 3  # iné nasadenie = nová odpoveď


# ------------------------------------------------------------------ scenár 10
def test_reservations_cover_concurrent_calls(tmp_path):
    path = str(tmp_path / "r.sqlite")
    DB(path).close()
    limits = {"celkom": ("0000", 0.02)}
    results: list = []

    def worker():
        d = DB(path)
        for _ in range(10):
            results.append(d.reserve_cloud("azure", "azure", "mini", "t", 0.003, limits, RATE))
        d.close()

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len([r for r in results if r is not None]) == 6  # 6 × 0,003 ≤ 0,02 < 7 × 0,003


def test_timeout_keeps_reservation_and_limit_blocks_next_call_spend_is_own_estimate(tmp_path, monkeypatch):
    env = cloud_env(tmp_path, monkeypatch, [APITimeoutError(request=REQ)])
    assert env.router.run_task("value_check", MSG, ReviewOut).reason == "timeout"
    usage = env.db.cloud_usage()[0]
    assert usage["status"] == "uncertain" and usage["cost_eur"] is None
    reserved = usage["reserved_eur"]
    summary = spend_summary(env.db, env.router.cloud)
    assert summary["celkom"]["open"] == pytest.approx(reserved) and summary["celkom"]["settled"] == 0
    assert "zostáva_v_rozpočte" in summary and "zostatok_azure" not in summary  # vlastný odhad, nie zostatok Azure

    save_cloud_settings(env.db, budget_total_eur=reserved * 1.5)
    env.comp.script = [OK]
    router = ModelRouter(env.cfg, env.db, client_factory=env.router.client_factory)
    env.router.models, env.router.cloud, env.router.budget = router.models, router.cloud, router.budget
    assert router.run_task("value_check", MSG, ReviewOut).reason == "cloud"
    assert len(env.comp.calls) == 1  # po vyčerpaní limitu žiadne nové cloudové volanie


def test_cloud_result_records_deployment_not_local_role_model(tmp_path, monkeypatch):
    """16. 10. 2026: prehľad domény a návrh prínosu z Azure mali uložený model „radar-qwen3.6-27b“ (lokálny model
    roly) – pôvod výstupu bol nesprávny. Výsledok aj záznam z cache nesú názov nasadenia."""
    env = cloud_env(tmp_path, monkeypatch, [OK])
    res = env.router.run_task("value_check", MSG, ReviewOut)
    assert res.model == "mini-b" and env.router.route("value_check").model == "mini-b"
    cached = env.router.run_task("value_check", MSG, ReviewOut)
    assert cached.model == "mini-b"
    rows = env.db.conn.execute("SELECT model FROM model_judgments WHERE task = 'value_check'").fetchall()
    assert {r[0] for r in rows} == {"mini-b"}


def test_reasoning_deployment_gets_headroom_for_reasoning_tokens(tmp_path, monkeypatch):
    """16. 10. 2026: gpt-6-luna (reasoning) orezal posudok prehľadu pri limite 2400 – tokeny uvažovania sa rátajú do
    max_completion_tokens. Rezerva cloud.reasoning_extra_tokens sa pripočíta (aj do rezervácie rozpočtu)."""
    env = cloud_env(tmp_path, monkeypatch, [OK], reasoning=True)
    env.router.run_task("value_check", MSG, ReviewOut, max_tokens=2400)
    params = env.comp.calls[-1]
    assert params["max_completion_tokens"] == 2400 + env.router.cloud.reasoning_extra_tokens
