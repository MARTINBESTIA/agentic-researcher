"""Hľadanie kontaktov podľa hypotézy (krok `contact-search`) a ručné pridanie kontaktu zo stránky.

Pre hypotézy zapnuté na Bránu 0 (ručne alebo automaticky po teste „podložená“, people.auto_select_validated):

1. plán (rýchly model, úloha contact_plan): koho osloviť – zákazník (má problém alebo vlastní proces), držiteľ dát
   (vzorka, schéma) a expert (zväz, výskum, watchdog) – s funkciami, vyhľadávacími dopytmi a tým, čo od nich žiadať.
   Plán sa uloží (`contact_targets`) a dá sa v GUI zmazať a navrhnúť znova.
2. hľadanie (kód): dopyty cez SearXNG → web organizácie → IČO na stránke → RPO → overená organizácia s väzbou na
   hypotézu (cieľ, dopyt, stránka výsledku). Zákazník sa pri známom filtri segmentu musí zhodovať v odvetví a type.
   Ľudí a ich kontakty z webu organizácie potom zbiera krok `people` (aj zo stránky výsledku) a `contacts`.
3. držitelia dát z Brány 1: vydavatelia zdrojov hypotézy (RPO podľa názvu, web overený cez IČO alebo názov) a kontaktné
   body datasetov z Národného katalógu (dcat:contactPoint – meno a e-mail zverejnil poskytovateľ v metadátach).
4. ďalšie kolo: keď sú dopyty vyčerpané a kontaktovateľných je menej ako people.contact_goal_per_hypothesis, model
   dostane, čo už bolo hľadané a koľko sa našlo, a navrhne nové dopyty (najviac people.contact_search_rounds kôl).

Nič sa neodosiela; e-maily sa nehádajú, obfuskované adresy sa nedekódujú, LinkedIn sa nesťahuje. Kontakt sa uloží
iba vtedy, keď je doslova v zdroji (úryvok), a po kontrole zoznamu potlačenia. Beží iba po potvrdení LIA.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel, Field

from .catalogs import NKOD_DATASET_RE, NKODClient
from .config import Config
from .contacts import extract_contacts, is_generic_email, normalize_email, org_email_ok, snippet_around, visible_text
from .db import DB
from .fetch import domain_blocked
from .models import ModelRouter, as_router
from .names import fold, looks_like_person_name, name_in_text, split_titles
from .orgs import (
    DIRECTORY_TITLE, OrgFinder, SegmentFilter, auto_select_validated, icos_in, least_recently_searched, name_in,
    name_on_page, root_url,
)
from .people import PeopleCollector
from .person_contacts import contacts_of_name
from .privacy import require_lia
from .prompts import CONTACT_PLAN_SYSTEM
from .registers import RPOClient, RUZClient
from .util import PERSONAL_RE, Deadline, utc_days_ago

log = logging.getLogger(__name__)

KINDS = ("customer", "data_holder", "expert")
KIND_LABEL = {"customer": "zákazník", "data_holder": "držiteľ dát", "expert": "expert"}
RELATION_OF = {"customer": "contact_search", "data_holder": "data_holder", "expert": "expert_org"}
LINK_OF = {"customer": "role_owner", "data_holder": "data_holder", "expert": "expert"}
# Čo žiadať, keď cieľ plánu nič konkrétne neuvádza (Brána 0: najprv rozhovor a vzorka, nie predaj).
DEFAULT_ASK = {
    "customer": "Rozhovor (20 min) o poslednom konkrétnom prípade problému: kedy sa stal, ako často, ako to riešia dnes, "
                "koľko to stojí a kto rozhoduje o nákupe.",
    "data_holder": "Schéma dát a 20 anonymizovaných riadkov; potom väčšiu vzorku výmenou za konkrétny výstup "
                   "(prehľad, nájdené výnimky, nameraná úspora).",
    "expert": "Odborné posúdenie problému, existujúce hodnotenia a odporúčanie, koho ďalej osloviť.",
}
MAX_QUERIES_PER_TARGET = 4
MAX_QUERY_CHARS = 120
CATALOG_CACHE_DAYS = 30


class TargetOut(BaseModel):
    kind: Literal["customer", "data_holder", "expert"]
    who: str
    roles: list[str] = Field(default_factory=list)
    queries: list[str] = Field(default_factory=list)
    ask: str = ""
    why: str = ""


class ContactPlan(BaseModel):
    targets: list[TargetOut] = Field(default_factory=list)


def clean_queries(cfg: Config, queries: list[str], tried: set[str]) -> list[str]:
    """Dopyty od modelu: bez e-mailov a telefónov, bez sociálnych sietí, bez opakovania a s rozumnou dĺžkou."""
    out: list[str] = []
    for q in queries:
        q = " ".join(str(q).split())[:MAX_QUERY_CHARS]
        low = q.lower()
        if len(q) < 4 or PERSONAL_RE.search(q) or fold(q) in tried or fold(q) in {fold(x) for x in out}:
            continue
        if any(d in low for d in cfg.blocked_domains()):
            continue
        out.append(q)
    return out[:MAX_QUERIES_PER_TARGET]


def target_queries(target) -> list[dict[str, Any]]:
    try:
        return list(json.loads(target["queries_json"] or "[]"))
    except ValueError:
        return []


def target_roles(target) -> list[str]:
    try:
        return [str(r) for r in json.loads(target["roles_json"] or "[]")]
    except ValueError:
        return []


def target_ask(target, kind: str) -> str:
    return (target["ask"] if target is not None and target["ask"] else "") or DEFAULT_ASK.get(kind, "")


def contactable(db: DB, hid: int) -> int:
    """Unikátne použiteľné kontaktné kanály hypotézy: e-maily ľudí s väzbou (vlastný alebo organizácie) a organizácie
    s e-mailom bez takého človeka. Spoločný e-mail sa ráta raz; odmietnutí a vylúčení sa nerátajú (audit 15. 10. 2026)."""
    channels: set[str] = set()
    people_orgs: set[int] = set()
    seen: set[int] = set()
    for link in db.links_for_hypothesis(hid):
        if link["person_id"] in seen or link["priority"] is None or link["person_status"] in ("excluded", "declined"):
            continue
        seen.add(link["person_id"])
        emails = [c["value"].lower() for c in db.contacts_for_person(link["person_id"]) if c["kind"] == "email"]
        if not emails and link["org_id"]:
            emails = [c["value"].lower() for c in db.org_contacts(link["org_id"]) if c["kind"] == "email"]
        if emails:
            channels.add(emails[0])
            if link["org_id"]:
                people_orgs.add(link["org_id"])
    for org in db.orgs_for_hypothesis(hid):
        if org["id"] not in people_orgs:
            emails = [c["value"].lower() for c in db.org_contacts(org["id"]) if c["kind"] == "email"]
            if emails:
                channels.add(emails[0])
    return len(channels)


def search_due(cfg: Config, db: DB, hid: int) -> str | None:
    """Prečo má hypotéza prácu na hľadanie kontaktov (None = nemá)."""
    if not cfg.people.contact_search:
        return None
    h = db.get_hypothesis(hid)
    if h is None:
        return None
    targets = db.contact_targets_for(hid)
    if not targets:
        return "bez plánu kontaktov"
    if any(not q.get("done_at") for t in targets for q in target_queries(t)):
        return "nevyhľadané dopyty"
    rounds = max(t["round"] for t in targets)
    # Ďalšie kolo až keď krok people spracoval organizácie z predošlého (inak ešte nevidno, koľko kontaktov dajú).
    if (rounds < cfg.people.contact_search_rounds and not db.orgs_waiting_for_people(hid)
            and contactable(db, hid) < cfg.people.contact_goal_per_hypothesis):
        return "málo kontaktov – ďalšie kolo dopytov"
    return None


class ContactScout:
    def __init__(self, cfg: Config, db: DB, router: ModelRouter | None, searx, fetcher, deadline: Deadline,
                 rpo: RPOClient | None = None, ruz: RUZClient | None = None, catalog: NKODClient | None = None):
        self.cfg, self.db, self.router, self.searx, self.fetcher, self.deadline = cfg, db, router, searx, fetcher, deadline
        self.collector = PeopleCollector(cfg, db, router, searx, fetcher, deadline, rpo, ruz)
        self.finder: OrgFinder = self.collector.orgs
        self.catalog = catalog
        self.stats: Counter = Counter()

    # ------------------------------------------------------------------ plán
    def plan_messages(self, hyp, feedback: str = "") -> list[dict[str, str]]:
        hid = hyp["id"]
        evidence = [e for e in self.db.evidence_for(hid) if e["status"] == "verified"][:6]
        ev = "\n".join(f"- {e['problem']} (who: {e['affected_party']}; quote: {e['quote'][:200]})" for e in evidence)
        parts = [f"PROBLEM: {hyp['title']}", f"SEGMENT: {hyp['segment'] or '?'}", f"DESCRIPTION: {hyp['description'] or ''}"]
        role_map = self.db.get_role_map(hid)
        if role_map and role_map["roles"]:
            parts.append("ROLES THAT OWN THE PROCESS: " + "; ".join(r["category"] for r in role_map["roles"]))
        card = self.db.latest_card(hid)
        if card is not None:
            try:
                data = json.loads(card["card_json"] or "{}")
            except ValueError:
                data = {}
            for key in ("who", "decision", "current_practice", "helpful_output_text"):
                value = data.get(key)
                if isinstance(value, dict):
                    value = value.get("text") or value.get("value")
                if value:
                    parts.append(f"PROBLEM CARD {key.upper()}: {str(value)[:200]}")
        fields = self.db.fields_for(hid)
        if fields:
            parts.append("DATA NEEDS: " + "; ".join(f"{f['name']}{' (key)' if f['is_key'] else ''}" for f in fields[:8]))
        publishers = sorted({s["publisher"] for s in self.db.sources_for(hid) if s["publisher"] and s["status"] != "rejected"})
        if publishers:
            parts.append("DATA SOURCES (publishers): " + "; ".join(publishers[:6]))
        if feedback:
            parts.append(feedback)
        user = "\n".join(parts) + f"\n\nEVIDENCE:\n<untrusted_document>\n{ev or '(none)'}\n</untrusted_document>"
        return [{"role": "system", "content": CONTACT_PLAN_SYSTEM.format(lang=self.cfg.pipeline.output_language)},
                {"role": "user", "content": user}]

    def plan(self, hyp, round_: int, feedback: str = "") -> int:
        """Ciele plánu z rýchleho modelu (kód vyčistí dopyty); vráti počet nových cieľov."""
        if self.router is None:
            return 0
        res = self.router.run_task("contact_plan", self.plan_messages(hyp, feedback), ContactPlan,
                                   object_type="hypothesis", object_id=hyp["id"], max_tokens=1600)
        if not res.ok:
            self.stats["llm_failures"] += 1
            return 0
        tried = {fold(q["q"]) for t in self.db.contact_targets_for(hyp["id"]) for q in target_queries(t)}
        added = 0
        for t in res.value.targets[:6]:
            who = " ".join(t.who.split())[:160]
            queries = clean_queries(self.cfg, t.queries, tried)
            if not who or not queries:
                self.stats["targets_without_queries"] += 1
                continue
            tried |= {fold(q) for q in queries}
            roles = [" ".join(r.split())[:80] for r in t.roles if r.strip()][:5]
            self.db.add_contact_target(hyp["id"], t.kind, who, roles, queries, " ".join(t.ask.split())[:300],
                                       " ".join(t.why.split())[:300], round_)
            added += 1
        self.stats["targets"] += added
        return added

    def feedback(self, hyp) -> str:
        targets = self.db.contact_targets_for(hyp["id"])
        tried = [q["q"] for t in targets for q in target_queries(t)]
        rel = self.db.org_hypotheses_by_relation(hyp["id"])
        found = {k: rel.get(RELATION_OF[k], 0) for k in KINDS}
        return ("ALREADY TRIED: " + "; ".join(tried[:30]) + "\nFOUND ORGANIZATIONS: "
                + ", ".join(f"{k} {n}" for k, n in found.items())
                + f"\nCONTACTABLE PEOPLE OR ORGANIZATIONS: {contactable(self.db, hyp['id'])}")

    # --------------------------------------------------------------- hľadanie
    def search(self, hyp, budget: int) -> int:
        """Nevyhľadané dopyty po rade naprieč typmi cieľov (zákazník, držiteľ dát, expert); vráti počet dopytov."""
        targets = self.db.contact_targets_for(hyp["id"])
        queue: list[tuple[int, int, Any, int]] = []
        for t in targets:
            for i, q in enumerate(target_queries(t)):
                if not q.get("done_at"):
                    queue.append((i, KINDS.index(t["kind"]) if t["kind"] in KINDS else 9, t, i))
        queue.sort(key=lambda x: (x[0], x[1], x[2]["id"]))
        done = 0
        for _, _, target, idx in queue:
            if done >= budget or self.deadline.expired():
                break
            target = self.db.get_contact_target(target["id"])  # aktuálne dopyty (zápis po každom)
            queries = target_queries(target)
            q = queries[idx]["q"]
            try:
                results = self.searx.search(q, limit=8)
            except Exception as exc:  # noqa: BLE001 – vyhľadávač nedostupný: dopyt ostane na ďalší beh
                log.warning("Hľadanie kontaktov '%s' zlyhalo: %s", q, exc)
                self.stats["search_errors"] += 1
                continue
            linked = 0
            for r in results:
                if self.deadline.expired() or self.finder._full(hyp["id"]):
                    break
                try:
                    linked += int(self.result(hyp, target, q, r))
                except Exception as exc:  # noqa: BLE001 – jedna stránka alebo odpoveď registra nezastaví beh
                    log.warning("Výsledok %s preskočený: %s", r.url, exc)
                    self.stats["errors"] += 1
            queries[idx].update(done_at=_now(), results=len(results), orgs=linked)
            self.db.update_contact_target(target["id"], queries_json=queries)
            self.stats["queries"] += 1
            done += 1
        return done

    def result(self, hyp, target, query: str, r) -> bool:
        if DIRECTORY_TITLE.search(r.title or ""):
            self.stats["directory"] += 1  # katalóg firiem alebo rebríček, nie web organizácie
            return False
        kind = target["kind"]
        allow = self.cfg.people.expert_org_domains if kind in ("data_holder", "expert") else None
        org_id = self.finder.org_from_result(r.url, "contact_search", allow_domains=allow)
        if org_id is None:
            return False
        if self.db.org_hypothesis(org_id, hyp["id"]) is not None:
            self.stats["already_linked"] += 1
            return False
        fit: dict[str, Any] = {"typ cieľa": KIND_LABEL[kind], "cieľ": target["who"], "dopyt": query}
        if kind == "customer" and hyp["segment_filter_json"]:
            comp = self.finder.fit_components(self.db.get_organization(org_id),
                                              SegmentFilter.model_validate_json(hyp["segment_filter_json"]))
            if "nezhoda" in (comp["odvetvie"], comp["typ"]):
                self.stats["segment_mismatch"] += 1  # napr. dodávateľ softvéru namiesto zákazníka
                return False
            fit.update({k: v for k, v in comp.items() if k != "dôkaz o probléme"})
        self.db.link_org_hypothesis(org_id, hyp["id"], RELATION_OF[kind], None, r.url, fit=fit, target_id=target["id"])
        self.stats[f"orgs_{kind}"] += 1
        return True

    # ------------------------------------------------------- držitelia dát (Brána 1)
    def data_holders(self, hyp) -> None:
        hid = hyp["id"]
        seen: set[str] = set()
        for src in self.db.sources_for(hid):
            if self.deadline.expired():
                return
            if src["status"] == "rejected":
                continue
            org_id = None
            publisher = " ".join((src["publisher"] or "").split())
            if publisher and fold(publisher) not in seen:
                seen.add(fold(publisher))
                org_id = self.publisher_org(hid, publisher, src)
            if self.catalog is not None and NKOD_DATASET_RE.match(src["url"] or ""):
                self.catalog_contacts(hid, src, org_id)

    def publisher_org(self, hid: int, publisher: str, src) -> int | None:
        found = self.db.find_organization(name_key=_org_key(publisher))
        if found is not None and self.db.org_hypothesis(found["id"], hid) is not None:
            return int(found["id"])
        info = self.finder.resolve_name(publisher)
        org_id = self.finder.store(publisher, "gate1_source", info, source_url=src["url"])
        if org_id is None:
            return None
        if self.db.org_hypothesis(org_id, hid) is None:
            self.db.link_org_hypothesis(org_id, hid, "data_holder", None, src["url"],
                                        fit={"typ cieľa": KIND_LABEL["data_holder"], "zdroj dát": src["name"],
                                             "pole": src["field_name"]})
            self.stats["orgs_data_holder"] += 1
            # Web overí (IČO alebo presný názov na stránke) iba pri novej väzbe – inak by sa pri každom behu hľadal znova.
            if not self.db.get_organization(org_id)["website_verified"] and not self.deadline.expired():
                self.finder.verify_website(org_id)
        return org_id

    def catalog_contacts(self, hid: int, src, org_id: int | None) -> None:
        """Kontaktný bod datasetu z Národného katalógu (úradné zverejnenie). Meno a e-mail sú doslova v úryvku."""
        key = f"nkod-contact:{src['url']}"
        cached = self.db.cache_get(key, CATALOG_CACHE_DAYS)
        points = json.loads(cached) if cached is not None else self.catalog.contact_points(src["url"])
        if cached is None and not self.catalog.failed:
            self.db.cache_put(key, json.dumps(points, ensure_ascii=False))
        for cp in points[:3]:
            email = normalize_email(cp.get("email") or "")
            fn = " ".join((cp.get("fn") or "").split())
            if not email:
                continue  # bez e-mailu nie je čo uložiť (meno bez kontaktu ani funkcie sa neukladá)
            title = (cp.get("title") or src["name"] or "")[:120]
            publisher = (cp.get("publisher") or src["publisher"] or "")[:100]
            snippet = f"Národný katalóg otvorených dát – kontaktný bod datasetu „{title}“ ({publisher}): {fn}, {email}"
            checks = {"úradné_zverejnenie": "Národný katalóg (dcat:contactPoint)",
                      "metadáta_zmenené": (cp.get("modified") or "")[:10] or "neuvedené"}
            name, titles = split_titles(fn)
            if fn and looks_like_person_name(name) and not is_generic_email(email):
                pid = self.collector.save_person(hid, org_id, name, titles, "kontaktný bod datasetu", src["url"], snippet,
                                                 "catalog")
                if pid is None:
                    continue
                self.collector.save_contact(pid, org_id, "email", email, src["url"], snippet, "catalog_contact_point",
                                            extra_checks=checks)
                self.db.upsert_link(pid, hid, "data_holder", None, src["url"],
                                    rationale=f"Kontaktný bod datasetu „{title}“ v Národnom katalógu.")
                self.stats["catalog_people"] += 1
            elif org_id is not None:
                self.collector.save_contact(None, org_id, "email", email, src["url"], snippet, "catalog_contact_point",
                                            generic=True, extra_checks=checks)
                self.stats["catalog_org_contacts"] += 1

    # ------------------------------------------------------------------- beh
    def run_hypothesis(self, hyp) -> None:
        hid = hyp["id"]
        budget = self.cfg.people.contact_queries_per_run
        if not self.db.contact_targets_for(hid):
            self.plan(hyp, 1)
        try:
            self.data_holders(hyp)
        except Exception:  # noqa: BLE001 – zdroje dát nezastavia hľadanie podľa plánu
            log.exception("Držitelia dát pre H%s zlyhali", hid)
            self.stats["errors"] += 1
        budget -= self.search(hyp, budget)
        targets = self.db.contact_targets_for(hid)
        pending = any(not q.get("done_at") for t in targets for q in target_queries(t))
        if not pending and targets and search_due(self.cfg, self.db, hid) and budget > 0 and not self.deadline.expired():
            round_ = max(t["round"] for t in targets) + 1
            if self.plan(hyp, round_, self.feedback(hyp)):
                self.stats["refined"] += 1
                self.search(hyp, budget)
            targets = self.db.contact_targets_for(hid)
            pending = any(not q.get("done_at") for t in targets for q in target_queries(t))
        if not pending and targets:
            rel = self.db.org_hypotheses_by_relation(hid)
            self.db.set_contacts_searched(hid, {
                "kolá": max(t["round"] for t in targets), "ciele": len(targets),
                "dopyty": sum(len(target_queries(t)) for t in targets),
                "organizácie": {KIND_LABEL[k]: rel.get(RELATION_OF[k], 0) for k in KINDS},
                "kontaktovateľní": contactable(self.db, hid),
            })
            self.stats["hypotheses_done"] += 1


def _now() -> str:
    from .util import utcnow

    return utcnow()


def _org_key(name: str) -> str:
    from .names import org_name_key

    return org_name_key(name)


def run_contact_search(cfg: Config, db: DB, llm: Any, searx, fetcher, deadline: Deadline,
                       hypothesis_ids: list[int] | None = None, rpo: RPOClient | None = None,
                       ruz: RUZClient | None = None, catalog: NKODClient | None = None) -> dict:
    """Plán kontaktov a hľadanie podľa neho pre hypotézy na Bráne 0 (najprv najdlhšie neprehľadané)."""
    require_lia(cfg)
    if not cfg.people.contact_search:
        return {"skipped": "people.contact_search: false"}
    router = as_router(llm, cfg, db)
    if hypothesis_ids is None:
        auto_select_validated(cfg, db)
    if rpo is None:
        rpo = RPOClient(db, cfg.people.registry_delay_s, cfg.people.registry_cache_days, cfg.fetch.user_agent)
    if ruz is None:
        ruz = RUZClient(db, cfg.people.registry_delay_s, cfg.people.registry_cache_days, cfg.fetch.user_agent)
    if catalog is None and cfg.gate1.nkod_enabled:
        catalog = NKODClient(cfg.gate1.nkod_sparql)
    ids = least_recently_searched(db, hypothesis_ids or [h["id"] for h in db.automation_hypotheses()])
    scout = ContactScout(cfg, db, router, searx, fetcher, deadline, rpo, ruz, catalog)
    for hid in ids:
        if deadline.expired():
            break
        hyp = db.get_hypothesis(hid)
        if hyp is None:
            continue
        fresh = (hyp["contacts_searched_at"] or "") >= utc_days_ago(cfg.people.contact_research_days)
        if fresh and not search_due(cfg, db, hid):
            continue  # nič nové (držitelia dát z Brány 1 sa doplnia pri ďalšom hľadaní po contact_research_days)
        try:
            scout.run_hypothesis(hyp)
        except Exception:  # noqa: BLE001 – chyba jednej hypotézy nezastaví ostatné
            log.exception("Hľadanie kontaktov pre H%s zlyhalo", hid)
            scout.stats["errors"] += 1
        scout.stats["hypotheses"] += 1
    return dict(scout.stats + scout.collector.stats)


# =================================================================== ručne zo stránky
def add_contact_from_url(cfg: Config, db: DB, fetcher, hid: int, url: str, name: str = "", kind: str = "customer",
                         role: str = "", org_name: str = "", ask: str = "", rpo: RPOClient | None = None) -> dict:
    """Človek zadá verejnú stránku organizácie (a meno). Kód stránku stiahne (robots.txt, blokované domény platia),
    nájde meno a kontakty v tom istom bloku (rovnaké pravidlá ako pri zbere) a uloží ich so zdrojom a úryvkom.
    Bez mena uloží generické kontakty stránky k organizácii. Čo na stránke nie je, sa neuloží – ani e-mail."""
    require_lia(cfg)
    if kind not in KINDS:
        raise ValueError(f"Neznámy typ kontaktu {kind!r} (zákazník, držiteľ dát, expert).")
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ValueError("Zadaj celú adresu stránky (https://…).")
    if domain_blocked(url, cfg.blocked_domains()):
        raise ValueError("Sociálne siete a LinkedIn sa nesťahujú. Zadaj stránku organizácie (kontakty, tím, odbor).")
    if db.get_hypothesis(hid) is None:
        raise ValueError(f"Hypotéza H{hid} neexistuje.")
    page = fetcher.fetch_html(url)
    if page.status != "ok":
        raise ValueError(f"Stránku sa nepodarilo stiahnuť ({page.status}).")
    final = page.final_url or url
    contacts = extract_contacts(page.html, final)
    text = visible_text(page.html)
    deadline = Deadline(5)
    collector = PeopleCollector(cfg, db, None, None, fetcher, deadline, rpo)
    finder = collector.orgs
    org_id = None
    icos = icos_in(text)
    info = finder.lookup_ico(icos[0]) if icos else None
    if info is not None and not info.terminated and name_on_page(info.name, text):
        org_id = finder.store(info.name, "manual", info, source_url=final, website=root_url(final),
                              verified_evidence=f"IČO {icos[0]} na {final}")
    elif org_name.strip():
        on_page = name_in(org_name, text)
        resolved = finder.resolve_name(org_name) if rpo is not None else None
        org_id = finder.store(org_name.strip(), "manual", resolved, source_url=final,
                              website=root_url(final) if on_page else None,
                              verified_evidence=f"presný názov na {final}" if on_page else None)
    else:
        raise ValueError("Na stránke nie je IČO organizácie – doplň názov organizácie.")
    if org_id is None:
        raise ValueError("Organizácia je vylúčená (outreach.exclude_orgs) – nič sa neuložilo.")
    if db.org_hypothesis(org_id, hid) is None:
        db.link_org_hypothesis(org_id, hid, RELATION_OF[kind], None, final,
                               fit={"typ cieľa": KIND_LABEL[kind], "zdroj": "pridané ručne"})
    # Kontakt pridaný človekom = hypotéza je vybraná na Bránu 0 (osobné údaje iba pre vybrané hypotézy).
    db.set_people_selection(hid, True)
    out: dict[str, Any] = {"organizácia": db.get_organization(org_id)["name"], "osoba": None, "kontakty": []}
    manual = {"zadané_ručne": True}
    org = db.get_organization(org_id)
    org = dict(org) | {"website": org["website"] or final}
    generic = [(e, snip) for e, snip in contacts.emails.items()
               if org_email_ok(org, e, cfg.people.official_domains)][: cfg.people.max_org_contacts_per_kind]
    for email, snip in generic:  # info@, podateľňa… k organizácii (aj keď sa osoba píše „cez info@…“)
        collector.save_contact(None, org_id, "email", email, final, snip, "manual_page", generic=True, extra_checks=manual)
    if not name.strip():
        out["kontakty"] = [e for e, _ in generic]
        if not generic:
            raise ValueError("Na stránke nie je generický e-mail organizácie (info@, podateľňa…). Zadaj meno osoby.")
        return out
    person_name, titles = split_titles(name.strip())
    found: list[tuple[str, str, str]] = []
    snippet = ""
    for block in contacts.blocks:
        pos = name_in_text(person_name, block.text)
        if pos < 0:
            continue
        snippet = snippet or snippet_around(block.text, max(0, pos - 60), pos + len(person_name) + 200)
        found += contacts_of_name(block.text, person_name, block.emails, block.phones)
    if not snippet:
        pos = name_in_text(person_name, text)
        if pos < 0:
            raise ValueError("Meno sa na stránke nenašlo – nič sa neuložilo.")
        snippet = snippet_around(text, max(0, pos - 60), pos + len(person_name) + 200)
    if role.strip() and fold(role) not in fold(snippet) and fold(role) not in fold(text):
        role = ""  # funkcia, ktorá na stránke nie je, sa neuloží
    pid = collector.save_person(hid, org_id, person_name, titles, role.strip(), final, snippet, "manual")
    if pid is None:
        raise ValueError("Osoba je v zozname potlačenia (forget) – nič sa neuložilo.")
    for kind_, value, snip in found:
        collector.save_contact(pid, org_id, kind_, value or final, final, snip,
                               "obfuscated" if kind_ == "hidden_contact" else "manual_page", extra_checks=manual)
        out["kontakty"].append(value or "skrytý e-mail – otvor ručne")
    if not found and generic:
        out["kontakty"].append(f"organizácia: {generic[0][0]}")
    reason = " ".join(ask.split())[:300] or DEFAULT_ASK[kind]
    db.upsert_link(pid, hid, LINK_OF[kind], None, final, rationale=f"Pridané ručne. Čo žiadať: {reason}")
    out["osoba"] = person_name
    return out
