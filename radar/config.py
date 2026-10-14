"""Načítanie konfigurácie z config.yaml (všetko má rozumné predvolené hodnoty)."""

from __future__ import annotations

import os
import unicodedata
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator


class LLMConfig(BaseModel):
    base_url: str = "http://localhost:11434/v1"  # Ollama; llama-server je http://localhost:8080/v1
    api_key: str = "local"  # lokálne servery kľúč nevyžadujú, klient ho však potrebuje
    model: str = "radar-qwen3.5-9b"
    temperature: float = 0.2
    max_tokens: int = 2048
    timeout_s: float = 600
    # Parametre posielané serveru navyše, napr. vypnutie "thinking" (Ollama: reasoning_effort: none).
    extra_body: dict[str, Any] = Field(default_factory=dict)
    # Ollama: po toľkých volaniach model uvoľní z pamäte. Server si inak pri každom novom prompte
    # odkladá cache (pri Qwen3.5 ~100 MB) a za hodinu behu narástol z 9 na 16 GB RAM, čo
    # spomalilo generovanie 15×. Opätovné načítanie trvá pár sekúnd. 0 = vypnuté.
    unload_every: int = 40


# ------------------------------------------------------------------ roly modelov (v0.2)
class RoleConfig(BaseModel):
    """Jeden model servera. Názov sa posiela v poli `model`, výmenu modelov rieši server
    (Ollama sama, alebo llama-swap / router mód llama-servera)."""

    model: str
    base_url: str | None = None  # None = models.base_url
    timeout_s: float = 600
    max_tokens: int = 2048
    temperature: float = 0.2
    extra_body: dict[str, Any] = Field(default_factory=dict)


class TaskRoute(BaseModel):
    role: str = "fast"
    thinking: bool = False
    k: int = 1  # počet hlasov (self-consistency); 1 = bez hlasovania


class EvalProfile(BaseModel):
    model: str  # názov roly (fast, accurate) alebo priamo názov modelu na serveri
    thinking: bool = False


def _route(role: str, thinking: bool = False, k: int = 1) -> TaskRoute:
    return TaskRoute(role=role, thinking=thinking, k=k)


# Profil "balanced" (spec 5.2): rýchly model hľadá kandidátov, presný rozhoduje.
DEFAULT_TASKS: dict[str, TaskRoute] = {
    "triage": _route("fast"),
    "screen": _route("fast"),
    "extract": _route("fast"),
    "number_extract": _route("fast"),
    # Rola „judge“ (sudca): posúdenie dôkazov a zaradenie – meranie 13. 10. 2026: 9B sa s 27B zhodol iba v 67 %
    # zo 100 posúdení a v 28 prípadoch bol benevolentnejší (čiastočne → podporené); generovanie ostáva na „accurate“.
    "entail": _route("judge"),
    "entail_gate": _route("judge", True, 3),
    "cluster_assign": _route("judge"),
    "merge_check": _route("judge"),
    "critique": _route("accurate", True),
    "market_structure": _route("accurate", True),
    # Agenti s nástrojmi (rola "agent"; predvolene ten istý model ako fast, v GUI sa dá zmeniť napr. na MoE 35B).
    "deep_dive": _route("agent"),
    "scout": _route("agent"),  # agent hľadá nové problémy (tlačidlo v GUI, run-all)
    "validate": _route("agent"),  # agent testuje uloženú hypotézu (dôkazy za aj proti)
    "gate1_sources": _route("agent"),  # Brána 1: agent hľadá zdroje dát k poliam
    "gate1_source_extract": _route("accurate"),  # Brána 1: zdroje z prečítaných stránok (keď agent nič neuloží)
    "gate1_fields": _route("accurate"),
    "gate1_user_eval": _route("judge"),
    "problem_card": _route("accurate"),  # problémová karta: problém, kto, rozhodnutie, výstup (s citátmi)
    "card_check": _route("judge"),  # podporujú citáty kľúčové polia karty?
    "experiment_plan": _route("accurate"),  # návrh experimentov Brány 2 z karty a profilu dát (kontroluje kód)
    "experiment_conclusion": _route("accurate"),  # záver pre problém iba z vypočítaných faktov
    "category_check": _route("fast"),  # kontrola vzorky zatriedenia textov (zatrieďujú embeddingy)  # Brána 1: ktoré polia pokrývajú dáta alebo schéma od človeka (skóre počíta kód)  # Brána 1: aké polia treba na riešenie problému
    "gate1_repro": _route("accurate"),  # Brána 1: SQL na zreprodukovanie známeho čísla (vykoná DuckDB)
    "gate2_prereg": _route("accurate"),  # Brána 2: návrh predregistrácie (hypotéza, metrika, postup)
    "gate2_demo": _route("accurate"),  # Brána 2: text dema iba z vypočítaných faktov
    "org_mentions": _route("fast"),
    "source_type": _route("fast"),  # typ vydavateľa neznámej domény (raz na doménu)
    "theme_name": _route("fast"),  # názov témy (zhluku hypotéz) zo zoznamu názvov hypotéz
    "person_mentions": _route("fast"),
    "segment_filter": _route("accurate"),
    "contact_assoc": _route("fast"),
    "contact_plan": _route("fast"),  # koho osloviť k hypotéze a ako ho nájsť (dopyty overuje a vykonáva kód)
    "role_map": _route("accurate", True, 3),
    "role_fit": _route("accurate"),
    "draft": _route("accurate"),
    "draft_check": _route("accurate", True),
}


def _default_roles() -> dict[str, RoleConfig]:
    return {
        "fast": RoleConfig(model="radar-qwen3.5-9b", timeout_s=600, max_tokens=2048),
        "accurate": RoleConfig(model="radar-qwen3.6-27b", timeout_s=1800, max_tokens=4096),
        "judge": RoleConfig(model="radar-qwen3.6-27b", timeout_s=1800, max_tokens=2048),
        "agent": RoleConfig(model="radar-qwen3.5-9b", timeout_s=900, max_tokens=2048),
        "embed": RoleConfig(model="radar-qwen3-embedding", timeout_s=120),
    }


class ModelsConfig(BaseModel):
    base_url: str = "http://localhost:11434/v1"
    api_key: str = "local"
    roles: dict[str, RoleConfig] = Field(default_factory=_default_roles)
    profile: str = "balanced"
    self_consistency_temperature: float = 0.7
    # Zapnutie a vypnutie uvažovania pre jednu požiadavku. Ollama: reasoning_effort;
    # llama-server so šablónou Qwen: chat_template_kwargs.enable_thinking.
    thinking_on: dict[str, Any] = Field(default_factory=lambda: {"reasoning_effort": "medium"})
    thinking_off: dict[str, Any] = Field(default_factory=lambda: {"reasoning_effort": "none"})
    # Pri uvažovaní model míňa tokeny pred odpoveďou; toto je rozpočet navyše k max_tokens úlohy.
    thinking_extra_tokens: int = 2048
    tasks: dict[str, TaskRoute] = Field(default_factory=lambda: dict(DEFAULT_TASKS))
    eval_profiles: dict[str, EvalProfile] = Field(
        default_factory=lambda: {
            "A": EvalProfile(model="fast", thinking=False),
            "B": EvalProfile(model="fast", thinking=True),
            "C": EvalProfile(model="accurate", thinking=False),
            "D": EvalProfile(model="accurate", thinking=True),
        }
    )
    unload_every: int = 40  # pozri LLMConfig.unload_every

    @field_validator("roles", mode="before")
    @classmethod
    def _merge_roles(cls, value: Any) -> Any:
        """V config.yaml stačí uviesť zmenené roly; ostatné ostanú predvolené."""
        if not isinstance(value, dict):
            return value
        merged: dict[str, Any] = {k: v.model_dump() for k, v in _default_roles().items()}
        for name, role in value.items():
            merged[name] = {**merged.get(name, {}), **(role or {})}
        return merged

    @field_validator("tasks", mode="before")
    @classmethod
    def _merge_tasks(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        merged: dict[str, Any] = {k: v.model_dump() for k, v in DEFAULT_TASKS.items()}
        for name, route in value.items():
            merged[name] = {**merged.get(name, {}), **(route or {})}
        return merged


class EmbeddingsConfig(BaseModel):
    enabled: bool = True
    base_url: str | None = None  # None = rovnaký server ako llm.base_url (alebo models.base_url)
    model: str = "radar-qwen3-embedding"  # qwen3-embedding:0.6b na CPU (ollama/Modelfile.qwen3-embedding)
    # Kosínusová podobnosť, od ktorej správa upozorní na možno rovnaké hypotézy. Kalibrované
    # pre qwen3-embedding:0.6b: rovnaký problém ~0,72–0,75, súvisiaci, ale iný problém do ~0,64.
    similar_hypotheses: float = 0.70


class ThemesConfig(BaseModel):
    """Témy nad hypotézami: priemerná väzba (average linkage) nad kosínusovou podobnosťou embeddingov."""

    # Kalibrované na 426 reálnych hypotézach (12. 10. 2026): 0,60 → 97 tém s 2–12 hypotézami (lieky, e-faktúry,
    # eurofondy v samosprávach…); 0,55 už spája nesúvisiace problémy, 0,70 zachytí iba takmer rovnaké hypotézy.
    threshold: float = 0.60
    max_size: int = 30  # väčšia téma by bola iba „verejný sektor“
    min_size: int = 2
    keep_jaccard: float = 0.5  # téma si ponechá ID a názov, ak sa jej členovia zmenili menej
    max_names_per_run: int = 60  # zvyšok dostane dočasne názov najtypickejšej hypotézy


class SearchConfig(BaseModel):
    searxng_url: str = "http://localhost:8888"
    language: str = "sk"
    results_per_query: int = 8
    delay_s: float = 2.0  # pauza medzi dopytmi, aby vyhľadávače SearXNG neobmedzili
    # Keď nič nevráti a vyhľadávače hlásia CAPTCHA/limit: jedno zopakovanie po narastajúcej pauze (max. 30 s)
    # a pri presnej fráze v úvodzovkách ešte pokus bez úvodzoviek (výsledky aj tak overuje kód).
    retry_on_empty: bool = True
    max_backoff_s: float = 30.0
    cache_minutes: float = 60.0  # rovnaký dopyt v jednom behu sa neposiela znova
    engine_suspend_minutes: float = 10.0  # vyhľadávač s CAPTCHA, limitom alebo výpadkom sa toľko minút vynechá


DEFAULT_BLOCKED_DOMAINS = ["linkedin.com", "facebook.com", "instagram.com", "x.com", "twitter.com", "tiktok.com"]


class FetchConfig(BaseModel):
    user_agent: str = "ProblemRadar/0.2 (osobny vyskumny projekt)"
    per_domain_delay_s: float = 3.0  # zdvorilá pauza medzi požiadavkami na tú istú doménu
    timeout_s: float = 30.0
    max_chars: int = 200_000
    max_pdf_pages: int = 40
    refetch_days: int = 14  # rovnakú URL nesťahuj častejšie
    respect_robots: bool = True
    # Sociálne siete sa nesťahujú nikdy (ani po presmerovaní); doplní sa people.blocked_domains.
    blocked_domains: list[str] = Field(default_factory=lambda: list(DEFAULT_BLOCKED_DOMAINS))

    @field_validator("user_agent")
    @classmethod
    def _ascii_only(cls, value: str) -> str:
        """HTTP hlavičky musia byť ASCII – diakritiku odstránime ("doplň" -> "dopln")."""
        return unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")


class PipelineConfig(BaseModel):
    output_language: str = "sk"
    chunk_chars: int = 6000
    chunk_overlap: int = 400
    max_chunks_per_doc: int = 8
    max_new_docs_per_run: int = 60
    max_docs_to_extract: int = 40
    max_claims_per_chunk: int = 5
    verify_with_llm: bool = True
    quote_match_threshold: int = 90  # 0–100, ako presne sa citát musí nájsť v zdroji
    duplicate_quote_threshold: int = 92  # rovnaký citát v inom dokumente = prebratý text, nie nový dôkaz
    critique_min_claims: int = 2
    market_min_claims: int = 3
    max_market_per_run: int = 2
    market_refresh_days: int = 30
    # Hodnoty pre odhad trhu sa hľadajú najprv na týchto doménach (ŠÚ SR, Eurostat), potom na celom webe.
    market_preferred_sites: list[str] = Field(default_factory=lambda: ["statistics.sk", "ec.europa.eu"])
    agent_max_steps: int = 12
    max_run_minutes: int = 240
    # Koľko tvrdení posúdi presný model za beh (pri ~1–2 min na tvrdenie). 0 = bez limitu.
    max_entail_per_run: int = 150
    max_cluster_per_run: int = 150


# ------------------------------------------------------------------ objavovanie (R-DISC)
DEFAULT_PAIN_PATTERNS = [
    "nedostatok pracovníkov", "meškanie dodávok", "oneskorenie projektov", "ručné prepisovanie údajov",
    "evidencia v exceli", "duplicitné zadávanie údajov", "chybovosť", "reklamácie", "prestoje",
    "administratívna záťaž", "nová povinnosť od", "pokuta za nesplnenie", "nedostatočná kapacita",
    "chýba prehľad o", "rastúce náklady na", "kontrola zistila nedostatky", "dlhé čakacie lehoty",
    "neefektívny proces", "manual process bottleneck", "compliance burden",
]
DEFAULT_SECTORS = [
    "priemyselná výroba", "výroba strojov a zariadení", "výroba kovových konštrukcií", "automobilový priemysel",
    "potravinárstvo", "stavebníctvo", "doprava a logistika", "energetika", "vodárenstvo a odpady",
    "veľkoobchod", "maloobchod", "IT služby", "účtovníctvo a poradenstvo", "samosprávy obcí",
    "štátna správa", "nemocnice", "sociálne služby", "školy", "poľnohospodárstvo",
]


class PromotionConfig(BaseModel):
    min_claims: int = 2
    min_domains: int = 2


class DiscoveryConfig(BaseModel):
    enabled: bool = True
    exploration_share: float = 0.3  # podiel dokumentov behu z objavovania
    min_arm_share: float = 0.05  # minimálny podiel každej kategórie zdrojov
    sources_file: str = "sources.yaml"
    # Pod touto podobnosťou s najbližšou hypotézou vznikne nová hypotéza "candidate" bez pýtania sa
    # modelu. Kalibrácia pre qwen3-embedding:0.6b: rovnaký problém ~0,72–0,75, iný do ~0,64.
    novelty_threshold: float = 0.55
    candidate_promotion: PromotionConfig = Field(default_factory=PromotionConfig)
    screen_chars: int = 3000  # koľko znakov zo začiatku dokumentu posúdi `screen`
    queries_per_run: int = 6  # bunky matice dopytov (vzor bolesti × odvetvie) za beh
    # Zo zdrojov s vyhľadávaním (method search) jednej kategórie sa za beh použijú najviac 3 v rotácii; RSS zdroje
    # vyhľadávač nepotrebujú a idú vždy. Desiatky zväzov a úradov by inak poslali stovky dopytov a SearXNG blokuje.
    search_sources_per_arm: int = 3
    seed: int | None = None  # pevný seed banditu (testy); None = náhodný
    pain_patterns: list[str] = Field(default_factory=lambda: list(DEFAULT_PAIN_PATTERNS))
    sectors: list[str] = Field(default_factory=lambda: list(DEFAULT_SECTORS))


# ------------------------------------------------------------------ agentické workflowy
class WorkflowsConfig(BaseModel):
    """Agent, ktorý hľadá nové hypotézy (scout), a agent, ktorý testuje uložené hypotézy (validate).
    Agent má iba nástroje na čítanie a uloženie dôkazu; dôkazy overí kód a presný model ako ostatné."""

    scout_sessions_per_run: int = 2  # sedenia agenta v run-all (bunky matice vzor bolesti × odvetvie)
    # Autopilot: akcia sa zaradí iba v týchto hodinách miestneho času [od, do) – napr. {"people": [6, 22]};
    # od > do = cez polnoc ([22, 6]). Prázdne = bez obmedzenia (predvolené).
    autopilot_hours: dict[str, list[int]] = Field(default_factory=dict)
    # 13. 10. 2026: viac dokumentov z katalógu (146 zdrojov, 15 kategórií – pri 15 dokumentoch ~1 na kategóriu),
    # menej sedení agenta (v noci 17–22 min na ~18 stránok a 3–4 citáty).
    scout_sessions_per_job: int = 3  # tlačidlo v GUI: najviac toľko sedení (obmedzuje aj čas)
    scout_docs_per_job: int = 40  # dokumenty z objavovania (matica + bandit) pri tlačidle v GUI
    scout_max_steps: int = 10
    validate_per_run: int = 6  # hypotéz otestovaných v run-all (agent na rýchlom modeli, ~3 min na hypotézu)
    validate_batch: int = 5  # tlačidlo v GUI: dávka hypotéz pred posúdením presným modelom
    validate_max_steps: int = 8
    validate_max_page_claims: int = 8  # tvrdení z prečítaných stránok na hypotézu, keď agent nič neuloží
    retest_days: int = 90  # po koľkých dňoch sa otestovaná hypotéza otestuje znova
    known_titles_in_prompt: int = 12  # scout dostane názvy podobných známych hypotéz, aby ich neopakoval
    jobs_log_dir: str = "logs/jobs"  # výstup úloh spustených z GUI (logy maskujú e-maily a telefóny)


# ------------------------------------------------------------------ Brána 1 a 2
class CatalogEntry(BaseModel):
    """Známy zdroj dát (štartovací bod pre agenta; dostupnosť a licenciu overí kód pri každom použití)."""

    name: str
    url: str
    publisher: str = ""
    topics: str = ""  # kľúčové slová, čo zdroj obsahuje


DEFAULT_CATALOG = [
    CatalogEntry(name="Národný katalóg otvorených dát", url="https://data.slovensko.sk", publisher="MIRRI SR",
                 topics="otvorené dáta všetkých inštitúcií, datasety CSV/JSON"),
    CatalogEntry(name="DATAcube ŠÚ SR", url="https://datacube.statistics.sk", publisher="Štatistický úrad SR",
                 topics="štatistika obcí, regiónov, odvetví, ceny, mzdy, podniky"),
    CatalogEntry(name="Register účtovných závierok (RÚZ) – API", url="https://www.registeruz.sk/cruz-public/api",
                 publisher="MF SR", topics="účtovné závierky, výnosy, náklady, zamestnanci podľa IČO"),
    CatalogEntry(name="Register právnických osôb (RPO) – API", url="https://api.statistics.sk/rpo/v1",
                 publisher="Štatistický úrad SR", topics="organizácie, IČO, právna forma, SK NACE, sídlo"),
    CatalogEntry(name="Centrálny register zmlúv (CRZ)", url="https://www.crz.gov.sk", publisher="Úrad vlády SR",
                 topics="zmluvy verejných inštitúcií, dodávatelia, sumy"),
    CatalogEntry(name="ÚVO – vestník a zákazky", url="https://www.uvo.gov.sk", publisher="Úrad pre verejné obstarávanie",
                 topics="verejné obstarávanie, zákazky, víťazi, ceny"),
    CatalogEntry(name="ITMS2014+ otvorené dáta", url="https://opendata.itms2014.sk", publisher="MIRRI SR",
                 topics="eurofondy, projekty, prijímatelia, výzvy, čerpanie"),
    CatalogEntry(name="Finančná správa – otvorené dáta", url="https://opendata.financnasprava.sk",
                 publisher="Finančné riaditeľstvo SR", topics="daňové subjekty, DPH, dlžníci"),
    CatalogEntry(name="Eurostat", url="https://ec.europa.eu/eurostat/data/database", publisher="Európska komisia",
                 topics="porovnanie krajín EÚ, odvetvia, regióny NUTS"),
    CatalogEntry(name="NKÚ SR – kontrolné správy", url="https://www.nku.gov.sk", publisher="Najvyšší kontrolný úrad SR",
                 topics="zistenia kontrol, čísla o hospodárení inštitúcií"),
    CatalogEntry(name="Útvar hodnoty za peniaze – revízie výdavkov", url="https://www.mfsr.sk/sk/financie/hodnota-za-peniaze/",
                 publisher="MF SR", topics="revízie výdavkov, analýzy, čísla o efektivite"),
]


class Gate1Config(BaseModel):
    """Brána 1 – dáta a dataset (beží pre hypotézy vybrané na Bránu 0, súbežne s rozhovormi)."""

    max_hypotheses_per_run: int = 2  # run-all: koľko hypotéz za noc
    max_fields: int = 6  # menej polí = kratší výstup presného modelu (~4,5 tok/s)
    source_agent_steps: int = 10
    max_sources_per_field: int = 3
    datasets_dir: str = "data/datasets"  # mimo gitu (data/)
    max_dataset_mb: int = 2048  # limit jedného súboru
    max_total_gb: int = 50  # limit všetkých datasetov spolu
    max_files_per_source: int = 3
    api_page_size: int = 1000  # záznamov na stranu pri sťahovaní z API (limit, minId/offset/page)
    max_api_pages: int = 200  # najviac strán na endpoint (aj limit veľkosti max_dataset_mb)
    key_field_weeks: int = 6  # kľúčové pole nezískané do 6 týždňov → príznak (nie vyradenie)
    repro_match: float = 0.01  # relatívna odchýlka pre „zhoda“
    repro_close: float = 0.10  # pre „blízko“
    max_repro_per_hypothesis: int = 3
    sql_timeout_s: int = 30
    # Prechod do Brány 2 (pokyn používateľa 13. 10. 2026): pripravenosť dát ≥ prah → automaticky „prešla“; inak stav
    # „čaká na tvoje dáta“ – po vstupe človeka (text, schéma, súbor) posúdi model polia a kód spočíta vhodnosť ≥ prah.
    auto_gate2_readiness: float = 0.5
    user_data_threshold: float = 0.5
    # Rozdelenie času akcie Brána 1 (úloha #75: agent zdrojov minul spoločný úsek a kontrola so sťahovaním sa preskočili).
    download_reserve_minutes: float = 15.0  # kontrola zdrojov a sťahovanie (bez modelu) – vyhradené, agent ich nemíňa
    sources_minutes_per_hypothesis: float = 15.0  # strop agenta zdrojov na jednu hypotézu (H15 minula 23 min)
    fields_minutes: float = 4.0  # odhad návrhu polí presným modelom na hypotézu (merané ~3,5 min)
    # Národný katalóg otvorených dát (SPARQL): kandidáti na zdroje bez vyhľadávača, súbory z distribúcií.
    nkod_enabled: bool = True
    nkod_sparql: str = "https://data.slovensko.sk/api/sparql"
    nkod_hits_per_field: int = 2  # hľadanie podľa názvu poľa je zašumené; agent si katalóg prehľadá aj sám
    nkod_rerank_pool: int = 8  # toľko kandidátov z katalógu sa zoradí podľa významu poľa a hypotézy (embeddingy)
    # Centrálny register zmlúv: denné dávky crz.gov.sk/export/RRRR-MM-DD.zip (robots.txt: crawl-delay 10 s).
    # 270 dní = 9 mesačných období pre spätný test Brány 2 (5 histórie + 4 testované); ~1 h sťahovania.
    crz_days: int = 270
    crz_delay_s: float = 10.0
    min_wide_periods: int = 3  # široká tabuľka (roky/štvrťroky ako stĺpce) → dlhý tvar dsN_long od 3 období
    max_long_rows: int = 5_000_000  # limit riadkov dlhého tvaru (riadky × obdobia)
    catalog: list[CatalogEntry] = Field(default_factory=lambda: list(DEFAULT_CATALOG))


class CardsConfig(BaseModel):
    """Problémové karty a rozdelenie širokých hypotéz (pokyn používateľa 13. 10. 2026)."""

    max_per_run: int = 6  # kariet na jeden krok (27B: ~3–5 min na kartu so sudcom)
    max_claims: int = 30  # citátov v zadaní pre model
    min_claims: int = 2  # hypotéza s menej citátmi kartu nedostane
    min_claims_per_subproblem: int = 2
    auto_split: bool = True  # podproblémy karty → podhypotézy (citáty sa presunú, dáta Brány 1 sa zdedia)
    refresh_new_claims: int = 5  # nepotvrdená karta sa prepočíta, keď pribudne toľko citátov
    check_fields: list[str] = Field(default_factory=lambda: ["problem", "who", "decision"])
    group_min_claims: int = 6  # od toľkých citátov sa pred kartou zoskupia podľa podobnosti (pomôcka pre rozdelenie)
    group_threshold: float = 0.6  # priemerná kosínusová podobnosť skupiny (embeddingy s inštrukciou)


class ExperimentsConfig(BaseModel):
    """Experimenty Brány 2 podľa typu úlohy (návrh modelom z karty a profilu dát, test kódom)."""

    plans_per_hypothesis: int = 3  # koľko experimentov navrhnúť na hypotézu (rôzne úlohy alebo štítky)
    max_plan_retries: int = 2  # oprava návrhu modelom po chybách skúšobného behu (27B: chýbajúci parameter kroku)
    max_category_texts: int = 30000  # zatriedenie textu: najčastejších rôznych textov (embeddingy ~70/s na CPU)
    category_check_sample: int = 30  # vzorka zatriedenia na kontrolu modelom (odhad presnosti)
    test_share: float = 0.3  # posledná časť obdobia na test (učenie a validácia sú staršie)
    min_positives: int = 30  # poradie: najmenej pozitívnych prípadov v teste
    bootstrap: int = 300
    top_items: int = 20  # položky na kontrolu vo výsledku
    max_entities: int = 300  # predpoveď: najväčších entít (spoločný model)
    forecast_folds: int = 6  # predpoveď: posledných období (polovica výber modelu, polovica test)
    threshold_window: float = 0.1  # prah: okno pod prahom ako podiel prahu


class Gate2Config(BaseModel):
    """Brána 2 – experimenty a demo (pre hypotézy, pri ktorých si Bránu 1 označil ako prejdenú)."""

    max_targets_per_hypothesis: int = 5  # dvojice hypotéza × organizácia
    backtest_folds: int = 4  # posledné obdobia, na ktorých sa predpoveď overí
    min_history: int = 5  # najmenej období histórie pred prvou predpoveďou
    bootstrap: int = 1000
    demos_dir: str = "reports/demos"


# ------------------------------------------------------------------ ľudia (R-ORG, R-PPL)
class PriorityWeights(BaseModel):
    role_fit: float = 0.35
    link: float = 0.25
    channel: float = 0.20
    org_fit: float = 0.20


class PeopleConfig(BaseModel):
    auto_select_top: int = 3
    min_strength: str = "stredná"
    max_orgs_per_hypothesis: int = 30
    max_people_per_hypothesis: int = 15
    max_people_per_org: int = 2
    max_experts_per_hypothesis: int = 3
    max_contact_pages_per_org: int = 5
    registry_delay_s: float = 1.0
    registry_cache_days: int = 30
    check_mx: bool = False  # SMTP overovanie schránok sa nerobí nikdy
    search_profiles: bool = True  # URL profilu (napr. LinkedIn) z výsledku vyhľadávania, nesťahuje sa
    # Kontakty ku konkrétnej osobe (krok contacts): stránky iba z overeného webu jej organizácie alebo z týchto domén.
    official_domains: list[str] = Field(default_factory=lambda: ["gov.sk", "nrsr.sk"])
    max_contact_pages_per_person: int = 3
    # Brána 0 automaticky aj pre hypotézy, ktorých test (validate) skončil jedným z týchto verdiktov; ručná voľba
    # (vypnutie v GUI alebo `select --people off`) má prednosť a automatický výber ju neprepíše.
    auto_select_validated: bool = True
    auto_select_verdicts: list[str] = Field(default_factory=lambda: ["podložená"])
    max_data_holders_per_hypothesis: int = 5  # ľudia, ktorí držia dáta k hypotéze (vzorka, schéma); zvlášť od zákazníkov
    # Hľadanie kontaktov podľa hypotézy (krok contact-search): rýchly model navrhne ciele (zákazník, držiteľ dát,
    # expert) a dopyty; vyhľadávanie, overenie webu cez IČO a kontakty robí kód.
    contact_search: bool = True
    contact_search_rounds: int = 2  # ďalšie kolo plánu, kým je kontaktovateľných menej ako contact_goal_per_hypothesis
    contact_queries_per_run: int = 12  # najviac dopytov na hypotézu a beh
    contact_goal_per_hypothesis: int = 15  # ľudia alebo organizácie s e-mailom
    contact_research_days: int = 14  # hypotéza sa prehľadá znova najskôr po tomto čase
    # Domény, ktoré sú v aggregator_domains (nie web zákazníka), no sú webom organizácie, ktorá drží dáta alebo je
    # expertom (kontrolór, watchdog, štatistický úrad) – pri cieľoch „držiteľ dát“ a „expert“ sa nepreskakujú.
    expert_org_domains: list[str] = Field(
        default_factory=lambda: ["nku.gov.sk", "slovensko.digital", "transparency.sk", "statistics.sk", "uvo.gov.sk",
                                 "rpvs.gov.sk"]
    )
    blocked_domains: list[str] = Field(default_factory=lambda: list(DEFAULT_BLOCKED_DOMAINS))
    priority_weights: PriorityWeights = Field(default_factory=PriorityWeights)
    # Domény, ktoré nie sú webom organizácie (katalógy firiem, registre, médiá).
    aggregator_domains: list[str] = Field(
        default_factory=lambda: [
            "finstat.sk", "foaf.sk", "orsr.sk", "zrsr.sk", "indexpodnikatela.sk", "firmy.sk", "zoznam.sk",
            "azet.sk", "wikipedia.org", "statistics.sk", "registeruz.sk", "crz.gov.sk", "uvo.gov.sk",
            "slovensko.sk", "facebook.com", "linkedin.com", "youtube.com", "kompass.com", "europages.com",
            "edb.sk", "detail.sk", "aktuality.sk", "sme.sk", "trend.sk", "hnonline.sk", "pravda.sk",
            "dnoviny.sk", "teraz.sk", "openiazoch.zoznam.sk", "slovensko.digital", "datahub.ekosystem.slovensko.digital",
            "transparency.sk", "nku.gov.sk", "google.com", "bing.com", "mapy.cz", "rpvs.gov.sk",
        ]
    )


# ------------------------------------------------------------------ oslovenia (R-OUT)
class SenderConfig(BaseModel):
    name: str = "{MENO_PRIEZVISKO}"
    email: str = "{EMAIL_ODOSIELATELA}"
    phone: str = ""
    affiliation: str = "{AFILIACIA}"
    privacy_notice_url: str = "{URL_INFORMACIE_O_SPRACUVANI}"
    # Gramatický rod v 1. osobe („rád/rada by som“): "" = neutrálne formulácie (rod sa neodhaduje z mena), "m" alebo "f".
    gender: str = ""


class OutreachConfig(BaseModel):
    sender: SenderConfig = Field(default_factory=SenderConfig)
    language: str = "sk"
    daily_cap: int = 10
    follow_up_business_days: list[int] = Field(default_factory=lambda: [5, 7])
    max_follow_ups: int = 1
    holidays: list[str] = Field(default_factory=list)  # YYYY-MM-DD, nepočítajú sa ako pracovné dni
    email_subject_max_chars: int = 70
    email_max_words: int = 150
    linkedin_note_max_chars: int = 200  # bezplatný účet; Premium 300
    phone_opener_max_words: int = 80
    mailto_max_chars: int = 1800
    forbidden_phrases: list[str] = Field(
        default_factory=lambda: [
            "ponúkame", "naše riešenie", "náš produkt", "demo", "zľava", "akcia", "bezplatná konzultácia",
            "cenová ponuka",
        ]
    )
    exclude_orgs: list[str] = Field(default_factory=list)  # IČO alebo domény, ktoré sa nikdy neoslovujú
    channels: list[str] = Field(default_factory=lambda: ["email", "linkedin", "phone"])
    # Návrh + kontrola presným modelom trvá ~1–2 min; za beh sa pripraví najviac toľko návrhov
    # (najprv osoby s najvyššou prioritou). 0 = bez limitu.
    max_drafts_per_run: int = 24
    facts_per_draft: int = 3  # koľko overených dôkazov hypotézy dostane generátor ako fakty


# ------------------------------------------------------------------ súkromie (R-PRIV)
class PrivacyConfig(BaseModel):
    lia_completed: bool = False  # true až po vyplnení docs/LIA_template.md (kópia v data/LIA.md)
    retention_days_uncontacted: int = 90
    retention_days_after_interaction: int = 365
    retention_days_declined: int = 30
    retention_days_exports: int = 30
    pseudonymize_reports: bool = True
    salt_path: str | None = None  # None = .salt vedľa databázy (data/.salt); súbor je mimo gitu


class GuiConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8501


class Topic(BaseModel):
    name: str
    queries: list[str] = Field(default_factory=list)


class Feed(BaseModel):
    name: str
    url: str
    max_items: int = 15
    triage: bool = True  # model vyberie z titulkov len články, ktoré pravdepodobne opisujú problém


class LinkSource(BaseModel):
    """Stránka so zoznamom dokumentov bez RSS (napr. ÚHP): zoberú sa odkazy podľa regulárneho výrazu."""

    name: str
    url: str
    pattern: str
    max_items: int = 10
    triage: bool = False


class Project(BaseModel):
    """Rozpracovaný nápad (priečinok v ExperimentalProjects), ktorý radar sleduje ako hypotézu."""

    name: str
    path: str | None = None
    title: str
    segment: str = ""
    description: str = ""
    queries: list[str] = Field(default_factory=list)

    @property
    def topic(self) -> str:
        return f"Projekt: {self.name}"


class Config(BaseModel):
    db_path: str = "data/radar.sqlite"
    reports_dir: str = "reports"
    logs_dir: str = "logs"
    exports_dir: str = "exports"
    llm: LLMConfig = Field(default_factory=LLMConfig)
    # Roly modelov (v0.2). Ak sekcia chýba, všetky úlohy idú na `llm` (správanie v0.1).
    models: ModelsConfig | None = None
    embeddings: EmbeddingsConfig = Field(default_factory=EmbeddingsConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    fetch: FetchConfig = Field(default_factory=FetchConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    workflows: WorkflowsConfig = Field(default_factory=WorkflowsConfig)
    themes: ThemesConfig = Field(default_factory=ThemesConfig)
    gate1: Gate1Config = Field(default_factory=Gate1Config)
    gate2: Gate2Config = Field(default_factory=Gate2Config)
    cards: CardsConfig = Field(default_factory=CardsConfig)
    experiments: ExperimentsConfig = Field(default_factory=ExperimentsConfig)
    people: PeopleConfig = Field(default_factory=PeopleConfig)
    outreach: OutreachConfig = Field(default_factory=OutreachConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    gui: GuiConfig = Field(default_factory=GuiConfig)
    topics: list[Topic] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    feeds: list[Feed] = Field(default_factory=list)
    link_sources: list[LinkSource] = Field(default_factory=list)
    seed_urls: list[str] = Field(default_factory=list)
    config_dir: str = "."  # priečinok s config.yaml (doplní load_config)

    def all_topics(self) -> list[Topic]:
        """Témy z config.yaml a dopyty rozpracovaných projektov."""
        return list(self.topics) + [Topic(name=p.topic, queries=p.queries) for p in self.projects if p.queries]

    def blocked_domains(self) -> list[str]:
        return sorted(set(self.fetch.blocked_domains) | set(self.people.blocked_domains))

    def path(self, value: str) -> Path:
        """Relatívna cesta od priečinka s config.yaml."""
        p = Path(value)
        return p if p.is_absolute() else Path(self.config_dir) / p


ENV_OVERRIDES = {
    # V kontajneri bežia model aj SearXNG na iných adresách než na hostiteľovi (docker-compose.yml).
    "RADAR_LLM_BASE_URL": ("llm", "base_url"),
    "RADAR_SEARXNG_URL": ("search", "searxng_url"),
}


def deep_merge(base: dict, override: dict) -> dict:
    """Hodnoty z override prepíšu base; vnorené slovníky sa zlúčia po kľúčoch (zoznamy sa nahradia celé)."""
    out = dict(base)
    for key, value in override.items():
        out[key] = deep_merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def load_config(path: str | Path) -> Config:
    """Relatívne cesty (databáza, správy, logy, projekty) sa berú od priečinka s config.yaml,
    takže na pracovnom priečinku nezáleží (napr. pri spúšťaní z Plánovača úloh)."""
    path = Path(path).resolve()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    local = path.with_name("config.local.yaml")  # osobné údaje (profil odosielateľa) mimo gitu
    if local.exists():
        data = deep_merge(data, yaml.safe_load(local.read_text(encoding="utf-8")) or {})
    cfg = Config.model_validate(data)
    for env, (section, field) in ENV_OVERRIDES.items():
        if os.environ.get(env):
            setattr(getattr(cfg, section), field, os.environ[env])
    if os.environ.get("RADAR_LLM_BASE_URL") and cfg.models is not None:
        cfg.models.base_url = os.environ["RADAR_LLM_BASE_URL"]
    base = path.parent
    cfg.config_dir = str(base)
    for field in ("db_path", "reports_dir", "logs_dir", "exports_dir"):
        value = Path(getattr(cfg, field))
        if not value.is_absolute():
            setattr(cfg, field, str(base / value))
    if cfg.privacy.salt_path and not Path(cfg.privacy.salt_path).is_absolute():
        cfg.privacy.salt_path = str(base / cfg.privacy.salt_path)
    for project in cfg.projects:
        if project.path and not Path(project.path).is_absolute():
            project.path = str((base / project.path).resolve())
    return cfg
