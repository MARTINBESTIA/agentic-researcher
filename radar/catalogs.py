"""Národný katalóg otvorených dát (data.slovensko.sk, NKOD) cez verejný SPARQL endpoint – zdroje pre Bránu 1
bez vyhľadávača (SearXNG pri automatických dopytoch často blokuje).

- Hľadá datasety podľa slov v názve alebo popise; vracia iba metadáta katalógu (poskytovateľ, licencia podľa
  podmienok užitia, periodicita, formáty a odkazy na stiahnutie) – nič sa nedopĺňa ani neodhaduje.
- Metadáta sa agentovi zdrojov ukážu ako prečítaná stránka (URL datasetu v katalógu), takže pri ukladaní zdroja
  platí rovnaká kontrola ako pri webe: citát, licencia a periodicita musia byť doslova v texte metadát.
- Kontrola zdroja (gate1-check) berie súbory priamo z distribúcií datasetu v katalógu.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx
import numpy as np

from .util import tls_context

log = logging.getLogger(__name__)

NKOD_SPARQL = "https://data.slovensko.sk/api/sparql"
NKOD_DATASET_RE = re.compile(r"^https://data\.gov\.sk/set/[0-9a-fA-F-]{16,}$")
LEGISLATION = "https://data.gov.sk/def/ontology/legislation/"
PREFIXES = ("PREFIX dcat: <http://www.w3.org/ns/dcat#> PREFIX dct: <http://purl.org/dc/terms/> "
            "PREFIX foaf: <http://xmlns.com/foaf/0.1/> ")
# Kódy licencií EÚ (publications.europa.eu/resource/authority/licence) → bežný zápis; neznámy kód ostane kódom.
LICENSES = {"CC_BY_4_0": "CC BY 4.0", "CC_BY": "CC BY", "CC0": "CC0 1.0", "CC_BYSA_4_0": "CC BY-SA 4.0",
            "CC_BYSA": "CC BY-SA", "CC_BYNC_4_0": "CC BY-NC 4.0", "CC_BYNCSA_4_0": "CC BY-NC-SA 4.0",
            "CC_BYND_4_0": "CC BY-ND 4.0", "CC_BYNCND_4_0": "CC BY-NC-ND 4.0", "CC_PDM_1_0": "Public Domain Mark 1.0"}
PERIODICITY = {"DAILY": "denne", "WEEKLY": "týždenne", "MONTHLY": "mesačne", "QUARTERLY": "štvrťročne",
               "ANNUAL": "ročne", "BIENNIAL": "raz za dva roky", "IRREG": "nepravidelne", "CONT": "priebežne",
               "UPDATE_CONT": "priebežne", "NEVER": "neaktualizuje sa"}
STOP = {"počet", "údaje", "údajov", "dáta", "dátum", "hodnota", "suma", "názov", "zoznam", "organizácie", "organizácia",
        "ktoré", "ktorý", "podľa", "alebo", "slovensko", "slovenskej", "republiky"}


FOLDED_STOP = {"".join(dict(zip("áäčďéíĺľňóôŕšťúýž", "aacdeillnoorstuyz")).get(c, c) for c in w) for w in STOP}


@dataclass
class Distribution:
    title: str
    format: str  # CSV, XLSX, JSON… (kód EÚ)
    url: str  # downloadURL, inak accessURL
    license: str = ""  # „CC BY 4.0 (CC_BY_4_0)“


@dataclass
class CatalogDataset:
    uri: str
    title: str
    description: str = ""
    publisher: str = ""
    modified: str = ""
    periodicity: str = ""  # „ročne (ANNUAL)“
    landing_page: str = ""
    distributions: list[Distribution] = field(default_factory=list)
    more_distributions: int = 0

    @property
    def formats(self) -> list[str]:
        return sorted({d.format for d in self.distributions if d.format})

    def text(self) -> str:
        """Metadáta ako „prečítaná stránka“ pre agenta (iba hodnoty z katalógu)."""
        lines = ["Národný katalóg otvorených dát (data.slovensko.sk) – metadáta datasetu",
                 f"Názov: {self.title}", f"Poskytovateľ: {self.publisher or '(neuvedený)'}"]
        if self.description:
            lines.append(f"Popis: {self.description[:800]}")
        if self.modified:
            lines.append(f"Posledná zmena: {self.modified[:10]}")
        if self.periodicity:
            lines.append(f"Periodicita aktualizácie: {self.periodicity}")
        licenses = sorted({d.license for d in self.distributions if d.license})
        if licenses:
            lines.append("Licencia (podmienky užitia, autorské dielo): " + "; ".join(licenses))
        lines.append("Distribúcie (súbory na stiahnutie):")
        lines += [f"- {d.title or 'súbor'}: formát {d.format or '?'}, {d.url}" for d in self.distributions]
        if self.more_distributions:
            lines.append(f"- … a ďalších {self.more_distributions} distribúcií")
        if self.landing_page:
            lines.append(f"Stránka datasetu: {self.landing_page}")
        lines.append(f"Identifikátor v katalógu: {self.uri}")
        return "\n".join(lines)


def _code(uri: str) -> str:
    return (uri or "").rstrip("/").rsplit("/", 1)[-1]


def license_label(uri: str) -> str:
    code = _code(uri)
    if not code:
        return ""
    return f"{LICENSES[code]} ({code})" if code in LICENSES else code


def periodicity_label(uri: str) -> str:
    code = _code(uri)
    return f"{PERIODICITY[code]} ({code})" if code in PERIODICITY else ""


def search_terms(text: str, max_terms: int = 3) -> list[str]:
    """Slová na hľadanie v názvoch datasetov: dlhšie slová bez bežných výrazov, skrátené na kmeň (bez poslednej
    hlásky, najviac 6 znakov), aby sa našli aj iné pády („lieky“ → „liek“ ~ „liekov“, „výdavky“ → „výdavk“)."""
    words = [w.lower() for w in re.findall(r"[\wáäčďéíĺľňóôŕšťúýž]{4,}", text or "", re.I)]
    out: list[str] = []
    for w in words:
        if w in STOP or fold(w) in FOLDED_STOP or w.isdigit():
            continue
        w = fold(w)
        stem = w[: max(4, min(6, len(w) - 1))]
        if stem not in out:
            out.append(stem)
    return out[:max_terms]


FOLD = dict(zip("áäčďéíĺľňóôŕšťúýž", "aacdeillnoorstuyz"))


def fold(text: str) -> str:
    """Bez diakritiky (pády menia dĺžne: „zmluvy“ ~ „zmlúv“)."""
    return "".join(FOLD.get(ch, ch) for ch in (text or "").lower())


def _fold_sql(expr: str) -> str:
    for a, b in FOLD.items():
        expr = f'REPLACE({expr}, "{a}", "{b}")'
    return expr


def _esc(term: str) -> str:
    return re.sub(r"[^a-z0-9 -]", "", fold(term))


class NKODClient:
    def __init__(self, endpoint: str = NKOD_SPARQL, http: httpx.Client | None = None, max_distributions: int = 8):
        self.endpoint = endpoint
        self.http = http or httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0), verify=tls_context(),
                                         headers={"User-Agent": "ProblemRadar/0.2"})
        self.max_distributions = max_distributions
        self.failed = False  # po chybe sa v tomto behu katalóg už nevolá (beh pokračuje vyhľadávačom)

    def _select(self, query: str) -> list[dict[str, Any]]:
        if self.failed:
            return []
        try:
            resp = self.http.get(self.endpoint, params={"query": PREFIXES + query},
                                 headers={"Accept": "application/sparql-results+json"})
            resp.raise_for_status()
            rows = resp.json()["results"]["bindings"]
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            log.warning("Národný katalóg nedostupný: %s", exc)
            self.failed = True
            return []
        return [{k: v.get("value", "") for k, v in row.items()} for row in rows]

    def search(self, text: str, limit: int = 5) -> list[CatalogDataset]:
        """Datasety, ktorých názov alebo popis obsahuje všetky hľadané slová (najnovšie zmenené najskôr)."""
        terms = [t for t in (_esc(t) for t in search_terms(text)) if t]
        # Všetky slová naraz bývajú príliš prísne (názvy datasetov sú krátke): postupne s menej slovami, ale pri
        # viacslovnom dopyte aspoň dve (jedno slovo by vrátilo stovky nesúvisiacich datasetov).
        for n in range(len(terms), min(2, len(terms)) - 1, -1):
            found = self._search_terms(terms[:n], limit)
            if found or self.failed:
                return found
        return []

    def _search_terms(self, terms: list[str], limit: int) -> list[CatalogDataset]:
        cond = " && ".join(f'CONTAINS(?hay, "{t}")' for t in terms)
        rows = self._select(f"""SELECT ?d (SAMPLE(?t) AS ?title) (SAMPLE(?ds) AS ?desc) (SAMPLE(?pn) AS ?publisher)
            (MAX(?m) AS ?modified) (SAMPLE(?per) AS ?periodicity) (SAMPLE(?lp) AS ?landing) WHERE {{
              ?d a dcat:Dataset ; dct:title ?t .
              OPTIONAL {{ ?d dct:description ?ds }}
              BIND({_fold_sql('LCASE(CONCAT(STR(?t), " ", COALESCE(STR(?ds), "")))')} AS ?hay)
              FILTER({cond})
              OPTIONAL {{ ?d dct:publisher ?p . ?p foaf:name ?pn }}
              OPTIONAL {{ ?d dct:modified ?m }}
              OPTIONAL {{ ?d dct:accrualPeriodicity ?per }}
              OPTIONAL {{ ?d dcat:landingPage ?lp }}
            }} GROUP BY ?d ORDER BY DESC(?modified) LIMIT {int(limit)}""")
        datasets = [CatalogDataset(uri=r["d"], title=r.get("title", ""), description=r.get("desc", ""),
                                   publisher=r.get("publisher", ""), modified=r.get("modified", ""),
                                   periodicity=periodicity_label(r.get("periodicity", "")), landing_page=r.get("landing", ""))
                    for r in rows if r.get("d")]
        for ds in datasets:
            self.fill_distributions(ds)
        return datasets

    def fill_distributions(self, ds: CatalogDataset) -> CatalogDataset:
        rows = self._select(f"""SELECT ?dist (SAMPLE(?dt) AS ?title) (SAMPLE(?f) AS ?format) (SAMPLE(?dl) AS ?download)
            (SAMPLE(?ac) AS ?access) (SAMPLE(?lic) AS ?license) WHERE {{
              <{ds.uri}> dcat:distribution ?dist .
              OPTIONAL {{ ?dist dct:title ?dt }} OPTIONAL {{ ?dist dct:format ?f }}
              OPTIONAL {{ ?dist dcat:downloadURL ?dl }} OPTIONAL {{ ?dist dcat:accessURL ?ac }}
              OPTIONAL {{ ?dist <{LEGISLATION}termsOfUse> ?tou . ?tou <{LEGISLATION}authorsWorkType> ?lic }}
            }} GROUP BY ?dist""")
        dists = [Distribution(title=r.get("title", ""), format=_code(r.get("format", "")),
                              url=r.get("download") or r.get("access", ""), license=license_label(r.get("license", "")))
                 for r in rows if r.get("download") or r.get("access")]
        ds.distributions = dists[: self.max_distributions]
        ds.more_distributions = max(0, len(dists) - self.max_distributions)
        return ds

    def dataset(self, uri: str) -> CatalogDataset | None:
        """Dataset podľa identifikátora v katalógu (pri kontrole zdroja)."""
        if not NKOD_DATASET_RE.match(uri or ""):
            return None
        rows = self._select(f"""SELECT (SAMPLE(?t) AS ?title) (SAMPLE(?pn) AS ?publisher) (SAMPLE(?per) AS ?periodicity) WHERE {{
              <{uri}> dct:title ?t . OPTIONAL {{ <{uri}> dct:publisher ?p . ?p foaf:name ?pn }}
              OPTIONAL {{ <{uri}> dct:accrualPeriodicity ?per }} }}""")
        if not rows or not rows[0].get("title"):
            return None
        ds = CatalogDataset(uri=uri, title=rows[0]["title"], publisher=rows[0].get("publisher", ""),
                            periodicity=periodicity_label(rows[0].get("periodicity", "")))
        return self.fill_distributions(ds)


# ======================================================================= lokálny index (v9)
INDEX_KIND = "catalog"


def index_text(row) -> str:
    """Text na embedding: názov, začiatok popisu a vydavateľ."""
    return f"{row['title'] or ''}. {(row['description'] or '')[:300]} ({row['publisher'] or ''})"


def fetch_all(client: NKODClient, page: int = 1000, max_pages: int = 100,
              progress: Callable[[str], None] | None = None) -> list[dict[str, Any]]:
    """Metadáta všetkých datasetov katalógu po stranách (bez distribúcií – tie sa doplnia až pri výbere)."""
    out: list[dict[str, Any]] = []
    for i in range(max_pages):
        rows = client._select(f"""SELECT ?d (SAMPLE(?t) AS ?title) (SAMPLE(?ds) AS ?desc) (SAMPLE(?pn) AS ?publisher)
            (MAX(?m) AS ?modified) (SAMPLE(?per) AS ?periodicity) (SAMPLE(?lp) AS ?landing) WHERE {{
              ?d a dcat:Dataset ; dct:title ?t .
              OPTIONAL {{ ?d dct:description ?ds }} OPTIONAL {{ ?d dct:publisher ?p . ?p foaf:name ?pn }}
              OPTIONAL {{ ?d dct:modified ?m }} OPTIONAL {{ ?d dct:accrualPeriodicity ?per }}
              OPTIONAL {{ ?d dcat:landingPage ?lp }}
            }} GROUP BY ?d ORDER BY ?d LIMIT {int(page)} OFFSET {int(i * page)}""")
        out += [{"uri": r["d"], "title": r.get("title", ""), "description": r.get("desc", ""),
                 "publisher": r.get("publisher", ""), "modified": r.get("modified", ""),
                 "periodicity": periodicity_label(r.get("periodicity", "")), "landing": r.get("landing", "")}
                for r in rows if r.get("d")]
        if progress:
            progress(f"katalóg: {len(out)} datasetov")
        if len(rows) < page or client.failed:
            break
    return out


def sync_catalog(db, client: NKODClient, embedder, deadline, progress: Callable[[str], None] | None = None,
                 batch: int = 256) -> dict[str, Any]:
    """Metadáta katalógu do SQLite a vektory (embeddingy na CPU, iba nové alebo zmenené texty – cache podľa hashu).
    Dá sa prerušiť: ďalší beh dopočíta zvyšok."""
    stats: dict[str, Any] = {}
    rows = fetch_all(client, progress=progress)
    if rows:
        db.upsert_catalog_datasets(rows)
    stats["stiahnuté"] = len(rows)
    if client.failed:
        stats["chyba"] = "katalóg nedostupný"
    if embedder is None:
        return stats
    items = [(r["id"], index_text(r)) for r in db.catalog_index()]
    have = {(e["ref_id"], e["text_hash"]) for e in db.embeddings_of_kind(INDEX_KIND, embedder.model)}
    todo = [it for it in items if (it[0], hashlib.sha1(it[1].encode("utf-8")).hexdigest()) not in have]
    done = 0
    for start in range(0, len(todo), batch):
        if deadline.expired():
            stats["nedokončené"] = len(todo) - done
            break
        embedder.vectors(INDEX_KIND, todo[start:start + batch])
        done += len(todo[start:start + batch])
        if progress:
            progress(f"vektory katalógu: {done} / {len(todo)}")
    stats["nové_vektory"] = done
    return stats


class LocalCatalog:
    """Hľadanie v lokálnom indexe podľa významu (kosínusová podobnosť embeddingov) – dopĺňa hľadanie podľa kmeňov
    slov v SPARQL, ktoré pri poliach ako „Rozpočet projektu“ vracia nesúvisiace datasety alebo nič."""

    def __init__(self, rows: list, matrix: np.ndarray, embedder):
        self.rows, self.matrix, self.embedder = rows, matrix, embedder

    @classmethod
    def load(cls, db, embedder) -> "LocalCatalog | None":
        if embedder is None:
            return None
        rows = db.catalog_index()
        if not rows:
            return None
        vecs = {e["ref_id"]: (e["text_hash"], e["vector"]) for e in db.embeddings_of_kind(INDEX_KIND, embedder.model)}
        keep, mats = [], []
        for r in rows:
            hit = vecs.get(r["id"])
            if hit and hit[0] == hashlib.sha1(index_text(r).encode("utf-8")).hexdigest():
                keep.append(r)
                mats.append(np.frombuffer(hit[1], dtype=np.float32))
        if not keep:
            return None
        return cls(keep, np.vstack(mats), embedder)

    def search(self, text: str, k: int = 8) -> list[CatalogDataset]:
        q = self.embedder.embed_texts([text])[0]
        sims = self.matrix @ q
        top = np.argsort(-sims)[:k]
        return [CatalogDataset(uri=self.rows[i]["uri"], title=self.rows[i]["title"] or "",
                               description=self.rows[i]["description"] or "", publisher=self.rows[i]["publisher"] or "",
                               modified=self.rows[i]["modified"] or "", periodicity=self.rows[i]["periodicity"] or "",
                               landing_page=self.rows[i]["landing"] or "") for i in top]

