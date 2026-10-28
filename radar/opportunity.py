"""Karta príležitosti pre konkrétnu organizáciu (zadanie 17. 10. 2026, časti B–D).

Väzba: organizácia + hypotéza + pracovný prípad (`process_key`) + rola adresáta. Karta dedí doménové podklady, ale
všeobecné tvrdenie o odvetví ostáva pre organizáciu hypotézou. Každý výrok má stav poznania (`source_fact`,
`contact_reported`, `data_observed`, `hypothesis`, `proposal`, `unknown`); rozpor sa eviduje zvlášť.

Tok: posúdenie dôkazov voči karte (relevance.py) → syntéza modelom (úloha `org_card`) → kontroly kódom (opora
organizácie, čísla, úspora dodávateľa, referencia používania produktu, najviac 3 možnosti, úplnosť skúšky) →
hodnotiteľ v dvoch častiach (opora / použiteľnosť) → tri pripravenosti (rozhovor / skúška / tvrdenie o prínose) a
hodnotiaca tabuľka → výskumné úlohy z medzier → ďalšia verzia karty. Nová verzia vzniká iba pri zmene vstupov alebo na
výslovnú žiadosť; zmena verzie logiky, promptu či politiky kartu iba označí ako zastaranú (žiadna hromadná regenerácia).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import Counter
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator

from .config import Config
from .db import DB
from .models import as_router
from .util import Deadline, utcnow

log = logging.getLogger(__name__)

CARD_SCHEMA_VERSION = "card-1"
ROLES = ("customer", "data_holder", "expert")
ROLE_LABEL = {"customer": "zákazník (používateľ alebo vlastník procesu)", "data_holder": "držiteľ dát", "expert": "expert"}
ROLE_TEXT = {"customer": "potential user or process owner in this organization (a possible customer)",
             "data_holder": "holder or steward of data (not a buyer) – their own benefit is a data check or a verifiable analysis",
             "expert": "domain expert (not a buyer) – their own benefit is a short verified summary or a correction"}
KINDS = {"source_fact": "zo zdroja", "contact_reported": "uviedol kontakt", "data_observed": "pozorované v dátach",
         "hypothesis": "hypotéza", "proposal": "návrh", "unknown": "nevieme"}
SUPPORTED = ("source_fact", "contact_reported", "data_observed")
INTERVENTIONS = {"process_change": "procesná zmena", "tool_configuration": "nastavenie existujúceho nástroja",
                 "integration": "integrácia", "task_automation": "automatizácia úkonu",
                 "quality_check": "kontrola úplnosti a kvality", "decision_support": "podpora rozhodovania",
                 "capacity_responsibility": "zmena kapacít alebo zodpovedností",
                 "analytics": "analytika (experiment Brány 2)"}
EVAL_LABELS = {"supported": "jasné s podkladom", "hypothesis": "jasná hypotéza", "missing": "chýba", "conflict": "rozpor"}
EVAL_ITEMS = (("adresát", "prečo tento adresát"), ("úkon", "konkrétny úkon"), ("dnešný_postup", "dnešný postup"),
              ("prekážka", "prekážka"), ("právomoc", "právomoc zasiahnuť"), ("riešenie", "existujúce riešenie / medzera"),
              ("zásah", "kandidátny zásah"), ("vstup", "minimálny vstup"), ("metrika", "metrika"), ("neistota", "neistota"))
DECISIONS = {"recipient": "adresát", "solution": "výber riešenia", "trial": "návrh skúšky"}
MAX_GAPS = 8  # medzier (výskumných úloh) na verziu karty – zlúčené podľa rozhodnutia, ktoré odpoveď zmení
DECISION_RANK = {"recipient": 0, "solution": 1, "trial": 2}
SOURCES = {"public_doc": "verejný dokument", "tool_docs": "dokumentácia nástroja", "contact": "kontakt (rozhovor)",
           "sample": "vzorka dokladov alebo dát", "event_log": "event log"}
PUBLIC_SOURCES = ("public_doc", "tool_docs")
RESEARCH_STATUS = {"open": "otvorené", "found": "nájdené", "partial": "čiastočné", "conflict": "rozpor",
                   "not_public": "verejne nezistiteľné", "unavailable": "zdroj nedostupný"}
CARD_STATUS = {"draft": "návrh", "checked": "skontrolované", "needs_review": "na kontrolu", "pending_review":
               "čaká na hodnotiteľa", "stale": "zastarané", "superseded": "nahradené", "edited": "upravené človekom"}

NATURAL_PERSON_RE = re.compile(r"fyzick|živnost|samostatne hospodár", re.I)
PROMISE_RE = re.compile(r"\bgarant|\bzaruč|\bsľubuj|\bistotou ušetr|\d+\s*%|percent", re.I)
SAVINGS_RE = re.compile(r"ušetr|úspor|zrýchl|skráti|menej (času|práce)|pracovn\w* čas", re.I)
HEDGE_RE = re.compile(r"\bmôž|\bmohl|\bby\b|overí|overi|zmer|odhad|hypotéz|predpoklad|skúšk|ak sa potvrdí|možn", re.I)
INTERNAL_RE = re.compile(r"koľko (času|hodín|minút)|ako často|frekvenc|dôvod\w* (zdržan|mešk|čakan|oprav)|skutočne používa|"
                         r"interné|pracovn\w* čas|trvá|mešk|posledn\w* prípad|kto (rozhoduje|schvaľuje) o kúp|rozpočet", re.I)
DATA_RE = re.compile(r"priebeh|trvan|čakan|odstup|lehot|termín|poradie krokov|vrát|opakovan", re.I)
TOOL_RE = re.compile(r"systém|softvér|nástroj|aplikáci|program|modul|erp|crm|tool", re.I)
SYNTHETIC_RE = re.compile(r"synthetic|syntet|demo|fixture|simul", re.I)
NUM_RE = re.compile(r"\d")


# ======================================================================= modely výstupu (zhovievavé k tvaru)
def _ids(value: Any) -> list[int]:
    out = []
    for v in value if isinstance(value, list) else [value]:
        if isinstance(v, int):
            out.append(v)
        elif isinstance(v, str):
            out += [int(x) for x in re.findall(r"\d+", v)]
    return out


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return str(value.get("text") or value.get("value") or "")
    if isinstance(value, list):
        return "; ".join(_text(v) for v in value if _text(v))
    return str(value)


class Lenient(BaseModel):
    """Model pripustí bežné odchýlky tvaru (reťazec namiesto objektu, jeden objekt namiesto zoznamu)."""

    @field_validator("*", mode="before")
    @classmethod
    def _coerce(cls, v: Any, info: Any) -> Any:
        field = cls.model_fields.get(info.field_name)
        if field is None:
            return v
        ann = field.annotation
        if ann is str:
            return _text(v)
        if ann == list[int]:
            return _ids(v)
        if getattr(ann, "__origin__", None) is list:
            if v is None:
                return []
            if not isinstance(v, list):
                return [v]
            inner = ann.__args__[0]
            if inner is str:
                return [_text(x) for x in v if _text(x)]
        return v


class Statement(Lenient):
    text: str = ""
    kind: str = "unknown"
    claims: list[int] = Field(default_factory=list)
    evidence: list[int] = Field(default_factory=list)
    rationale: str = ""
    falsify: str = ""

    @model_validator(mode="before")
    @classmethod
    def _from_str(cls, v: Any) -> Any:
        if isinstance(v, str):
            return {"text": v, "kind": "hypothesis"}
        return {} if v is None else v


class WorkCase(Lenient):
    trigger: Statement = Field(default_factory=Statement)
    unit: Statement = Field(default_factory=Statement)
    roles: Statement = Field(default_factory=Statement)
    input: Statement = Field(default_factory=Statement)
    steps: list[Statement] = Field(default_factory=list)
    tools: Statement = Field(default_factory=Statement)
    output: Statement = Field(default_factory=Statement)
    exceptions: Statement = Field(default_factory=Statement)


class Obstacle(Lenient):
    observed: list[Statement] = Field(default_factory=list)
    cause_hypotheses: list[Statement] = Field(default_factory=list)


class Authority(Lenient):
    who_can_act: Statement = Field(default_factory=Statement)
    what_changes: Statement = Field(default_factory=Statement)
    out_of_reach: Statement = Field(default_factory=Statement)


class CurrentSolution(Lenient):
    tool: Statement = Field(default_factory=Statement)
    functions: Statement = Field(default_factory=Statement)
    deployment: Statement = Field(default_factory=Statement)
    gap: Statement = Field(default_factory=Statement)


class Option(Lenient):
    type: str = "process_change"
    title: str = ""
    mechanism: str = ""
    dependencies: str = ""
    downsides: str = ""


class Trial(Lenient):
    input: str = ""
    output: str = ""
    comparison: str = ""
    metric: str = ""
    limits: str = ""
    stop_condition: str = ""


class PartnerAsk(Lenient):
    first_step: str = ""
    alternative_if_no_data: str = ""
    ask_kind: str = "technical_example"


class CardQuestion(Lenient):
    text: str = ""
    decision_changed: str = ""

    @model_validator(mode="before")
    @classmethod
    def _from_str(cls, v: Any) -> Any:
        if isinstance(v, str):
            return {"text": v}
        return {} if v is None else v


class Offer(Lenient):
    deliverable: str = ""
    possible_benefit: str = ""
    conditions: str = ""
    open_questions: list[str] = Field(default_factory=list)
    next_action: str = ""


class Example(Lenient):
    today: str = ""
    after: str = ""


class Conflict(Lenient):
    text: str = ""
    claims: list[int] = Field(default_factory=list)


class OrgCardOut(Lenient):
    process_key: str = ""
    why_org: list[Statement] = Field(default_factory=list)
    work_case: WorkCase = Field(default_factory=WorkCase)
    obstacle: Obstacle = Field(default_factory=Obstacle)
    authority: Authority = Field(default_factory=Authority)
    current_solution: list[CurrentSolution] = Field(default_factory=list)
    options: list[Option] = Field(default_factory=list)
    trial: Trial = Field(default_factory=Trial)
    partner_ask: PartnerAsk = Field(default_factory=PartnerAsk)
    main_question: CardQuestion = Field(default_factory=CardQuestion)
    interview_questions: list[CardQuestion] = Field(default_factory=list)
    offer: Offer = Field(default_factory=Offer)
    example: Example | None = None
    conflicts: list[Conflict] = Field(default_factory=list)


class CheckProblem(BaseModel):
    sentence: str = ""
    issue: str = ""


class CheckPart(BaseModel):
    verdict: Literal["ok", "problems"] = "ok"
    problems: list[CheckProblem] = Field(default_factory=list)


class CardCheckOut(BaseModel):
    support: CheckPart = Field(default_factory=CheckPart)
    usability: CheckPart = Field(default_factory=CheckPart)


class GapAnswerOut(Lenient):
    status: Literal["found", "partial", "conflict", "not_found"] = "not_found"
    answer: str = ""
    excerpts: list[int] = Field(default_factory=list)
    note: str = ""


# ======================================================================= vstupy karty
def is_natural_person(org) -> bool:
    """Fyzická osoba – podnikateľ (živnostník): jej názov je osobný údaj (cloud iba s povolením, export pseudonymizuje)."""
    return org is not None and bool(NATURAL_PERSON_RE.search(org["legal_form"] or ""))


def org_scope_key(org_id: int) -> str:
    """Kľúč podkladov z cieleného výskumu organizácie (tabuľka domain_evidence)."""
    return f"org:{org_id}"


def card_target(db: DB, org, hyp):
    from .outreach import is_public_org
    from .relevance import Target, programs_in

    return Target(key=f"card:{org['id']}:{hyp['id']}", title=hyp["title"], segment=hyp["segment"] or "",
                  org_id=org["id"], org_name=org["name"], org_public=is_public_org(org),
                  programs=programs_in(f"{hyp['title']} {hyp['description'] or ''} {hyp['segment'] or ''}"))


def _hash(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()[:20]


def _replies(db: DB, hid: int, org_id: int) -> list[dict[str, Any]]:
    """Odpovede kontaktov tejto organizácie (iba jej; poznámka bez mena osoby, e-mailov a telefónov)."""
    from .privacy import pseudonymize_text

    out = []
    for o in db.outcomes_for(hid):
        if o["org_id"] != org_id or empty_outcome(o):
            continue
        person = db.get_person(o["person_id"]) if o["person_id"] else None
        keys = o.keys()
        item = {"id": o["id"], "dátum": (o["recorded_at"] or "")[:10], "odpoveď": o["reply"],
                "problém": o["problem_relevance"], "poznámka": pseudonymize_text(o["note"] or "", [person["full_name"]]
                                                                                if person is not None else [])[:300]}
        for k in ("team_role", "last_case", "frequency", "tool_used", "unsolved_part", "process_owner", "buying_process",
                  "data_availability", "decline_reason", "card_version"):
            if k in keys and o[k]:
                item[k] = o[k] if k == "card_version" else pseudonymize_text(str(o[k]), [person["full_name"]]
                                                                             if person is not None else [])[:200]
        for flag in ("sample_obtained", "interview_agreed", "pilot_agreed", "value_shown"):
            if o[flag]:
                item[flag] = True
        out.append(item)
    return out


STRUCTURED_KEYS = ("team_role", "last_case", "frequency", "tool_used", "unsolved_part", "process_owner", "buying_process",
                   "data_availability", "decline_reason")
OUTCOME_FLAGS = ("sample_obtained", "interview_agreed", "demo_done", "pilot_agreed", "value_shown", "paid")


def empty_outcome(o) -> bool:
    """„Bez odpovede“ bez obsahu: nie je vyvrátenie ani nový podklad karty (nemení ju ani neoznačí na obnovu)."""
    keys = o.keys()
    return (o["reply"] in (None, "none") and not o["problem_relevance"] and not (o["note"] or "").strip()
            and not any(k in keys and o[k] for k in STRUCTURED_KEYS + OUTCOME_FLAGS))


def _experiment_rows(db: DB, hid: int, org_id: int) -> list[dict[str, Any]]:
    from .exp_tasks import verdict

    out = []
    for x in db.experiments_for(hid):
        if x["org_id"] != org_id or not x["result_json"]:
            continue
        res = json.loads(x["result_json"] or "{}")
        flags = json.loads(x["flags_json"] or "{}")
        spec = x["spec_json"] or ""
        v = verdict(res, flags)
        osi = res.get("osi") or {}
        out.append({"id": x["id"], "výrok": x["statement"][:200], "metrika": x["metric"], "verdikt": v["verdikt"],
                    "pokračovať": v["pokračovať"], "prekonal_porovnanie": (osi.get("silné_porovnanie") or {}).get("prekonal"),
                    "metodika_platná": (osi.get("metodika") or {}).get("platné", None) if osi else None,
                    "syntetické": bool(flags.get("synthetic") or SYNTHETIC_RE.search(spec[:400])),
                    "karta_proces": flags.get("karta_proces")})
    return out


def _data_results(db: DB, hid: int, org_id: int) -> dict[str, Any]:
    """Výsledky z dát organizácie: importy event logu (dostupnosť metrík), procesné reporty a experimenty."""
    out: dict[str, Any] = {"event_log": [], "experimenty": _experiment_rows(db, hid, org_id)}
    try:
        for imp in db.eventlog_imports(org_id=org_id):
            avail = json.loads(imp["availability_json"] or "{}")
            item = {"import": imp["id"], "stav": imp["status"], "obdobie": imp["period"], "riadky": imp["retained_rows"],
                    "dostupné_metriky": sorted(k for k, v in avail.items() if (v or {}).get("dostupné")) if isinstance(
                        avail, dict) else []}
            reports = db.process_reports(imp["id"])
            if reports:
                item["report"] = {"stav": reports[0]["status"],
                                  "obmedzenia": json.loads(reports[0]["limitations_json"] or "[]")[:5]}
            out["event_log"].append(item)
        out["žiadosti"] = [{"id": r["id"], "stav": r["status"], "alternatíva": r["alternative"]}
                           for r in db.eventlog_requests(hid=hid, org_id=org_id)]
    except Exception:  # noqa: BLE001 – staršia schéma bez event logov
        pass
    return out


def _profile(cfg: Config, db: DB, org, oh, hyp, include_roles: bool = True) -> dict[str, Any]:
    from .recipients import procurement_of, segment_fit

    fit = segment_fit(db, org, hyp)
    claim = db.get_claim(oh["evidence_claim_id"]) if oh is not None and oh["evidence_claim_id"] else None
    natural = is_natural_person(org)
    roles = []
    # funkcie ľudí bez mien – iba lokálne (alebo s privacy.cloud_personal_data): „konateľ“ malej s.r.o. je z registra
    # dohľadateľná osoba (vyhodnotenie 17. 10. 2026); pri fyzickej osobe nikdy
    if include_roles and not natural:
        roles = sorted({p["role_title"] for p in db.people_of_org(org["id"])
                        if p["role_title"] and p["status"] not in ("declined", "excluded", "objected")})[:6]
    return {"názov": org["name"], "ičo": org["ico"], "právna_forma": org["legal_form"], "sk_nace": org["sk_nace"],
            "veľkosť": org["size_category"], "sektor": org["sector_kind"], "web": org["website"] if org["website_verified"]
            else None, "dôvod_zaradenia": oh["relation"] if oh is not None else None,
            "zhoda_segmentu": fit, "obstarávanie": procurement_of(oh)["status"] if oh is not None else "none",
            "citát_zaradenia": {"id": claim["id"], "quote": claim["quote"][:300]} if claim is not None else None,
            "funkcie_kontaktov": roles, "fyzická_osoba": natural}


def card_inputs(cfg: Config, db: DB, org_id: int, hid: int, role: str = "customer", router=None) -> dict[str, Any]:
    """Všetky vstupy karty a ich hash (dôkazy s posúdením voči karte, prehľad domény, doménové a organizačné podklady,
    odpovede, dáta, výskum, profil; verzie schémy, promptov, politiky a logiky relevantnosti)."""
    from .prompts import prompt_version
    from .relevance import LOGIC_VERSION, assess_target, select_buckets
    from .value import _domain_claims, domain_evidence_items, domain_of, policy_version

    hyp, org = db.get_hypothesis(hid), db.get_organization(org_id)
    if hyp is None or org is None:
        raise ValueError("Karta potrebuje existujúcu organizáciu a hypotézu.")
    oh = db.org_hypothesis(org_id, hid)
    key, title, ids = domain_of(db, hid)
    target = card_target(db, org, hyp)
    route_router = router if router is not None else as_router(None, cfg, db)  # iba smerovanie, bez volania modelu
    in_cloud = route_router.is_cloud(route_router.route("org_card").role)
    include_roles = not in_cloud or cfg.privacy.cloud_personal_data
    pairs = assess_target(cfg, db, target, _domain_claims(db, ids), router, org_names={org_id: org["name"]})
    if oh is not None and oh["evidence_claim_id"]:  # citát, z ktorého organizácia vznikla, hovorí o nej
        pairs = [(e, {**a, "organization_id": org_id}) if e["id"] == oh["evidence_claim_id"] else (e, a) for e, a in pairs]
    oc = cfg.org_cards
    buckets = select_buckets(pairs, limit=oc.max_evidence, per_doc=oc.per_document, org_id=org_id)
    brief = db.latest_brief(key)
    dom = domain_evidence_items(db, key, cfg.domain_research.max_evidence_chunks)
    try:
        org_ev = list(db.domain_evidence(org_scope_key(org_id)))
    except Exception:  # noqa: BLE001
        org_ev = []
    replies = _replies(db, hid, org_id)
    data = _data_results(db, hid, org_id)
    # „zdroj nedostupný“ (výpadok) nie je nový podklad – nemení vstupy karty ani nespustí novú verziu
    research = [{"id": t["id"], "otázka": t["question"], "stav": t["status"], "výsledok": json.loads(t["result_json"] or "{}")}
                for t in db.research_results_for_org(org_id, hid) if t["status"] != "unavailable"]
    profile = _profile(cfg, db, org, oh, hyp, include_roles)
    versions = {"schéma": CARD_SCHEMA_VERSION, "prompt": prompt_version("org_card"), "kontrola": prompt_version(
        "org_card_check"), "politika": policy_version(cfg), "logika": LOGIC_VERSION}
    digest = _hash([(e["id"], e["quote"], a["relevance"], a["meaning"], a.get("organization_id"))
                    for name in buckets for e, a in buckets[name]],
                   (brief["id"], brief["inputs_hash"]) if brief is not None else None,
                   [(d["id"], d["content_hash"]) for d in dom + org_ev], replies, data, research, profile, role, versions)
    return {"hyp": hyp, "org": org, "oh": oh, "domain_key": key, "domain_title": title, "target": target,
            "buckets": buckets, "brief": brief, "dom": dom, "org_ev": org_ev, "replies": replies, "data": data,
            "research": research, "profile": profile, "role": role, "versions": versions, "hash": digest}


def card_user_text(inp: dict[str, Any]) -> str:
    from .relevance import bucket_text
    from .value import domain_evidence_text

    brief = inp["brief"]
    brief_txt = "-"
    if brief is not None:
        b = json.loads(brief["brief_json"] or "{}")
        brief_txt = json.dumps({k: b.get(k) for k in ("process_steps", "roles", "decision", "current_practice",
                                                     "existing_solutions", "alternative_explanations", "unknowns")},
                               ensure_ascii=False)[:5000]
    parts = [f"HYPOTHESIS: {inp['hyp']['title']}\nSEGMENT: {inp['hyp']['segment'] or '-'}\nRECIPIENT ROLE: {inp['role']}",
             "ORGANIZATION (registers, own publications, selection reason):\n<untrusted_document>\n"
             + json.dumps(inp["profile"], ensure_ascii=False) + "\n</untrusted_document>",
             "EVIDENCE in groups (cite ids in \"claims\"; only ABOUT THIS ORGANIZATION is a fact about it; vendors describe "
             "existing solutions and their savings are not transferable; CONTEXT ONLY is never evidence):\n"
             f"<untrusted_document>\n{bucket_text(inp['buckets']) or '-'}\n</untrusted_document>",
             f"DOMAIN BRIEF (general for the sector, version {brief['version'] if brief is not None else '-'} – never a "
             f"fact about this organization):\n<untrusted_document>\n{brief_txt}\n</untrusted_document>"]
    if inp["dom"] or inp["org_ev"]:
        parts.append("DOMAIN AND ORGANIZATION SOURCES (cite in \"evidence\"; scope says whether it is about this "
                     f"organization):\n<untrusted_document>\n{domain_evidence_text(inp['dom'] + inp['org_ev'])}\n"
                     "</untrusted_document>")
    parts.append("REPLIES (statements of contacts of THIS organization – not measured results):\n<untrusted_document>\n"
                 + (json.dumps(inp["replies"], ensure_ascii=False) if inp["replies"] else "none") + "\n</untrusted_document>")
    parts.append("DATA RESULTS:\n<untrusted_document>\n" + json.dumps(inp["data"], ensure_ascii=False) + "\n</untrusted_document>")
    if inp["research"]:
        parts.append("RESEARCH RESULTS:\n<untrusted_document>\n" + json.dumps(inp["research"], ensure_ascii=False)
                     + "\n</untrusted_document>")
    return "\n\n".join(parts)


# ======================================================================= kontroly kódom
ORG_BLOCKS = ("why_org", "work_case", "obstacle.observed", "authority", "current_solution")


def _statements(card: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """(blok, výrok) pre všetky výroky karty."""
    out = [("why_org", s) for s in card.get("why_org") or []]
    wc = card.get("work_case") or {}
    out += [("work_case", wc[k]) for k in ("trigger", "unit", "roles", "input", "tools", "output", "exceptions") if wc.get(k)]
    out += [("work_case", s) for s in wc.get("steps") or []]
    ob = card.get("obstacle") or {}
    out += [("obstacle.observed", s) for s in ob.get("observed") or []]
    out += [("obstacle.cause", s) for s in ob.get("cause_hypotheses") or []]
    au = card.get("authority") or {}
    out += [("authority", au[k]) for k in ("who_can_act", "what_changes", "out_of_reach") if au.get(k)]
    for cs in card.get("current_solution") or []:
        out += [("current_solution", cs[k]) for k in ("tool", "functions", "deployment", "gap") if cs.get(k)]
    return [(b, s) for b, s in out if isinstance(s, dict) and (s.get("text") or "").strip()]


def code_check_card(cfg: Config, out: OrgCardOut, inp: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Kontroly kódom; vráti (uložená karta, kontroly). Fakt bez platnej opory sa zoslabí na hypotézu (nemaže sa),
    všeobecný fakt o odvetví je pre organizáciu hypotéza, úspora dodávateľa sa neprenáša, referencia používania
    produktu nie je aktuálny problém. Výrok kontaktu iba z odpovedí, pozorovanie iba z dát."""
    from .relevance import SOLUTION_MEANINGS
    from .value import CITATION_RE, RANGE_RE
    from .verify import numbers_missing_in_source

    oc = cfg.org_cards
    card = out.model_dump()
    card.pop("process_key", None)
    problems: list[str] = []
    downgraded: list[str] = []
    buckets = inp["buckets"]
    assess = {e["id"]: a for name in buckets for e, a in buckets[name]}
    claims = {e["id"]: e for name in buckets for e, _ in buckets[name]}
    allowed = {cid for cid, a in assess.items() if cid in claims and a["relevance"] != "unrelated"} - {
        e["id"] for e, _ in buckets.get("context") or []}
    org_claims = {e["id"] for e, _ in buckets.get("organization") or []}
    allowed_ev = {d["id"] for d in inp["dom"]} | {d["id"] for d in inp["org_ev"]}
    org_ev = {d["id"] for d in inp["org_ev"] if d["scope"] == "organization"}
    source_text = " ".join(f"{e['problem']} {e['quote']}" for e in claims.values()) + " " + " ".join(
        d["excerpt"] for d in inp["dom"] + inp["org_ev"]) + " " + json.dumps(inp["replies"], ensure_ascii=False) + " " + \
        json.dumps(inp["data"], ensure_ascii=False)
    has_replies = any(r.get("odpoveď") not in (None, "none") for r in inp["replies"])
    has_data = bool(inp["data"].get("event_log") or inp["data"].get("experimenty"))

    for block, s in _statements(card):
        kind = s.get("kind") if s.get("kind") in KINDS else "hypothesis"
        s["kind"] = kind
        s["claims"] = [c for c in s.get("claims") or [] if c in allowed]
        s["evidence"] = [d for d in s.get("evidence") or [] if d in allowed_ev]
        label = f"{block}: {s['text'][:60]}"
        if (block == "obstacle.observed" and kind == "source_fact"
                and s["claims"] and all((assess.get(c) or {}).get("meaning") in SOLUTION_MEANINGS for c in s["claims"])):
            s["kind"], kind = "hypothesis", "hypothesis"
            s["rationale"] = ((s.get("rationale") or "") + " Zdroj je referencia používania riešenia – historický "
                                                           "problém, nie potvrdená aktuálna potreba.").strip()
            downgraded.append(f"{label} (referencia používania produktu)")
        vendor_refs = [c for c in s["claims"] if (assess.get(c) or {}).get("publisher_type") == "vendor"]
        if vendor_refs and block != "current_solution" and (SAVINGS_RE.search(s["text"]) or PROMISE_RE.search(s["text"])):
            s["claims"] = [c for c in s["claims"] if c not in vendor_refs]  # opis funkcie áno, úspora dodávateľa nie
            problems.append(f"úspora dodávateľa sa neprenáša na túto organizáciu ({label})")
        if kind == "source_fact":
            if not s["claims"] and not s["evidence"]:
                s["kind"] = "hypothesis"
                downgraded.append(f"{label} (bez platného zdroja)")
            elif block in ORG_BLOCKS and not (set(s["claims"]) & org_claims or set(s["evidence"]) & org_ev):
                s["kind"] = "hypothesis"
                s["rationale"] = ((s.get("rationale") or "") + " Všeobecný podklad o odvetví – pre túto organizáciu "
                                                                "predpoklad.").strip()
                downgraded.append(f"{label} (všeobecný podklad, nie fakt o organizácii)")
        elif kind == "contact_reported" and not has_replies:
            s["kind"] = "hypothesis"
            downgraded.append(f"{label} (žiadna odpoveď kontaktu)")
        elif kind == "data_observed" and not has_data:
            s["kind"] = "hypothesis"
            downgraded.append(f"{label} (žiadne dáta organizácie)")
        if s["kind"] in SUPPORTED:
            missing = numbers_missing_in_source(RANGE_RE.sub(" až ", CITATION_RE.sub(" ", s["text"])),
                                                RANGE_RE.sub(" až ", source_text))
            if missing:
                problems.append(f"čísla bez podkladu ({label}): {', '.join(missing[:4])}")
        if s["kind"] == "hypothesis" and not (s.get("rationale") or "").strip():
            problems.append(f"hypotéza bez odôvodnenia ({label}) – nižšia priorita")
        if s["kind"] == "hypothesis" and not (s.get("falsify") or "").strip() and block in ("obstacle.observed",
                                                                                           "obstacle.cause", "why_org"):
            problems.append(f"hypotéza bez spôsobu vyvrátenia ({label})")
    options = card.get("options") or []
    if len(options) > oc.max_options:
        problems.append(f"možností je {len(options)} (najviac {oc.max_options}) – ponechané prvé")
        card["options"] = options = options[: oc.max_options]
    for o in options:
        if o.get("type") not in INTERVENTIONS:
            problems.append(f"neznámy typ zásahu „{o.get('type')}“ – zmenené na procesnú zmenu")
            o["type"] = "process_change"
        if not (o.get("mechanism") or "").strip():
            problems.append(f"možnosť „{o.get('title', '')[:40]}“ nemá mechanizmus prínosu")
    if not options:
        problems.append("chýba kandidátny zásah")
    trial = card.get("trial") or {}
    missing_trial = [label for key, label in (("input", "vstup"), ("output", "výstup"), ("comparison", "porovnanie s dnešným "
                     "postupom"), ("metric", "metrika"), ("stop_condition", "podmienka nepokračovať")) if not trial.get(key)]
    if missing_trial:
        problems.append("skúška neúplná: chýba " + ", ".join(missing_trial))
    card["interview_questions"] = (card.get("interview_questions") or [])[: oc.max_interview_questions]
    if not (card.get("main_question") or {}).get("text"):
        problems.append("chýba hlavná otázka do oslovenia")
    for q in [card.get("main_question") or {}] + card["interview_questions"]:
        if q.get("text") and not q.get("decision_changed"):
            problems.append(f"otázka bez rozhodnutia, ktoré zmení: {q['text'][:60]}")
    offer = card.get("offer") or {}
    offer_text = " ".join(str(offer.get(k) or "") for k in ("deliverable", "possible_benefit", "conditions"))
    measured = any(x.get("pokračovať") and not x.get("syntetické") for x in inp["data"].get("experimenty") or [])
    if PROMISE_RE.search(offer_text) and not measured:
        problems.append("ponuka obsahuje percentá alebo záruku bez nameraného výsledku")
    if SAVINGS_RE.search(offer.get("possible_benefit") or "") and not HEDGE_RE.search(offer.get("possible_benefit") or "") \
            and not measured:
        problems.append("prínos je tvrdený ako istý – bez merania ho opíš ako možnosť overenú skúškou")
    if inp["role"] in ("data_holder", "expert") and re.search(r"predplat|licenci|\bcen[auy]\b|platen", offer_text, re.I):
        problems.append("adresát nie je kupujúci – bez predplatného, ceny a licencií")
    if card.get("example"):
        card["example"] = {**card["example"], "syntetické": True, "označenie": "ilustrácia – nie dôkaz ani výsledok pilotu"}
    card["conflicts"] = [{**c, "claims": [x for x in c.get("claims") or [] if x in allowed]}
                         for c in card.get("conflicts") or [] if c.get("text")]
    used = sorted({c for _, s in _statements(card) for c in s.get("claims") or []})
    checks = {"problémy": problems, "zoslabené": downgraded, "citáty": used,
              "organizačná_opora": sorted(set(used) & org_claims)}
    return card, checks


# ======================================================================= hodnotiteľ, stav, hodnotenie
REVIEW_MAX_TOKENS = 2400


def review_card(cfg: Config, router, card: dict[str, Any], inp: dict[str, Any], personal: bool) -> dict[str, Any]:
    """Hodnotiteľ v dvoch častiach (opora tvrdení / použiteľnosť návrhu); zlyhanie = čaká (nikdy nie schválenie)."""
    from .prompts import ORG_CARD_CHECK_SYSTEM, language_name
    from .value import evidence_text

    evidence = [e for name in ("process_problem", "organization", "solutions", "counter")
                for e, _ in inp["buckets"].get(name) or []]
    res = router.run_task("org_card_check", [
        {"role": "system", "content": ORG_CARD_CHECK_SYSTEM.format(lang=language_name(cfg.pipeline.output_language))},
        {"role": "user", "content": f"EVIDENCE:\n<untrusted_document>\n{evidence_text(evidence) or '-'}\n</untrusted_document>"
                                    f"\n\nREPLIES:\n<untrusted_document>\n{json.dumps(inp['replies'], ensure_ascii=False)}"
                                    f"\n</untrusted_document>\n\nCARD:\n<untrusted_document>\n"
                                    f"{json.dumps(card, ensure_ascii=False)}\n</untrusted_document>"}],
        CardCheckOut, object_type="organization", object_id=inp["org"]["id"], max_tokens=REVIEW_MAX_TOKENS,
        personal=personal)
    if not res.ok:
        return {"support": {"verdict": "pending", "problems": []}, "usability": {"verdict": "pending", "problems": []},
                "model": None, "dôvod": res.reason}
    return {"support": res.value.support.model_dump(), "usability": res.value.usability.model_dump(),
            "model": getattr(res, "model", None)}


def card_status(checks: dict[str, Any]) -> str:
    rev = checks.get("hodnotiteľ") or {}
    parts = [rev.get("support") or {}, rev.get("usability") or {}]
    if any(p.get("verdict") == "pending" for p in parts):
        return "pending_review"
    if checks.get("problémy") or any(p.get("verdict") == "problems" and p.get("problems") for p in parts):
        return "needs_review"
    return "checked"


def _grade(items: list[dict[str, Any]], conflicts: set[int]) -> str:
    items = [s for s in items if isinstance(s, dict) and (s.get("text") or "").strip()]
    if any(set(s.get("claims") or []) & conflicts for s in items):
        return "conflict"
    if any(s.get("kind") in SUPPORTED and (s.get("claims") or s.get("evidence") or s.get("kind") != "source_fact")
           for s in items):
        return "supported"
    if any(s.get("kind") in ("hypothesis", "proposal") for s in items):
        return "hypothesis"
    return "missing"


def _proposal_grade(*texts: str) -> str:
    return "hypothesis" if any((t or "").strip() for t in texts) else "missing"


def evaluate_card(card: dict[str, Any]) -> list[dict[str, str]]:
    """Hodnotiaca tabuľka karty (zadanie, časť E): pri každej položke jasné s podkladom / jasná hypotéza / chýba /
    rozpor. Žiadna percentuálna pravdepodobnosť úspechu."""
    wc = card.get("work_case") or {}
    au = card.get("authority") or {}
    cs = card.get("current_solution") or []
    conflicts = {c for x in card.get("conflicts") or [] for c in x.get("claims") or []}
    trial = card.get("trial") or {}
    unknowns = [s for _, s in _statements(card) if s.get("kind") == "unknown"]
    grades = {
        "adresát": _grade(card.get("why_org") or [], conflicts),
        "úkon": _grade([wc.get("unit"), wc.get("trigger")] + list(wc.get("steps") or [])[:1], conflicts),
        "dnešný_postup": _grade(list(wc.get("steps") or []) + [wc.get("tools")], conflicts),
        "prekážka": _grade((card.get("obstacle") or {}).get("observed") or [], conflicts),
        "právomoc": _grade([au.get("who_can_act"), au.get("what_changes")], conflicts),
        "riešenie": _grade([x.get(k) for x in cs for k in ("tool", "gap")], conflicts),
        "zásah": _proposal_grade(*[o.get("mechanism") for o in card.get("options") or []]),
        "vstup": _proposal_grade((card.get("partner_ask") or {}).get("first_step"), trial.get("input")),
        "metrika": _proposal_grade(trial.get("metric")),
        "neistota": "conflict" if card.get("conflicts") else ("supported" if unknowns else "missing"),
    }
    notes = {"neistota": f"{len(unknowns)} pomenovaných neznámych" + (f", {len(card.get('conflicts') or [])} rozporov"
                                                                   if card.get("conflicts") else ""),
             "riešenie": "nevieme, aký nástroj používajú – to neznamená, že nepoužívajú nič" if not cs else ""}
    return [{"položka": label, "stav": EVAL_LABELS[grades[key]], "kľúč": key, "poznámka": notes.get(key, "")}
            for key, label in EVAL_ITEMS]


def card_answers(card: dict[str, Any], org_name: str, role: str) -> dict[str, str]:
    """Päť hlavných otázok karty: komu pomáham → v akom úkone → čo sa zmení → aký minimálny vstup → ako overíme prínos."""
    wc, au, trial = card.get("work_case") or {}, card.get("authority") or {}, card.get("trial") or {}
    options = card.get("options") or []
    sel = card.get("_selected_option")
    opt = options[sel] if isinstance(sel, int) and 0 <= sel < len(options) else (options[0] if options else {})

    def t(s: Any) -> str:
        return (s or {}).get("text", "") if isinstance(s, dict) else str(s or "")

    return {"komu": f"{org_name} – {ROLE_LABEL.get(role, role)}" + (f"; konať môže: {t(au.get('who_can_act'))}"
                                                                    if t(au.get("who_can_act")) else ""),
            "úkon": " · ".join(x for x in (t(wc.get("unit")), t(wc.get("trigger"))) if x) or "chýba",
            "zmena": t(au.get("what_changes")) or (opt.get("mechanism") or "chýba"),
            "vstup": (card.get("partner_ask") or {}).get("first_step") or trial.get("input") or "chýba",
            "overenie": " · ".join(x for x in (trial.get("metric"), trial.get("comparison"),
                                               ("stop: " + trial["stop_condition"]) if trial.get("stop_condition") else "")
                                   if x) or "chýba"}


# ======================================================================= pripravenosť
def card_readiness(cfg: Config, db: DB, row) -> dict[str, Any]:
    """Tri nezávislé pripravenosti s dôvodmi: na rozhovor (obsah, nie kontroly odoslania), na návrh skúšky (zvlášť
    súhlas a dáta), na tvrdenie o prínose (porovnanie, význam dát, nie syntetické, väzba na úkon karty)."""
    from .recipients import procurement_of, segment_fit

    card = json.loads(row["card_json"] or "{}")
    org_id, hid = row["organization_id"], row["hypothesis_id"]
    org, hyp, oh = db.get_organization(org_id), db.get_hypothesis(hid), db.org_hypothesis(org_id, hid)
    # 1) rozhovor
    why = [s for s in card.get("why_org") or [] if (s.get("text") or "").strip()]
    fit = segment_fit(db, org, hyp)
    selection = oh is not None and (oh["evidence_claim_id"] or oh["relation"] in ("data_holder", "expert_org", "manual",
                                                                                  "procured_related"))
    question = (card.get("main_question") or {}).get("text") or ""
    hyp_clear = any(s.get("kind") == "hypothesis" and (s.get("rationale") or "").strip()
                    for s in (card.get("obstacle") or {}).get("observed") or [])
    channel = bool(db.org_contacts(org_id)) or bool(db.people_of_org(org_id)) or bool(org["website_verified"])
    interview_missing = [m for ok, m in (
        (bool(why), "dôvod výberu organizácie"),
        (row["recipient_role"] != "customer" or fit["zhoda"] != "nie", f"rola: {fit['dôvod']}"),
        (bool(question) or hyp_clear, "zrozumiteľná hypotéza alebo relevantná otázka"),
        (channel, "overený kanál (kontakt alebo overený web organizácie)")) if not ok]
    interview_why = ([f"dôvod zaradenia: {oh['relation']}" + (f" (C{oh['evidence_claim_id']})" if oh['evidence_claim_id']
                                                              else "")] if selection else [])
    if question:
        interview_why.append(f"hlavná otázka: {question[:120]}")
    if procurement_of(oh)["status"] in ("candidate", "confirmed") if oh is not None else False:
        interview_why.append("⚠ obstarávanie: " + procurement_of(oh)["label"] + " (kontroly odoslania sú samostatné)")
    # 2) skúška
    wc, trial = card.get("work_case") or {}, card.get("trial") or {}
    options = card.get("options") or []
    sel = row["selected_option"]
    opt = options[sel] if sel is not None and 0 <= sel < len(options) else (options[0] if options else None)
    trial_missing = [m for ok, m in (
        (any((s.get("text") or "").strip() for s in wc.get("steps") or []) or bool((wc.get("unit") or {}).get("text")),
         "konkrétny pracovný úkon"),
        (opt is not None, "kandidátny zásah"),
        (bool((card.get("partner_ask") or {}).get("first_step") or trial.get("input")), "potrebný vstup"),
        (bool(trial.get("output") and trial.get("limits")), "realizovateľný rozsah (výstup a obmedzenia)"),
        (bool(trial.get("metric")), "metrika"),
        (bool(opt and opt.get("dependencies")), "jasné závislosti")) if not ok]
    outcomes = [o for o in db.outcomes_for(hid) if o["org_id"] == org_id]
    consent = any(o["pilot_agreed"] or o["interview_agreed"] for o in outcomes)
    has_data = any(o["sample_obtained"] for o in outcomes)
    try:
        has_data = has_data or any(i["status"] == "checked" for i in db.eventlog_imports(org_id=org_id))
    except Exception:  # noqa: BLE001
        pass
    # 3) tvrdenie o prínose
    benefit_missing = []
    exps = _experiment_rows(db, hid, org_id)
    good = [x for x in exps if x["pokračovať"] and not x["syntetické"] and x["prekonal_porovnanie"] is True
            and x["metodika_platná"] is not False and x["karta_proces"] == row["process_key"]]
    if not exps:
        benefit_missing.append("žiadne porovnanie s dnešným alebo jednoduchým postupom na dátach tejto organizácie")
    elif not good:
        for x in exps:
            why_not = []
            if x["syntetické"]:
                why_not.append("syntetické dáta")
            if x["prekonal_porovnanie"] is not True:
                why_not.append("neprekonal relevantné jednoduché porovnanie")
            if x["metodika_platná"] is False:
                why_not.append("neplatná metodika (napr. význam entity/štítku)")
            if not x["pokračovať"]:
                why_not.append(f"verdikt: {x['verdikt']}")
            if x["karta_proces"] != row["process_key"]:
                why_not.append("výsledok nie je priradený k úkonu tejto karty (priradí človek)")
            benefit_missing.append(f"E{x['id']}: " + ", ".join(why_not))
    # 4) ponuka (M38): prínos overený a aspoň jeden nákladový údaj zákazníka – veľký trh ani odhad nestačí
    from .economics import economics

    econ = economics(db, hid, org_id)
    offer_missing = ([] if good else ["tvrdenie o prínose (porovnanie na dátach organizácie)"]) + (
        [] if econ["pripravené_na_ponuku"] else [econ["dôvod"]])
    ready = {"rozhovor": {"pripravené": not interview_missing, "dôvody": interview_why, "chýba": interview_missing},
             "skúška": {"pripravené": not trial_missing, "chýba": trial_missing, "súhlas": consent, "dáta": has_data,
                        "dôvody": [f"zásah: {INTERVENTIONS.get(opt['type'], opt['type'])} – {opt.get('title', '')}"]
                        if opt else []},
             "prínos": {"pripravené": bool(good), "chýba": benefit_missing,
                        "dôvody": [f"E{x['id']}: {x['verdikt']}" for x in good]},
             "ponuka": {"pripravené": not offer_missing, "chýba": offer_missing,
                        "dôvody": ([econ["dôvod"]] if econ["pripravené_na_ponuku"] else [])
                        + ([f"ročná hodnota: {econ['text']['ročná_hodnota']}"] if econ["ročná_hodnota"] else [])}}
    if interview_missing:
        nxt = "doplň: " + interview_missing[0]
    elif trial_missing:
        nxt = f"rozhovor – hlavná otázka: {question[:140]}" if question else "rozhovor o poslednom konkrétnom prípade"
    elif not consent or not has_data:
        nxt = "dohodnúť prvý krok: " + ((card.get("partner_ask") or {}).get("first_step") or "malá skúška")
    elif not good:
        nxt = "spustiť skúšku a porovnať s dnešným postupom (výsledok priradiť ku karte)"
    elif not econ["pripravené_na_ponuku"]:
        nxt = "doplň ekonomiku u zákazníka – " + (econ["otázky"][0] if econ["otázky"] else econ["dôvod"])
    else:
        nxt = "ponuka po potvrdení rozsahu človekom"
    ready["ďalší_krok"] = nxt
    return ready


# ======================================================================= vytvorenie a verzie
def _slug(text: str) -> str:
    from .privacy import fold

    s = re.sub(r"[^a-z0-9]+", "-", fold(text or "").lower()).strip("-")
    return s[:40] or "hlavny-pripad"


def current_card(db: DB, org_id: int, hid: int, role: str = "customer", process_key: str | None = None):
    """Aktuálna (nenahradená) karta organizácie pre hypotézu a rolu; najnovšia, ak je viac pracovných prípadov."""
    rows = [c for c in db.org_cards_for(hid, org_id) if c["recipient_role"] == role
            and (process_key is None or c["process_key"] == process_key)]
    return max(rows, key=lambda c: (c["updated_at"] or "", c["id"])) if rows else None


def ensure_card(cfg: Config, db: DB, llm: Any, org_id: int, hid: int, role: str = "customer", *, force: bool = False,
                process_key: str | None = None, stats: Counter | None = None) -> int | None:
    """Platná karta: nová verzia iba pri zmene vstupov (hash) alebo `force`. Zlyhanie modelu ponechá predošlú verziu.
    Vráti id karty."""
    stats = stats if stats is not None else Counter()
    router = as_router(llm, cfg, db)
    inp = card_inputs(cfg, db, org_id, hid, role, router)
    cur = current_card(db, org_id, hid, role, process_key)
    if cur is not None and cur["inputs_hash"] == inp["hash"] and not force:
        stats["nezmenené"] += 1
        return int(cur["id"])
    from .prompts import ORG_CARD_SYSTEM, language_name, prompt_version
    from .value import policy_version

    oc = cfg.org_cards
    personal = is_natural_person(inp["org"])
    system = ORG_CARD_SYSTEM.format(lang=language_name(cfg.pipeline.output_language), recipient=ROLE_TEXT[role],
                                    max_options=oc.max_options, max_questions=oc.max_interview_questions)
    res = router.run_task("org_card", [{"role": "system", "content": system},
                                       {"role": "user", "content": card_user_text(inp)}],
                          OrgCardOut, object_type="organization", object_id=org_id, max_tokens=9000, personal=personal)
    # 9000: karta štátnej organizácie (H11, veľa podkladov) sa pri 6000 + 4000 tokenoch uvažovania gpt-6-luna
    # orezala (vyhodnotenie 17. 10. 2026)
    if not res.ok:
        stats["čaká_osobné_údaje_cloud" if res.reason == "cloud_not_allowed" else "llm_failures"] += 1
        return int(cur["id"]) if cur is not None else None
    card, checks = code_check_card(cfg, res.value, inp)
    checks["hodnotiteľ"] = review_card(cfg, router, card, inp, personal)
    status = card_status(checks)
    key = cur["process_key"] if cur is not None else (process_key or _slug(res.value.process_key))
    sources = [{"claim_id": e["id"], "url": e["url"], "domain": e["domain"], "stiahnuté": (e["fetched_at"] or "")[:10],
                "skupina": name} for name in inp["buckets"] for e, _ in inp["buckets"][name] if e["id"] in checks["citáty"]]
    used_ev = {d for _, s in _statements(card) for d in s.get("evidence") or []}
    sources += [{"evidence_id": d["id"], "url": d["source_url"] or d["local_reference"], "rozsah": d["scope"],
                 "stiahnuté": (d["retrieved_at"] or "")[:10]} for d in inp["dom"] + inp["org_ev"] if d["id"] in used_ev]
    role_cfg = router.role_config(router.route("org_card").role)
    card_id = db.add_org_card(
        org_id, hid, key, role, card, inputs_hash=inp["hash"], status=status, sources_json=sources, checks_json=checks,
        evaluation_json=evaluate_card(card), brief_id=inp["brief"]["id"] if inp["brief"] is not None else None,
        schema_version=CARD_SCHEMA_VERSION, prompt_version=prompt_version("org_card"), policy_version=policy_version(cfg),
        provider=getattr(role_cfg, "provider", "local"), model=getattr(res, "model", None))
    if cur is not None and cur["approved_at"]:
        db.update_org_card(card_id, conflict_json={"text": f"predošlá verzia {cur['version']} bola schválená "
                                                           f"{cur['approved_at'][:10]} – nová verzia potrebuje nové schválenie"})
    db.update_org_card(card_id, readiness_json=card_readiness(cfg, db, db.get_org_card(card_id)))
    plan_research(cfg, db, card_id)
    stats[status] += 1
    return card_id


def refresh_readiness(cfg: Config, db: DB, card_id: int) -> dict[str, Any]:
    ready = card_readiness(cfg, db, db.get_org_card(card_id))
    db.update_org_card(card_id, readiness_json=ready)
    return ready


def version_staleness(cfg: Config, row) -> str | None:
    """Dôvod zastarania podľa verzií (schéma, prompt, politika) – bez prepočtu vstupov a bez volania modelu."""
    from .prompts import prompt_version
    from .value import policy_version

    diffs = [label for stored, now, label in ((row["schema_version"], CARD_SCHEMA_VERSION, "schéma karty"),
                                              (row["prompt_version"], prompt_version("org_card"), "prompt karty"),
                                              (row["policy_version"], policy_version(cfg), "obchodná politika"))
             if stored and stored != now]
    return ("zmena verzie: " + ", ".join(diffs)) if diffs else None


def mark_stale(cfg: Config, db: DB, hid: int | None = None, org_id: int | None = None, check_inputs: bool = False) -> int:
    """Označí karty na obnovu (zmena verzie; pri `check_inputs` aj zmena vstupov). Nič neregeneruje."""
    n = 0
    for row in db.org_cards_for(hid, org_id):
        reason = version_staleness(cfg, row)
        if reason is None and check_inputs:
            try:
                if card_inputs(cfg, db, row["organization_id"], row["hypothesis_id"], row["recipient_role"])["hash"] != \
                        row["inputs_hash"]:
                    reason = "zmena podkladov karty"
            except ValueError:
                continue
        if reason and row["stale_reason"] != reason:
            n += db.mark_org_cards_stale(reason, card_ids=[row["id"]])
    return n


def approve_card(db: DB, card_id: int) -> None:
    """Človek schváli verziu karty (rozsah skúšky ostáva návrh, kým ho človek nepotvrdí v ponuke)."""
    row = db.get_org_card(card_id)
    if row is None:
        raise ValueError("Karta neexistuje.")
    db.update_org_card(card_id, approved_at=utcnow(), conflict_json=None)


def edit_card(cfg: Config, db: DB, card_id: int, card: dict[str, Any], note: str = "") -> int:
    """Ručná oprava = nová verzia s autorom „human“ (pôvodná ostáva v histórii)."""
    row = db.get_org_card(card_id)
    if row is None:
        raise ValueError("Karta neexistuje.")
    checks = {"problémy": [], "zoslabené": [], "ručná_oprava": note or "bez poznámky", "pôvodná_verzia": row["version"]}
    new_id = db.add_org_card(row["organization_id"], row["hypothesis_id"], row["process_key"], row["recipient_role"], card,
                             inputs_hash=row["inputs_hash"], status="edited", author="human", checks_json=checks,
                             sources_json=json.loads(row["sources_json"] or "[]"), evaluation_json=evaluate_card(card),
                             brief_id=row["brief_id"], schema_version=row["schema_version"],
                             prompt_version=row["prompt_version"], policy_version=row["policy_version"],
                             provider="human", model=None)
    db.update_org_card(new_id, readiness_json=card_readiness(cfg, db, db.get_org_card(new_id)),
                       selected_option=row["selected_option"])
    return new_id


def select_trial(cfg: Config, db: DB, card_id: int, option_index: int) -> dict[str, Any]:
    row = db.get_org_card(card_id)
    options = json.loads(row["card_json"] or "{}").get("options") or []
    if not 0 <= option_index < len(options):
        raise ValueError("Neplatná možnosť.")
    db.update_org_card(card_id, selected_option=option_index)
    return refresh_readiness(cfg, db, card_id)


def link_experiment(db: DB, exp_id: int, card_id: int) -> None:
    """Človek priradí výsledok experimentu k úkonu karty (podmienka tvrdenia o prínose: zhoda s pracovným rozhodnutím)."""
    exp, row = db.get_experiment(exp_id), db.get_org_card(card_id)
    if exp is None or row is None:
        raise ValueError("Experiment alebo karta neexistuje.")
    if exp["org_id"] != row["organization_id"]:
        raise ValueError("Experiment nie je na dátach tejto organizácie.")
    flags = json.loads(exp["flags_json"] or "{}")
    flags["karta_proces"] = row["process_key"]
    db.update_experiment(exp_id, flags_json=flags)


# ======================================================================= cielený výskum medzier
def _t(s: Any) -> str:
    return (s or {}).get("text", "") if isinstance(s, dict) else str(s or "")


def card_gaps(card: dict[str, Any], org_name: str, process: str) -> list[dict[str, Any]]:
    """Prioritné medzery karty → otázky (bez duplicít), čo odpoveď zmení a najvhodnejší zdroj."""
    gaps: list[dict[str, Any]] = []

    def add(question: str, part: str, decision: str, source: str | None = None) -> None:
        if any(g["question"] == question for g in gaps):
            return
        if source is None:
            source = "contact" if INTERNAL_RE.search(question) else (
                "tool_docs" if part == "current_solution" and TOOL_RE.search(question) else "public_doc")
        gaps.append({"question": question, "gap_part": part, "decision_changed": decision, "best_source": source})

    def weak(items: list) -> bool:
        items = [s for s in items if isinstance(s, dict) and _t(s)]
        return not any(s.get("kind") in SUPPORTED for s in items)

    if weak(card.get("why_org") or []):
        add(f"Čím sa zaoberá {org_name} v oblasti „{process}“ (agenda, služby, kompetencie)?", "why_org", "recipient")
    au = card.get("authority") or {}
    if weak([au.get("who_can_act")]):
        add(f"Ktorý útvar alebo funkcia v {org_name} zodpovedá za „{process}“?", "authority", "recipient")
    cs = card.get("current_solution") or []
    tools = [_t(x.get("tool")) for x in cs if (x.get("tool") or {}).get("kind") not in (None, "unknown") and _t(x.get("tool"))]
    if not tools:
        add(f"Aký systém alebo nástroj používa {org_name} pri „{process}“ (referencie dodávateľov, vlastné zverejnenia)?",
            "current_solution", "solution", "public_doc")
    for tool in tools[:2]:
        if weak([x.get("functions") for x in cs if _t(x.get("tool")) == tool]):
            add(f"Aké funkcie a integrácie má {tool} pre „{process}“ (dokumentácia nástroja)?", "current_solution",
                "solution", "tool_docs")
    wc = card.get("work_case") or {}
    if weak(list(wc.get("steps") or [])):
        add(f"Ako prebehol v {org_name} posledný konkrétny prípad „{process}“ (kroky, kto, nástroje)?", "work_case",
            "trial", "contact")
    if weak((card.get("obstacle") or {}).get("observed") or []):
        add(f"Nastal v poslednom prípade v {org_name} jav, ktorý predpokladáme (ručná práca, čakanie, opravy)?",
            "obstacle", "trial", "contact")
    trial = card.get("trial") or {}
    if trial.get("input") or (card.get("partner_ask") or {}).get("first_step"):
        src = "event_log" if DATA_RE.search(f"{trial.get('metric', '')} {trial.get('input', '')}") else "sample"
        add(f"Má {org_name} podklady pre skúšku: {(trial.get('input') or card['partner_ask'].get('first_step'))[:120]}?",
            "trial", "trial", src)
    # neznáme výroky sa zlúčia podľa rozhodnutia, ktoré menia (nie každá neznáma = samostatná úloha; vyhodnotenie
    # 17. 10. 2026: 17 úloh na jednu kartu, väčšina duplicitných)
    covered = {g["gap_part"] for g in gaps}
    unknown: dict[str, list[str]] = {}
    for block, s in _statements(card):
        if s.get("kind") == "unknown":
            part = block.split(".")[0]  # podľa bloku karty (nie podľa slov – „systémy“ v právomoci nie je nástroj)
            if part == "work_case" and TOOL_RE.search(s["text"]):
                part = "current_solution"
            unknown.setdefault(part, []).append(s["text"].rstrip(".?"))
    for part, texts in unknown.items():
        if part in covered:
            continue
        decision = {"current_solution": "solution", "why_org": "recipient", "authority": "recipient"}.get(part, "trial")
        add("Zistiť: " + "; ".join(texts[:2])[:220] + "?", part, decision)
    gaps.sort(key=lambda g: (DECISION_RANK[g["decision_changed"]], g["best_source"] not in PUBLIC_SOURCES))
    return gaps[:MAX_GAPS]


def plan_research(cfg: Config, db: DB, card_id: int) -> int:
    """Výskumné úlohy z medzier karty (iba nové otázky). Interné otázky (čas, frekvencia, dôvod zdržania, skutočné
    používanie) idú na kontakt alebo dáta, nehľadajú sa na webe."""
    row = db.get_org_card(card_id)
    card = json.loads(row["card_json"] or "{}")
    process = row["process_key"].replace("-", " ")  # krátky názov pracovného prípadu (nie celá veta z karty)
    existing = {t["question"] for t in db.research_tasks_for(card_id)}
    host = _host(db.get_organization(row["organization_id"]))
    n = 0
    for i, g in enumerate(card_gaps(card, row["org_name"], process[:80])):
        if g["question"] in existing:
            continue
        queries = []
        nxt = None
        if g["best_source"] in PUBLIC_SOURCES:
            queries = research_queries(g["gap_part"], g["best_source"], row["org_name"], host, process,
                                       _known_tools(card))[: cfg.org_cards.research_max_queries_per_task]
        else:
            nxt = {"contact": "otázka do rozhovoru (verejné mlčanie nie je dôkaz)",
                   "sample": "požiadať o malú vzorku dokladov a správny výsledok",
                   "event_log": "žiadosť o event log iba ak ho otázka potrebuje (Event log a procesy)"}[g["best_source"]]
        db.add_research_task(card_id, g["question"], g["decision_changed"], g["best_source"], gap_part=g["gap_part"],
                             priority=DECISION_RANK[g["decision_changed"]] * 10 + i, queries=queries, next_step=nxt)
        n += 1
    return n


LEGAL_FORM_RE = re.compile(r",?\s*(s\.\s?r\.\s?o\.|spol\.\s?s\s?r\.\s?o\.|a\.\s?s\.|v\.\s?o\.\s?s\.|k\.\s?s\.|n\.\s?o\.)\s*$",
                           re.I)


def base_name(name: str) -> str:
    """Názov bez právnej formy („X & Y Účty, s.r.o.“ → „X & Y Účty“) – takto ho píše web aj články."""
    return LEGAL_FORM_RE.sub("", name or "").strip(" ,")


def _known_tools(card: dict[str, Any]) -> list[str]:
    """Názvy nástrojov z karty na dopyt: iba názov („Docspoint (podľa zdroja) alebo iný systém.“ → „Docspoint“)."""
    out = []
    for x in card.get("current_solution") or []:
        if (x.get("tool") or {}).get("kind") in (None, "unknown"):
            continue
        name = re.split(r"\s*[(,;:]|\s+(?:alebo|či|a iný|napr\.)\s+|\.\s|\.$", _t(x.get("tool")))[0].strip(" .")
        if name and len(name.split()) <= 4:
            out.append(name)
    return out[:2]


def research_queries(gap_part: str, best_source: str, org_name: str, host: str, process: str,
                     tools: list[str] | None = None) -> list[str]:
    """Krátke dopyty podľa druhu medzery – názov organizácie (bez právnej formy) alebo nástroja a pár slov procesu,
    nie slová otázky. Testovací beh 17. 10. 2026: dopyt „"<názov>, s.r.o." Ktorý útvar alebo funkcia …
    zodpovedá“ nevrátil nič použiteľné (vyhľadávač našiel heslo o jednom písmene)."""
    name = f'"{base_name(org_name)}"'
    proc = " ".join(process.split()[:4])
    if best_source == "tool_docs" and tools:
        return [f"{tools[0]} {proc} funkcie", f"{tools[0]} import dokladov integrácia"]
    # overený web organizácie má prednosť: názvy s jednopísmenovými slovami (napr. „X & Y“) vyhľadávač nenájde ani
    # v úvodzovkách, „site:<web> služby“ áno (testovací beh 17. 10. 2026)
    if host:
        site = f"site:{host}"
        by_part = {"why_org": [f"{site} služby", f"{name} služby"], "authority": [f"{site} kontakt", f"{name} kontakt"],
                   "current_solution": [f"{site} program", f"{name} referencia"]}
        out = by_part.get(gap_part) or [f"{site} {proc}", f"{name} {proc}"]
    else:
        by_part = {"why_org": [f"{name} služby", f"{name} o nás"], "authority": [f"{name} kontakt", f"{name} tím"],
                   "current_solution": [f"{name} softvér", f"{name} referencia"]}
        out = by_part.get(gap_part) or [f"{name} {proc}", name]
    return list(dict.fromkeys(q.strip() for q in out if q.strip()))


def _host(org) -> str:
    if org is None or not org["website"] or not org["website_verified"]:
        return ""
    return re.sub(r"^www\.", "", urlparse(org["website"]).netloc or org["website"].split("/")[0])


def _pick_tasks(tasks: list, limit: int) -> list:
    """Najviac `limit` úloh podľa poradia; aspoň jedna o existujúcom riešení, ak existuje (všeobecné dopyty
    nesmú minúť celý limit pred otázkami o riešeniach)."""
    chosen = list(tasks[:limit])
    solution = next((t for t in tasks if t["decision_changed"] == "solution"), None)
    if solution is not None and solution not in chosen and chosen:
        chosen[-1] = solution
    return chosen


def run_research(cfg: Config, db: DB, llm: Any, card_id: int, searx=None, fetcher=None,
                 deadline: Deadline | None = None) -> dict[str, Any]:
    """Ohraničený cielený výskum medzier karty: iba verejné zdroje, limity dopytov, dokumentov, času a pokusov, cache
    (už stiahnuté URL organizácie sa neopakujú, úloha sa po pokuse neopakuje). Výpadok → zdroj nedostupný, otázka ostáva."""
    from .domain_sources import normalize, select_chunks, stems
    from .prompts import GAP_ANSWER_SYSTEM, language_name

    oc = cfg.org_cards
    row = db.get_org_card(card_id)
    org = db.get_organization(row["organization_id"])
    stats: dict[str, Any] = {"úlohy": 0, "dopyty": 0, "dokumenty": 0, "model": 0}
    tasks = [t for t in db.research_tasks_for(card_id) if t["status"] == "open" and t["best_source"] in PUBLIC_SOURCES
             and t["attempts"] < oc.research_max_attempts]
    if searx is None or fetcher is None:
        stats["ukončenie"] = "vyhľadávanie nie je k dispozícii – úlohy ostávajú otvorené"
        return stats
    started = time.monotonic()
    budget = oc.research_max_seconds_per_card
    if deadline is not None:
        budget = min(budget, deadline.remaining_minutes() * 60)
    stop_at = started + budget
    key = org_scope_key(org["id"])
    seen = {e["source_url"] for e in db.domain_evidence(key)}
    host = _host(org)
    router = as_router(llm, cfg, db) if oc.research_model_calls else None
    for task in _pick_tasks(tasks, oc.research_max_tasks_per_card):
        if time.monotonic() >= stop_at:
            stats["ukončenie"] = "časový limit – zvyšné úlohy ostávajú otvorené"
            break
        stats["úlohy"] += 1
        cost = {"dopyty": 0, "dokumenty": 0, "model": 0}
        terms = stems(f"{task['question']} {org['name']}")
        excerpts: list[tuple[int, str, str]] = []  # (id podkladu, url, text)
        errors = []
        # už uložené odseky z webu organizácie (cache) sa použijú aj pre ďalšie otázky – inak by druhá úloha
        # stránku preskočila ako „už videnú“ a skončila „verejne nezistiteľné“ (testovací beh 17. 10. 2026)
        for e in db.domain_evidence(key):
            if sum(1 for t in terms if t in normalize(e["excerpt"])) >= 2:
                excerpts.append((e["id"], e["source_url"], e["excerpt"]))
        excerpts = excerpts[: oc.research_max_documents_per_task * oc.per_document]
        cost["z_cache"] = len(excerpts)
        for q in json.loads(task["queries_json"] or "[]")[: oc.research_max_queries_per_task]:
            if time.monotonic() >= stop_at:
                break
            try:
                results = searx.search(q)
            except Exception as exc:  # noqa: BLE001 – výpadok: otázka ostáva bez vymyslenej odpovede
                errors.append(f"vyhľadávanie: {type(exc).__name__}")
                break
            cost["dopyty"] += 1
            for r in results:
                if cost["dokumenty"] >= oc.research_max_documents_per_task or time.monotonic() >= stop_at:
                    break
                if r.url in seen:
                    continue
                seen.add(r.url)
                try:
                    page = fetcher.fetch(r.url)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"stiahnutie: {type(exc).__name__}")
                    continue
                if page is None:
                    continue
                cost["dokumenty"] += 1
                url = page.final_url or page.url
                own = bool(host) and _host_of(url).endswith(host)
                for _, chunk in select_chunks(page.text, terms, per_doc=oc.per_document):
                    # „o organizácii“ iba z jej overeného webu alebo s jej IČO v odseku – samotná zhoda názvu nestačí
                    # (testovací beh 17. 10. 2026: pri organizácii bez IČO a webu sa našli iné subjekty s rovnakým menom)
                    ico = re.sub(r"\D", "", org["ico"] or "")
                    scope = "organization" if own or (ico and ico in re.sub(r"\D", "", chunk)) else "sector"
                    eid = db.add_domain_evidence(key, "web", chunk, hashlib.sha256(chunk.encode()).hexdigest(), scope,
                                                 source_url=url, title=(page.title or "")[:200],
                                                 source_locator=f"výskumná úloha #{task['id']}",
                                                 source_version=(page.content_hash or "")[:16], language="sk")
                    excerpts.append((eid, url, chunk))
        stats["dopyty"] += cost["dopyty"]
        stats["dokumenty"] += cost["dokumenty"]
        fields: dict[str, Any] = {"attempts": task["attempts"] + 1}
        if not excerpts:
            status = "unavailable" if errors and not cost["dopyty"] else "not_public"
            fields.update(status=status, result_json={"answer": "", "note": "; ".join(errors) or "verejne sa nenašlo – "
                                                      "verejné mlčanie nie je dôkaz, že niečo neexistuje"},
                          next_step="otázka do rozhovoru" if status == "not_public" else "skús neskôr alebo v rozhovore")
        elif router is None:
            fields.update(status="partial", result_json={"answer": "", "note": "úryvky bez posúdenia modelom"},
                          sources_json=[{"evidence_id": e, "url": u} for e, u, _ in excerpts],
                          next_step="prečítaj úryvky a doplň kartu")
        else:
            text = "\n".join(f"[{e}] ({u}) „{c}“" for e, u, c in excerpts)
            res = router.run_task("gap_answer", [
                {"role": "system", "content": GAP_ANSWER_SYSTEM.format(lang=language_name(cfg.pipeline.output_language))},
                {"role": "user", "content": f"QUESTION: {task['question']}\nORGANIZATION: {org['name']}\n\nEXCERPTS:\n"
                                            f"<untrusted_document>\n{text}\n</untrusted_document>"}],
                GapAnswerOut, object_type="research_task", object_id=task["id"], max_tokens=1200,
                personal=is_natural_person(org))
            cost["model"] += 1
            stats["model"] += 1
            valid = {e for e, _, _ in excerpts}
            if not res.ok:
                fields.update(status="partial", result_json={"answer": "", "note": "posúdenie modelom zlyhalo – úryvky "
                                                                                   "uložené"},
                              sources_json=[{"evidence_id": e, "url": u} for e, u, _ in excerpts],
                              next_step="prečítaj úryvky a doplň kartu")
            else:
                used = [e for e in res.value.excerpts if e in valid]
                status = {"not_found": "not_public"}.get(res.value.status, res.value.status)
                if status in ("found", "partial", "conflict") and not used:
                    status = "not_public"  # odpoveď bez úryvku je nepodložená – neukladá sa ako nález
                note = res.value.note
                scopes = {e: (db.get_domain_evidence(e) or {"scope": None})["scope"] for e in used}
                if status == "found" and not any(v == "organization" for v in scopes.values()):
                    status = "partial"  # identita neoverená: úryvky nie sú z jej webu ani s jej IČO
                    note = (note + " · " if note else "") + ("úryvky nie sú z webu organizácie ani neobsahujú jej IČO – "
                                                             "nemusia sa jej týkať (over v rozhovore)")
                fields.update(status=status, result_json={"answer": res.value.answer if used else "",
                                                          "excerpts": used, "note": note},
                              sources_json=[{"evidence_id": e, "url": u} for e, u, _ in excerpts if e in used],
                              next_step={"found": "použiť v ďalšej verzii karty", "partial": "doplniť v rozhovore",
                                         "conflict": "rozpor – overiť v rozhovore",
                                         "not_public": "otázka do rozhovoru"}[status])
        fields["cost_json"] = {**cost, "sekundy": round(time.monotonic() - started, 1)}
        db.update_research_task(task["id"], **fields)
        stats[f"stav_{fields['status']}"] = stats.get(f"stav_{fields['status']}", 0) + 1
    stats["sekundy"] = round(time.monotonic() - started, 1)
    return stats


def _host_of(url: str) -> str:
    return re.sub(r"^www\.", "", urlparse(url).netloc)


# ======================================================================= výber organizácií a akcia
FOCUS_KEY = "org_cards_focus"
REQUEST_KEY = "org_cards_requests"  # karty vyžiadané v GUI (spracuje najbližšia úloha org_cards)


def focus_settings(db: DB) -> dict[str, Any]:
    """Režim sústredený na oslovenia: zapnutý/vypnutý a zoznam (organizácia, hypotéza, rola), predvolene najviac 3."""
    return db.get_setting(FOCUS_KEY, {"enabled": False, "items": []}) or {"enabled": False, "items": []}


def set_focus(cfg: Config, db: DB, enabled: bool, items: list[dict[str, Any]]) -> dict[str, Any]:
    limit = cfg.org_cards.focus_max_orgs
    clean = []
    for it in items:
        entry = {"org_id": int(it["org_id"]), "hypothesis_id": int(it["hypothesis_id"]), "role": it.get("role") or "customer"}
        if entry not in clean:
            clean.append(entry)
    if len({c["org_id"] for c in clean}) > limit:
        raise ValueError(f"V sústredenom režime najviac {limit} organizácie naraz (org_cards.focus_max_orgs).")
    value = {"enabled": bool(enabled), "items": clean, "updated_at": utcnow()}
    db.set_setting(FOCUS_KEY, value)
    return value


def request_card(db: DB, org_id: int, hid: int, role: str = "customer", force: bool = False) -> list[dict[str, Any]]:
    """GUI: vyžiada vytvorenie/obnovu karty – spracuje ju najbližšia úloha `org_cards` (správca úloh, dá sa zrušiť)."""
    items = [r for r in (db.get_setting(REQUEST_KEY, []) or [])
             if not (r["org_id"] == org_id and r["hypothesis_id"] == hid and r["role"] == role)]
    items.append({"org_id": int(org_id), "hypothesis_id": int(hid), "role": role, "force": bool(force)})
    db.set_setting(REQUEST_KEY, items)
    return items


def default_targets(cfg: Config, db: DB, hid: int, limit: int) -> list[dict[str, Any]]:
    """Bez sústredeného zoznamu: najvyššie odporúčané organizácie z „Koho osloviť“ (rola podľa skupiny)."""
    from .shortlist import contact_shortlist

    out, seen = [], set()
    for r in contact_shortlist(cfg, db, hid):
        if not r["org_id"] or r["org_id"] in seen or r["odporúčanie"] in ("neoslovovať", "preveriť rolu"):
            continue
        seen.add(r["org_id"])
        out.append({"org_id": r["org_id"], "hypothesis_id": hid, "role": r["skupina"] if r["skupina"] in ROLES
                    else "customer"})
        if len(out) >= limit:
            break
    return out


def run_org_cards(cfg: Config, db: DB, llm: Any, deadline: Deadline, ids: list[int] | None = None, searx=None,
                  fetcher=None, targets: list[dict[str, Any]] | None = None, force: bool = False) -> dict[str, Any]:
    """Akcia `org_cards`: karty pre sústredený zoznam (alebo zadané ciele, inak najvyššie odporúčané organizácie
    hypotéz Brány 0 – najviac `org_cards.focus_max_orgs` naraz), cielený výskum medzier a ďalšia verzia karty, ak
    výskum niečo našiel. Zastarané karty iba označí; regeneruje len zvolenú dávku."""
    stats: Counter = Counter()
    if not cfg.org_cards.enabled:
        return {"preskočené": "org_cards.enabled = false"}
    focus = focus_settings(db)
    requested = db.get_setting(REQUEST_KEY, []) or []
    if targets is None and requested:  # výslovne vyžiadané karty (GUI) majú prednosť; po spracovaní sa vymažú
        db.set_setting(REQUEST_KEY, [])
        targets = requested
    elif targets is None:
        if focus["enabled"] and focus["items"]:
            targets = focus["items"]
        else:
            targets = []
            for hid in ids or [h["id"] for h in db.automation_hypotheses()]:
                targets += default_targets(cfg, db, hid, cfg.org_cards.focus_max_orgs - len(targets))
                if len(targets) >= cfg.org_cards.focus_max_orgs:
                    break
    stats["označené_zastarané"] = mark_stale(cfg, db)
    for t in targets[: max(cfg.org_cards.focus_max_orgs, 1) * 3]:
        if deadline.expired():
            stats["odložené_na_ďalší_beh"] += 1
            break
        try:
            card_id = ensure_card(cfg, db, llm, t["org_id"], t["hypothesis_id"], t.get("role") or "customer",
                                  force=force or bool(t.get("force")), stats=stats)
        except ValueError as exc:
            stats["chyby"] += 1
            log.warning("Karta %s: %s", t, exc)
            continue
        if card_id is None:
            continue
        stats["karty"] += 1
        res = run_research(cfg, db, llm, card_id, searx, fetcher, deadline)
        found = sum(v for k, v in res.items() if k in ("stav_found", "stav_partial", "stav_conflict"))
        stats["výskum_úlohy"] += res.get("úlohy", 0)
        if found and not deadline.expired():
            ensure_card(cfg, db, llm, t["org_id"], t["hypothesis_id"], t.get("role") or "customer", stats=stats)
            stats["nová_verzia_po_výskume"] += 1
    return dict(stats)


# ======================================================================= odpovede (učenie)
def on_outcome(cfg: Config, db: DB, hid: int, org_id: int | None, outcome_id: int, contradicts: bool = False) -> dict:
    """Nová odpoveď: iba závislé karty a návrhy tejto organizácie sa označia na obnovu (nič sa neprepíše); schválená
    karta dostane viditeľný rozpor, ak odpoveď vyvracia alebo odporuje. Iná organizácia ani odvetvie sa nemení."""
    if not org_id:
        return {}
    o = next((x for x in db.outcomes_for(hid) if x["id"] == outcome_id), None)
    if o is not None and empty_outcome(o) and not contradicts:
        return {"karty_na_obnovu": 0, "návrhy_na_obnovu": 0, "poznámka": "bez odpovede – karta sa nemení"}
    reason = f"nová odpoveď (výsledok #{outcome_id}" + (f": {o['problem_relevance']}" if o is not None and
                                                         o["problem_relevance"] else "") + ")"
    cards = db.org_cards_for(hid, org_id)
    n = db.mark_org_cards_stale(reason, card_ids=[c["id"] for c in cards])
    refuted = o is not None and (o["problem_relevance"] == "refuted" or contradicts)
    for c in cards:
        if c["approved_at"] and refuted:
            db.update_org_card(c["id"], conflict_json={"text": f"odpoveď #{outcome_id} po schválení "
                                                               f"{c['approved_at'][:10]} odporuje karte – preveriť",
                                                       "outcome_id": outcome_id})
    proposals = [p for p in db.value_proposals_for(hid) if p["card_id"] in {c["id"] for c in cards}]
    for p in proposals:
        db.update_value_proposal(p["id"], stale_reason=reason)
    return {"karty_na_obnovu": n, "návrhy_na_obnovu": len(proposals)}


# ======================================================================= ponuka a export
def offer_for(db: DB, org_id: int | None, hid: int, role: str) -> dict[str, Any] | None:
    """Ponuka z aktuálnej karty (organizácia + hypotéza + rola); bez karty None (iba situačná otázka)."""
    if not org_id:
        return None
    row = current_card(db, org_id, hid, role)
    if row is None:
        return None
    card = json.loads(row["card_json"] or "{}")
    offer = card.get("offer") or {}
    au = card.get("authority") or {}
    obstacle = [s for s in (card.get("obstacle") or {}).get("observed") or [] if _t(s)]
    return {"card_id": row["id"], "card_version": row["version"], "process_key": row["process_key"],
            "stav": row["status"], "zastarané": row["stale_reason"], "rozpor": json.loads(row["conflict_json"] or "null"),
            "potreba": _t(obstacle[0]) if obstacle else "", "potreba_druh": obstacle[0].get("kind") if obstacle else None,
            "výstup": offer.get("deliverable") or "", "prínos": offer.get("possible_benefit") or "",
            "rozhodnutie": _t(au.get("what_changes")), "minimálny_vstup": (card.get("partner_ask") or {}).get("first_step")
            or "", "alternatíva": (card.get("partner_ask") or {}).get("alternative_if_no_data") or "",
            "čo_nevieme": "; ".join(offer.get("open_questions") or []), "podmienky": offer.get("conditions") or "",
            "hlavná_otázka": card.get("main_question") or {},
            "otázky": [q.get("text") for q in card.get("interview_questions") or [] if q.get("text")]}


def _org_label(org, with_names: bool) -> str:
    if is_natural_person(org) and not with_names:
        return f"Organizácia #{org['id']} (fyzická osoba – pseudonymizované)"
    return org["name"]


def card_markdown(db: DB, row, with_names: bool = False) -> str:
    """Karta ako Markdown (GUI, export, dosiér): päť odpovedí, tri pripravenosti, hodnotenie, bloky so stavom poznania,
    ponuka, otázky, zdroje, medzery a verzia použitého prehľadu domény. Odpovede kontaktov sú bez mien."""
    card = json.loads(row["card_json"] or "{}")
    org = db.get_organization(row["organization_id"])
    ready = json.loads(row["readiness_json"] or "{}")
    checks = json.loads(row["checks_json"] or "{}")
    evaluation = json.loads(row["evaluation_json"] or "[]")
    brief = db.get_brief(row["brief_id"]) if row["brief_id"] else None
    name = _org_label(org, with_names)
    card["_selected_option"] = row["selected_option"]
    answers = card_answers(card, name, row["recipient_role"])
    out = [f"# Karta organizácie: {name}", "",
           f"Hypotéza H{row['hypothesis_id']} · pracovný prípad `{row['process_key']}` · adresát "
           f"{ROLE_LABEL.get(row['recipient_role'], row['recipient_role'])} · verzia {row['version']} · stav "
           f"{CARD_STATUS.get(row['status'], row['status'])}" + (f" · **zastarané:** {row['stale_reason']}"
                                                                if row["stale_reason"] else ""), ""]
    if row["conflict_json"]:
        out += [f"> ⚠ Rozpor: {json.loads(row['conflict_json']).get('text', '')}", ""]
    out += ["## Päť odpovedí", "", f"1. **Komu pomáham:** {answers['komu']}", f"2. **V akom úkone:** {answers['úkon']}",
            f"3. **Čo sa zmení:** {answers['zmena']}", f"4. **Minimálny vstup:** {answers['vstup']}",
            f"5. **Ako overíme prínos:** {answers['overenie']}", "", "## Pripravenosť (nezávislé úrovne)", ""]
    for key, label in (("rozhovor", "Na rozhovor"), ("skúška", "Na návrh skúšky"), ("prínos", "Na tvrdenie o prínose"),
                       ("ponuka", "Na ponuku")):
        r = ready.get(key) or {}
        line = f"- **{label}:** {'áno' if r.get('pripravené') else 'nie'}"
        if r.get("dôvody"):
            line += " – " + "; ".join(r["dôvody"])
        if r.get("chýba"):
            line += " · chýba: " + "; ".join(r["chýba"])
        if key == "skúška":
            line += f" · súhlas: {'áno' if r.get('súhlas') else 'nie'} · dáta: {'áno' if r.get('dáta') else 'nie'}"
        out.append(line)
    if ready.get("ďalší_krok"):
        out += ["", f"**Ďalší krok:** {ready['ďalší_krok']}"]
    rev = checks.get("hodnotiteľ") or {}
    out += ["", "## Kontroly (oddelene od pripravenosti a pokrytia)", "",
            f"- kód: {len(checks.get('problémy') or [])} problémov, {len(checks.get('zoslabené') or [])} zoslabených výrokov",
            f"- hodnotiteľ – opora tvrdení: {(rev.get('support') or {}).get('verdict', '-')}, použiteľnosť: "
            f"{(rev.get('usability') or {}).get('verdict', '-')}"]
    for p in (checks.get("problémy") or [])[:8]:
        out.append(f"  - {p}")
    for part in ("support", "usability"):
        for p in ((rev.get(part) or {}).get("problems") or [])[:4]:
            out.append(f"  - hodnotiteľ ({'opora' if part == 'support' else 'použiteľnosť'}): „{p.get('sentence', '')}“ – "
                       f"{p.get('issue', '')}")
    out += ["", "## Hodnotenie karty", "", "| položka | stav | poznámka |", "|---|---|---|"]
    out += [f"| {e['položka']} | {e['stav']} | {e.get('poznámka', '')} |" for e in evaluation]

    def st(s: Any) -> str:
        if not isinstance(s, dict) or not _t(s):
            return ""
        refs = [f"[{c}]" for c in s.get("claims") or []] + [f"[D{d}]" for d in s.get("evidence") or []]
        extra = ""
        if s.get("kind") == "hypothesis":
            extra = (f" _(dôvod: {s['rationale']})_" if s.get("rationale") else "") + (
                f" _(vyvráti: {s['falsify']})_" if s.get("falsify") else "")
        return f"{s['text']} — *{KINDS.get(s.get('kind'), s.get('kind'))}* {' '.join(refs)}{extra}".strip()

    def block(title: str, items: list) -> None:
        lines = [st(s) for s in items if st(s)]
        if lines:
            out.extend(["", f"## {title}", ""] + [f"- {x}" for x in lines])

    wc = card.get("work_case") or {}
    block("Prečo práve táto organizácia", card.get("why_org") or [])
    block("Pracovný prípad", [wc.get(k) for k in ("trigger", "unit", "roles", "input")] + list(wc.get("steps") or [])
          + [wc.get(k) for k in ("tools", "output", "exceptions")])
    ob = card.get("obstacle") or {}
    block("Prekážka – pozorovaný jav", ob.get("observed") or [])
    block("Prekážka – hypotézy príčiny", ob.get("cause_hypotheses") or [])
    au = card.get("authority") or {}
    block("Možnosť zásahu", [au.get(k) for k in ("who_can_act", "what_changes", "out_of_reach")])
    sols = card.get("current_solution") or []
    if sols:
        block("Dnešné riešenie", [x.get(k) for x in sols for k in ("tool", "functions", "deployment", "gap")])
    else:
        out += ["", "## Dnešné riešenie", "", "- nevieme (to neznamená, že nepoužívajú nič)"]
    if card.get("options"):
        out += ["", "## Možnosti riešenia (návrhy)", ""]
        for i, o in enumerate(card["options"]):
            mark = " **(vybraná)**" if row["selected_option"] == i else ""
            out.append(f"{i + 1}. **{o.get('title', '')}** ({INTERVENTIONS.get(o.get('type'), o.get('type'))}){mark} – "
                       f"mechanizmus: {o.get('mechanism', '')}; závislosti: {o.get('dependencies', '')}; nevýhody: "
                       f"{o.get('downsides', '')}")
    trial = card.get("trial") or {}
    if any(trial.values()):
        out += ["", "## Odporúčaná malá skúška (návrh – rozsah potvrdí človek)", ""]
        out += [f"- {label}: {trial.get(k) or 'chýba'}" for k, label in (
            ("input", "vstup"), ("output", "výstup"), ("comparison", "porovnanie s dnešným postupom"), ("metric", "metrika"),
            ("limits", "obmedzenia"), ("stop_condition", "podmienka nepokračovať"))]
    pa = card.get("partner_ask") or {}
    out += ["", "## Čo potrebujeme od partnera", "", f"- prvý krok: {pa.get('first_step') or 'chýba'} "
            f"({'technický príklad' if pa.get('ask_kind') != 'effect_sample' else 'vzorka na preukázanie účinku'})",
            f"- ak dáta neexistujú: {pa.get('alternative_if_no_data') or 'chýba'}"]
    mq = card.get("main_question") or {}
    out += ["", "## Otázky", "", f"- **Hlavná otázka do oslovenia:** {mq.get('text') or 'chýba'}"
            + (f" _(zmení: {mq['decision_changed']})_" if mq.get("decision_changed") else "")]
    out += [f"- {q.get('text')}" + (f" _(zmení: {q['decision_changed']})_" if q.get("decision_changed") else "")
            for q in card.get("interview_questions") or [] if q.get("text")]
    offer = card.get("offer") or {}
    out += ["", "## Ponuka", "", f"- výstup: {offer.get('deliverable') or 'chýba'}",
            f"- možný prospech: {offer.get('possible_benefit') or 'chýba'}", f"- podmienky: {offer.get('conditions') or '-'}",
            f"- nezodpovedané otázky: {'; '.join(offer.get('open_questions') or []) or '-'}",
            f"- ďalšia akcia: {offer.get('next_action') or '-'}"]
    ex = card.get("example")
    if ex and (ex.get("today") or ex.get("after")):
        out += ["", "## Príklad (ILUSTRÁCIA – syntetické, nie dôkaz ani výsledok pilotu)", "",
                f"- dnes: {ex.get('today', '')}", f"- po zmene: {ex.get('after', '')}"]
    if card.get("conflicts"):
        out += ["", "## Rozpory", ""] + [f"- {c['text']} {' '.join(f'[{x}]' for x in c.get('claims') or [])}"
                                        for c in card["conflicts"]]
    tasks = db.research_tasks_for(row["id"])
    if tasks:
        out += ["", "## Medzery a cielený výskum", "", "| otázka | zmení | zdroj | stav | ďalší krok |", "|---|---|---|---|---|"]
        for t in tasks:
            res = json.loads(t["result_json"] or "{}")
            ans = f" – {res['answer']}" if res.get("answer") else ""
            out.append(f"| {t['question']}{ans} | {DECISIONS.get(t['decision_changed'], t['decision_changed'])} | "
                       f"{SOURCES.get(t['best_source'], t['best_source'])} | {RESEARCH_STATUS.get(t['status'], t['status'])}"
                       f" | {t['next_step'] or ''} |")
    sources = json.loads(row["sources_json"] or "[]")
    if sources:
        out += ["", "## Zdroje", ""]
        out += [f"- [{s['claim_id']}] {s['url']} (stiahnuté {s.get('stiahnuté', '')}, skupina {s.get('skupina', '')})"
                if "claim_id" in s else f"- [D{s['evidence_id']}] {s['url']} (rozsah {s.get('rozsah', '')})" for s in sources]
    out += ["", "## Verzie", "",
            f"- prehľad domény: {('v' + str(brief['version']) + ' (#' + str(brief['id']) + ')') if brief is not None else 'žiadny'}"
            " – všeobecný pre odvetvie, nie fakt o organizácii",
            f"- schéma {row['schema_version']}, prompt {row['prompt_version']}, politika {row['policy_version']}, "
            f"poskytovateľ {row['provider']}, model {row['model']}, autor {row['author']}, vytvorené {row['created_at'][:16]}"]
    return "\n".join(out) + "\n"


def cards_markdown(db: DB, rows: list, with_names: bool = False) -> str:
    return "\n\n---\n\n".join(card_markdown(db, r, with_names) for r in rows)


# ======================================================================= porovnanie starého a nového formátu
def legacy_offer(db: DB, hid: int, role: str) -> dict[str, Any]:
    """Ponuka podľa logiky pred v15 (rekonštrukcia `build_package`): prvý všeobecný návrh hypotézy pre všetkých
    adresátov danej roly (schválený → skontrolovaný → čakajúci), inak „nevieme“, a hlavná otázka domény."""
    from .value import best_proposal, domain_of

    proposal = best_proposal(db, hid, role)
    p = json.loads(proposal["proposal_json"]) if proposal is not None else {}
    main = None
    try:
        from .domain import main_question

        main = main_question(db, domain_of(db, hid)[0], role)
    except Exception:  # noqa: BLE001
        pass
    any_general = db.value_proposals_for(hid, role)
    return {"potreba": p.get("need_statement") or "nevieme – prvým krokom je otázka na skúsenosť",
            "výstup": p.get("deliverable") or "", "minimálny_vstup": p.get("minimum_input") or "",
            "hlavná_otázka": main["question"] if main is not None else "",
            "poznámka": (f"všeobecný návrh #{proposal['id']} ({proposal['status']}) – rovnaký pre všetkých adresátov roly"
                         if proposal is not None else
                         f"žiadny použiteľný všeobecný návrh ({len(any_general)} v stave na úpravu) – rovnaký "
                         "generický text pre všetkých")}


def comparison_markdown(cfg: Config, db: DB, targets: list[dict[str, Any]], pseudonymize: bool = True) -> str:
    """Starý (všeobecná ponuka hypotézy) vs. nový výstup (karta organizácie) pre zvolené organizácie. Pseudonymizovaná
    verzia (dokumentácia) nahradí názvy organizácií a webové adresy; bez mien osôb a kontaktov v oboch verziách."""
    labels: dict[int, str] = {}

    def name(org) -> str:
        if not pseudonymize:
            return _org_label(org, False)
        labels.setdefault(org["id"], f"Organizácia {chr(ord('A') + len(labels))}")
        return labels[org["id"]]

    def scrub(text: str, org) -> str:
        if not pseudonymize:
            return text
        label = name(org)
        base = re.sub(r",?\s*(s\.\s?r\.\s?o\.|a\.\s?s\.|spol\.)\s*$", "", org["name"], flags=re.I).strip()
        out = text
        for variant in sorted({org["name"], base}, key=len, reverse=True):  # aj bez právnej formy a so skloňovaním
            out = re.sub(re.escape(variant) + r"\w*", label, out)
        words = [w for w in re.split(r"[\s,()&.]+", base) if len(w) >= 5
                 and w.lower() not in ("services", "solutions", "slovensko", "slovenská", "republika", "spoločnosť")]
        for word in words:  # kmeň slova zachytí pády („Alfe“, „Alfou“); radšej viac než menej
            stem = word[:-1] if len(word) > 5 else word
            out = re.sub(r"\b" + re.escape(stem) + r"\w*", label if len(words) == 1 else "[názov]", out, flags=re.I)
        return re.sub(r"https?://\S+", "[odkaz]", out)

    out = ["# Porovnanie: všeobecná ponuka (pred v15) → karta organizácie (v15)", "",
           f"Vygenerované {utcnow()[:16]} UTC. Starý formát je rekonštrukcia `build_package` pred v15 (prvý všeobecný "
           "návrh hypotézy pre všetkých adresátov roly). Nový je aktuálna karta organizácie. Výsledky modelov sú "
           "návrhy na kontrolu človekom, nie overenie u zákazníka." + (" Názvy organizácií sú pseudonymizované." if
                                                                        pseudonymize else ""), ""]
    for t in targets:
        org, hyp = db.get_organization(t["org_id"]), db.get_hypothesis(t["hypothesis_id"])
        role = t.get("role") or "customer"
        if org is None or hyp is None:
            continue
        old = legacy_offer(db, hyp["id"], role)
        row = current_card(db, org["id"], hyp["id"], role)
        profile = f"SK NACE {org['sk_nace'] or '-'}, sektor {org['sector_kind'] or 'neuvedený'}"
        out += [f"## {name(org)} – H{hyp['id']} ({ROLE_LABEL.get(role, role)}; {profile})", "",
                "### Pred v15 (všeobecná ponuka)", "", f"- potreba: {old['potreba']}", f"- výstup: {old['výstup'] or '–'}",
                f"- minimálny vstup: {old['minimálny_vstup'] or '–'}", f"- hlavná otázka: {old['hlavná_otázka'] or '–'}",
                f"- poznámka: {old['poznámka']}", "", "### v15 (karta organizácie)", ""]
        if row is None:
            out += ["- karta zatiaľ nevznikla (spusti `python -m radar org-cards --run --target ORG:HYP`)", ""]
            continue
        card = json.loads(row["card_json"] or "{}")
        card["_selected_option"] = row["selected_option"]
        ans = card_answers(card, name(org), role)
        ready = json.loads(row["readiness_json"] or "{}")
        offer = card.get("offer") or {}
        out += [f"- verzia {row['version']}, stav {CARD_STATUS.get(row['status'], row['status'])}, model {row['model']}, "
                f"poskytovateľ {row['provider']}", f"- komu: {scrub(ans['komu'], org)}", f"- úkon: {scrub(ans['úkon'], org)}",
                f"- zmena: {scrub(ans['zmena'], org)}", f"- minimálny vstup: {scrub(ans['vstup'], org)}",
                f"- overenie: {scrub(ans['overenie'], org)}",
                f"- ponuka: {scrub(offer.get('deliverable') or '–', org)}",
                f"- hlavná otázka: {scrub((card.get('main_question') or {}).get('text') or '–', org)}",
                "- pripravenosť: " + "; ".join(
                    f"{lab} {'áno' if (ready.get(k) or {}).get('pripravené') else 'nie'}"
                    for k, lab in (("rozhovor", "rozhovor"), ("skúška", "skúška"), ("prínos", "tvrdenie o prínose"))),
                f"- ďalší krok: {scrub(ready.get('ďalší_krok') or '–', org)}", "", "| položka | stav |", "|---|---|"]
        out += [f"| {e['položka']} | {e['stav']} |" for e in json.loads(row["evaluation_json"] or "[]")]
        checks = json.loads(row["checks_json"] or "{}")
        tasks = db.research_tasks_for(row["id"])
        out += ["", f"Kontroly: {len(checks.get('problémy') or [])} výhrad kódu, {len(checks.get('zoslabené') or [])} "
                    f"zoslabených výrokov; hodnotiteľ opora "
                    f"{((checks.get('hodnotiteľ') or {}).get('support') or {}).get('verdict', '-')}, použiteľnosť "
                    f"{((checks.get('hodnotiteľ') or {}).get('usability') or {}).get('verdict', '-')}.", ""]
        if tasks:
            out += ["| medzera | zmení | zdroj | stav |", "|---|---|---|---|"]
            out += [f"| {scrub(t_['question'], org)} | {DECISIONS.get(t_['decision_changed'])} | "
                    f"{SOURCES.get(t_['best_source'])} | {RESEARCH_STATUS.get(t_['status'])} |" for t_ in tasks]
            out.append("")
    return "\n".join(out) + "\n"
