"""End-to-end test celého pipeline so simulovaným modelom, vyhľadávaním aj webom.

Overuje logiku (overenie citátov, zaradenie do hypotéz, výpočet trhu, správu)
bez llama-servera a bez internetu.
"""

import json
from types import SimpleNamespace

from radar.agent import run_deep_dive
from radar.cli import App, run_stage
from radar.config import Config, EmbeddingsConfig, PipelineConfig, Project, Topic
from radar.fetch import FetchedPage
from radar.search import SearchResult
from radar.util import Deadline
from radar.verify import run_verify

DOC_TEXT = (
    "Úvod k analýze samospráv. " * 40
    + "\nMalé obce nemajú kapacitu na verejné obstarávanie, a preto im projekty meškajú.\n"
    + "Na Slovensku je 2 890 obcí.\n"
    + "Záverečné poznámky k téme. " * 40
)

REAL_QUOTE = "Malé obce nemajú kapacitu na verejné obstarávanie, a preto im projekty meškajú."


class FakeLLM:
    schema_mode = "fake"

    def __init__(self):
        self.tool_script = []

    def chat_json(self, messages, model_cls, retries=2, max_tokens=None):
        name = model_cls.__name__
        if name == "ExtractionResult":
            return model_cls.model_validate(
                {
                    "relevant": True,
                    "claims": [
                        {
                            "problem": "Malé obce nemajú kapacitu na obstarávanie, projekty meškajú.",
                            "affected_party": "malé obce",
                            "claim_type": "pain",
                            "quote": REAL_QUOTE,
                            "numbers": [],
                        },
                        {  # halucinácia – citát v zdroji nie je
                            "problem": "Obce míňajú 40 % rozpočtu na IT.",
                            "affected_party": "obce",
                            "claim_type": "cost",
                            "quote": "Obce míňajú až 40 % svojho rozpočtu na informačné technológie.",
                            "numbers": [{"value": 40, "unit": "%", "meaning": "podiel rozpočtu"}],
                        },
                        {  # citát takmer sedí, ale číslo je zmenené
                            "problem": "Na Slovensku je 2 900 obcí.",
                            "affected_party": "obce",
                            "claim_type": "market_data",
                            "quote": "Na Slovensku je 2 900 obcí.",
                            "numbers": [{"value": 2900, "unit": "obcí", "meaning": "počet obcí"}],
                        },
                    ],
                }
            )
        if name == "Entailment":
            return model_cls(verdict="supported", reason="Citát to priamo uvádza.")
        if name == "Assignment":
            return model_cls(decision="new", title="Kapacita malých obcí na obstarávanie", description="Popis.", segment="malé obce")
        if name == "Critique":
            return model_cls(
                summary="Dôkaz [C1] ukazuje problém s kapacitou.",
                counterarguments=["Problém môžu riešiť centrá zdieľaných služieb."],
                missing_evidence=["Kto by platil?"],
                competitor_queries=["softvér verejné obstarávanie obce"],
                interview_questions=["Kedy ste naposledy obstarávali a kto to robil?"],
            )
        if name == "SizingModel":
            return model_cls.model_validate(
                {
                    "segment_definition": "obce v SR",
                    "result_unit": "€/rok",
                    "factors": [
                        {"name": "Počet obcí", "unit": "obcí", "kind": "data", "search_query": "počet obcí Slovensko"},
                        {"name": "Podiel obcí s problémom", "unit": "%", "kind": "assumption", "low": 20, "high": 40, "rationale": "odhad"},
                        {"name": "Cena služby", "unit": "€/rok", "kind": "assumption", "low": 300, "high": 1200, "rationale": "odhad"},
                    ],
                }
            )
        if name == "Triage":
            return model_cls(selected=[0])
        if name == "NumberExtraction":
            return model_cls.model_validate(
                {"candidates": [{"value": 2890, "unit": "obcí", "quote": "Na Slovensku je 2 890 obcí.", "measures": "počet obcí v SR"}]}
            )
        raise AssertionError(f"Neočakávaná schéma {name}")

    def chat_tools(self, messages, tools):
        return self.tool_script.pop(0)


class FakeSearx:
    def search(self, query, limit=None):
        return [SearchResult(url="https://example.sk/analyza", title="Analýza", snippet="…")]


class FakeFetcher:
    def fetch(self, url):
        return FetchedPage(url=url, final_url=url, title="Analýza", text=DOC_TEXT, content_hash=str(hash(DOC_TEXT)))


def make_app(tmp_path):
    cfg = Config(
        db_path=str(tmp_path / "radar.sqlite"),
        reports_dir=str(tmp_path / "reports"),
        topics=[Topic(name="Samosprávy", queries=["obce obstarávanie problém"])],
        projects=[Project(name="eurofondy-radar", title="Kraje nevidia, kde sa projekty zasekávajú", segment="VÚC")],
        pipeline=PipelineConfig(critique_min_claims=1, market_min_claims=1, market_preferred_sites=[]),
        embeddings=EmbeddingsConfig(enabled=False),  # testy bez Ollamy
    )
    app = App(cfg)
    app._llm, app._searx, app._fetcher = FakeLLM(), FakeSearx(), FakeFetcher()
    return app


def test_full_pipeline(tmp_path):
    app = make_app(tmp_path)
    deadline = Deadline(5)
    stats = {stage: run_stage(app, stage, deadline) for stage in
             ["collect", "extract", "verify", "cluster", "critique", "market", "report"]}

    assert stats["collect"]["new_documents"] == 1
    assert stats["extract"]["claims"] == 3
    assert stats["verify"] == {"verified": 1, "rejected": 2}  # vymyslený citát ani zmenené číslo neprejdú
    rejected_notes = [r["verify_note"] for r in app.db.conn.execute("SELECT verify_note FROM claims WHERE status = 'rejected'")]
    assert any("2 900" in note for note in rejected_notes)
    assert stats["cluster"]["new_hypotheses"] == 1
    assert stats["cluster"]["project_created"] == 1

    hyp = [h for h in app.db.active_hypotheses() if not h["project"]][0]
    evidence = app.db.evidence_for(hyp["id"])
    assert [e["quote"] for e in evidence] == [REAL_QUOTE]

    est = app.db.latest_market_estimate(hyp["id"])
    assert est["complete"] == 1
    assert est["low"] == 2890 * 0.20 * 300
    assert est["high"] == 2890 * 0.40 * 1200

    report = (tmp_path / "reports" / "latest.md").read_text(encoding="utf-8")
    assert "Kapacita malých obcí na obstarávanie" in report
    assert "Otázky na rozhovory" in report
    assert "Obce míňajú až 40 %" not in report  # zamietnuté tvrdenie sa v správe neukáže
    assert report.index("## Rozpracované projekty") < report.index("## Nové hypotézy")
    assert "Miera istoty" in report
    register = (tmp_path / "reports" / "register.csv").read_text(encoding="utf-8-sig")
    assert REAL_QUOTE in register and "Obce míňajú" not in register


def call(name, args, call_id):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=json.dumps(args, ensure_ascii=False)))


def test_deep_dive_agent_records_only_verifiable_evidence(tmp_path):
    app = make_app(tmp_path)
    url = "https://example.sk/analyza"
    app.llm.tool_script = [
        SimpleNamespace(content=None, tool_calls=[call("web_search", {"query": "kapacita obcí obstarávanie"}, "1")]),
        SimpleNamespace(content=None, tool_calls=[call("read_page", {"url": url}, "2")]),
        SimpleNamespace(
            content=None,
            tool_calls=[
                call("record_evidence", {"url": url, "part": 0, "problem": "Malé obce nemajú kapacitu.", "quote": REAL_QUOTE}, "3"),
                call("record_evidence", {"url": url, "part": 0, "problem": "Vymyslené.", "quote": "Toto v texte vôbec nie je napísané."}, "4"),
                call("record_evidence", {"url": url, "part": 0, "problem": "Duplicitný dôkaz.", "quote": REAL_QUOTE}, "4b"),
            ],
        ),
        SimpleNamespace(content=None, tool_calls=[call("finish", {"summary": "Našiel som jeden dôkaz."}, "5")]),
    ]
    summary, claim_ids = run_deep_dive(app.cfg, app.db, app.llm, app.searx, app.fetcher, question="Majú obce problém s obstarávaním?")
    assert summary == "Našiel som jeden dôkaz."
    assert len(claim_ids) == 2  # duplicitný citát sa neuložil druhýkrát
    stats = run_verify(app.cfg, app.db, app.llm, Deadline(1), claim_ids=claim_ids)
    assert stats == {"verified": 1, "rejected": 1}
