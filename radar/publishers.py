"""Typ vydavateľa zdroja: úrad, médiá, výskum, združenie, dodávateľ, iné (krok `source-types`).

Najprv pravidlá podľa domény (gov.sk, europa.eu, známe médiá, univerzity…), neznámu doménu raz posúdi
rýchly model (titulok a začiatok jedného dokumentu) a výsledok platí pre celú doménu. Dodávateľ = firma,
ktorá opisuje alebo predáva vlastný produkt či službu (aj jej blog): jej citáty sa do sily dôkazov
nepočítajú (marketing nie je nezávislý dôkaz problému), v GUI ostanú viditeľné.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel

from .config import Config
from .db import DB
from .models import as_router
from .prompts import SOURCE_TYPE_SYSTEM
from .util import Deadline

log = logging.getLogger(__name__)

SourceType = Literal["government", "media", "research", "association", "vendor", "other"]
TYPE_LABELS = {"government": "úrad", "media": "médiá", "research": "výskum", "association": "združenie / NGO",
               "vendor": "dodávateľ", "other": "iné"}

GOVERNMENT_SUFFIXES = (".gov.sk", ".gov", ".gov.cz", ".europa.eu", ".gv.at", ".gov.uk", ".gob.es")
GOVERNMENT_DOMAINS = {
    "statistics.sk", "slov-lex.sk", "nku.gov.sk", "uvo.gov.sk", "crz.gov.sk", "slovensko.sk", "nbs.sk", "mfsr.sk",
    "minv.sk", "employment.gov.sk", "itms2014.sk", "opendata.itms2014.sk", "itms21.sk", "nrsr.sk", "vlada.gov.sk",
    "eurofondy.gov.sk", "partnerskadohoda.gov.sk", "registeruz.sk", "financnasprava.sk", "socpoist.sk", "udzs-sk.sk",
    "oecd.org", "ec.europa.eu", "eur-lex.europa.eu", "who.int", "worldbank.org", "imf.org",
}
MEDIA_DOMAINS = {
    "sme.sk", "dennikn.sk", "aktuality.sk", "pravda.sk", "hnonline.sk", "trend.sk", "etrend.sk", "teraz.sk", "tasr.sk",
    "startitup.sk", "forbes.sk", "ta3.com", "rtvs.sk", "stvr.sk", "noviny.sk", "cas.sk", "topky.sk", "dnes24.sk",
    "euractiv.sk", "finweb.hnonline.sk", "korzar.sme.sk", "mynet.sk", "idnes.cz", "seznamzpravy.cz", "ihned.cz",
    "novinky.cz", "denik.cz", "aktualne.cz", "irozhlas.cz", "e15.cz", "reuters.com", "bloomberg.com", "ft.com",
    "theguardian.com", "bbc.com", "zive.sk", "touchit.sk", "efocus.sk", "webnoviny.sk", "dobrenoviny.sk", "plus7dni.sk",
}
RESEARCH_DOMAINS = {
    "sav.sk", "uniba.sk", "stuba.sk", "tuke.sk", "euba.sk", "uniza.sk", "ukf.sk", "umb.sk", "upjs.sk", "tuzvo.sk",
    "uniag.sk", "ineko.sk", "iness.sk", "ivo.sk", "sguide.sk", "researchgate.net", "sciencedirect.com", "mdpi.com",
    "springer.com", "arxiv.org", "nber.org", "cuni.cz", "muni.cz", "vse.cz", "vutbr.cz",
}
# Dodávatelia softvéru a služieb (ich web opisuje vlastný produkt): pravidlo má prednosť pred odhadom modelu
# (zadanie 17. 10. 2026: kros.sk model označil ako „združenie“). Citáty ostávajú dostupné pri existujúcich riešeniach.
VENDOR_DOMAINS = {
    "kros.sk", "docspoint.cz", "docspoint.sk", "money.sk", "moneyerp.com", "stormware.sk", "stormware.cz", "pohoda.sk",
    "superfaktura.sk", "idoklad.cz", "fakturoid.cz", "abra.eu", "flowii.com", "omegasoft.sk", "kros.cz", "dativery.com",
    "rossum.ai", "seyfor.com", "sap.com", "microsoft.com", "oracle.com", "salesforce.com",
}
ASSOCIATION_DOMAINS = {
    "sopk.sk", "zmos.sk", "unia-miest.sk", "rlzsr.sk", "azzz.sk", "kozsr.sk", "zps.sk", "sario.sk", "transparency.sk",
    "slovensko.digital", "sbagency.sk", "npz.sk", "kpbs.sk", "ksk.sk",
}


def rule_type(domain: str) -> str | None:
    d = (domain or "").lower().removeprefix("www.")
    parts = d.split(".")
    parents = {".".join(parts[i:]) for i in range(len(parts) - 1)}
    if d.endswith(GOVERNMENT_SUFFIXES) or parents & GOVERNMENT_DOMAINS:
        return "government"
    if parents & MEDIA_DOMAINS:
        return "media"
    if d.endswith((".edu", ".ac.uk", ".ac.at")) or parents & RESEARCH_DOMAINS:
        return "research"
    if parents & VENDOR_DOMAINS:
        return "vendor"
    if parents & ASSOCIATION_DOMAINS:
        return "association"
    if d.endswith((".wikipedia.org", "wikipedia.org", "youtube.com", "reddit.com")):
        return "other"
    return None


class SourceTypeVerdict(BaseModel):
    source_type: SourceType
    reason: str = ""


def run_source_types(cfg: Config, db: DB, llm: Any, deadline: Deadline, limit: int = 120) -> dict:
    """Typ vydavateľa pre domény bez typu: pravidlo, inak rýchly model (raz na doménu)."""
    router = as_router(llm, cfg, db)
    stats: Counter = Counter()
    stats["from_known_domains"] = db.apply_domain_types()
    for row in db.domain_types():  # pravidlo prebije starší odhad modelu (napr. dodávateľ označený ako združenie)
        typ = rule_type(row["domain"])
        if typ and row["method"] == "model" and row["source_type"] != typ:
            db.set_domain_type(row["domain"], typ, "rule", f"pravidlo nahradilo odhad modelu ({row['source_type']})")
            stats[f"rule_override:{typ}"] += 1
    for row in db.untyped_domains(limit):
        if deadline.expired():
            break
        domain = row["domain"]
        typ = rule_type(domain)
        if typ:
            db.set_domain_type(domain, typ, "rule")
            stats[f"rule:{typ}"] += 1
            continue
        doc = db.get_document(row["doc_id"])
        res = router.run_task(
            "source_type",
            [{"role": "system", "content": SOURCE_TYPE_SYSTEM.format(lang=cfg.pipeline.output_language)},
             {"role": "user", "content": f"DOMAIN: {domain}\nTITLE: {doc['title'] or ''}\n<untrusted_document>\n"
                                         f"{(doc['text'] or '')[:1200]}\n</untrusted_document>"}],
            SourceTypeVerdict, object_type="document", object_id=row["doc_id"], max_tokens=200,
        )
        if not res.ok:
            stats["llm_failures"] += 1
            continue
        db.set_domain_type(domain, res.value.source_type, "model", res.value.reason[:200])
        stats[f"model:{res.value.source_type}"] += 1
    return dict(stats)
