"""Schéma databázy po verziách. Existujúcu verziu nikdy neupravuj, pridaj novú (migrations.py)."""

# ---------------------------------------------------------------------------------- verzia 1
# Register dôkazov v0.1 (vrátane stĺpcov, ktoré v0.1 dopĺňala pri otvorení databázy).
SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL,
    domain TEXT NOT NULL,
    title TEXT,
    source_kind TEXT NOT NULL,          -- seed | rss | links | search | market | agent | discovery | register | interview
    topic TEXT,
    query TEXT,
    fetched_at TEXT NOT NULL,           -- kedy sme tento obsah videli prvýkrát
    last_checked_at TEXT NOT NULL,      -- kedy sme URL naposledy sťahovali
    content_hash TEXT NOT NULL,
    text TEXT NOT NULL,
    extract_progress INTEGER NOT NULL DEFAULT 0,
    extracted_at TEXT,
    UNIQUE (url, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_documents_url ON documents(url);

CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    chunk_index INTEGER NOT NULL,
    topic TEXT,
    problem TEXT NOT NULL,
    affected_party TEXT,
    claim_type TEXT,
    quote TEXT NOT NULL,
    numbers_json TEXT NOT NULL DEFAULT '[]',
    origin TEXT NOT NULL DEFAULT 'extract',   -- extract | agent | market | interview
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | verified | partial | rejected
    quote_score REAL,
    verify_note TEXT,
    duplicate_of INTEGER REFERENCES claims(id),  -- prebratý text: rovnaký citát už máme z iného dokumentu
    created_at TEXT NOT NULL,
    verified_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_claims_status ON claims(status);

CREATE TABLE IF NOT EXISTS hypotheses (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    segment TEXT,
    status TEXT NOT NULL DEFAULT 'new',       -- candidate | new | watch | interviewing | rejected
    project TEXT,                             -- názov rozpracovaného projektu z config.yaml
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hypothesis_claims (
    hypothesis_id INTEGER NOT NULL REFERENCES hypotheses(id),
    claim_id INTEGER NOT NULL UNIQUE REFERENCES claims(id),
    PRIMARY KEY (hypothesis_id, claim_id)
);

CREATE TABLE IF NOT EXISTS critiques (
    id INTEGER PRIMARY KEY,
    hypothesis_id INTEGER NOT NULL REFERENCES hypotheses(id),
    created_at TEXT NOT NULL,
    claim_count INTEGER NOT NULL,
    content_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_estimates (
    id INTEGER PRIMARY KEY,
    hypothesis_id INTEGER NOT NULL REFERENCES hypotheses(id),
    created_at TEXT NOT NULL,
    complete INTEGER NOT NULL,
    unit TEXT,
    low REAL,
    base REAL,
    high REAL,
    detail_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY,
    hypothesis_id INTEGER REFERENCES hypotheses(id),
    created_at TEXT NOT NULL,
    source TEXT NOT NULL,                     -- human | agent
    text TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    stage TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    stats_json TEXT
);

CREATE TABLE IF NOT EXISTS merge_checks (
    a INTEGER NOT NULL,                       -- menšie id hypotézy
    b INTEGER NOT NULL,
    same INTEGER NOT NULL,                    -- rozhodnutie modelu, či ide o rovnaký problém
    checked_at TEXT NOT NULL,
    PRIMARY KEY (a, b)
);

CREATE TABLE IF NOT EXISTS embeddings (
    kind TEXT NOT NULL,                       -- claim | hypothesis
    ref_id INTEGER NOT NULL,
    model TEXT NOT NULL,
    text_hash TEXT NOT NULL,                  -- pri zmene textu sa vektor prepočíta
    vector BLOB NOT NULL,                     -- float32, normalizovaný na dĺžku 1
    PRIMARY KEY (kind, ref_id, model)
);
"""

# Stĺpce, ktoré v0.1 pridávala k starším databázam pri otvorení (súčasť verzie 1).
V1_COLUMNS = {
    "claims": {"duplicate_of": "INTEGER REFERENCES claims(id)"},
    "hypotheses": {"project": "TEXT"},
}
V1_INDEXES = [
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_hypotheses_project ON hypotheses(project) WHERE project IS NOT NULL",
]

# ---------------------------------------------------------------------------------- verzia 2
# Brána 0 agregátor (docs/SPEC_brana0.md, kap. 13). Odchýlky sú v docs/DECISIONS.md.
V2_COLUMNS = {
    "documents": {
        "source_category": "TEXT",            # audit|spending_review|fiscal|ombudsman|legislation|eu_obligations|procurement|association|annual_report|media|innovation_call|event|other
        "screened": "TEXT",                   # NULL | yes | no  (výsledok `screen` pri objavovaní)
        "bandit_counted": "INTEGER NOT NULL DEFAULT 0",  # výsledok dokumentu už je v query_stats
    },
    "claims": {
        "sector_hint": "TEXT",
        "effective_date": "TEXT",             # pri claim_type = regulation
        "details_json": "TEXT",               # regulation: kto, čo, sankcia; demand_signal: kto hľadá čo
        "det_status": "TEXT",                 # výsledok kontroly kódom: ok | partial (NULL = ešte nie)
        "entail_verdict": "TEXT",             # supported | partial | unsupported | undecided
        "gate_verdict": "TEXT",               # entail_gate (fakty pre oslovenia): supported | partial | unsupported | undecided
        "low_agreement": "INTEGER NOT NULL DEFAULT 0",
    },
    "hypotheses": {
        "origin": "TEXT NOT NULL DEFAULT 'topic'",          # topic | discovery | manual | interview
        "topic": "TEXT",
        "sector": "TEXT",
        "first_seen_at": "TEXT",
        "people_enabled": "INTEGER NOT NULL DEFAULT 0",
        "people_manual": "INTEGER",                         # NULL = automatický výber, 1 = zapnuté ručne, 0 = vypnuté ručne
        "founder_fit": "TEXT",                              # high | medium | low | unknown (ručne)
        "gate0_status": "TEXT NOT NULL DEFAULT 'not_started'",  # not_started | in_progress | passed | failed | paused
        "payer_confirmed": "TEXT",                          # kto platí (ručne), NULL = nepotvrdené
        "segment_filter_json": "TEXT",                      # segment -> filter pre registre (R-ORG-1), upraviteľný v GUI
    },
}

SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS organizations (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  name_key TEXT NOT NULL,                   -- názov bez diakritiky a právnej formy (deduplikácia)
  ico TEXT UNIQUE,
  legal_form TEXT, sk_nace TEXT, size_category TEXT,
  sector_kind TEXT,                         -- public|private|nonprofit
  region TEXT, municipality TEXT,
  website TEXT, website_verified INTEGER NOT NULL DEFAULT 0, website_evidence TEXT,
  source TEXT NOT NULL,                     -- evidence|procurement|rpo|ruz|search|public_list|manual
  source_url TEXT,
  excluded INTEGER NOT NULL DEFAULT 0,
  register_json TEXT,                       -- údaje z RPO/RÚZ o organizácii (bez osobných údajov)
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_org_name_key ON organizations(name_key);

CREATE TABLE IF NOT EXISTS org_hypotheses (
  org_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  hypothesis_id INTEGER NOT NULL REFERENCES hypotheses(id),
  relation TEXT NOT NULL,                   -- mentioned_with_problem|procured_related|segment_match|manual|quoted_person
  evidence_claim_id INTEGER REFERENCES claims(id),
  evidence_url TEXT,
  fit_json TEXT,                            -- zložky zhody so segmentom
  procurement_active INTEGER NOT NULL DEFAULT 0,   -- R-OUT-13: práve obstaráva súvisiacu vec
  procurement_note TEXT,
  status TEXT NOT NULL DEFAULT 'candidate', -- candidate|website_checked|people_done|excluded
  created_at TEXT,
  PRIMARY KEY (org_id, hypothesis_id)
);

CREATE TABLE IF NOT EXISTS contact_pages (  -- iba metadáta, obsah stránky sa neukladá (R-PPL-2)
  id INTEGER PRIMARY KEY,
  org_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  url TEXT NOT NULL, fetched_at TEXT NOT NULL, content_hash TEXT,
  status TEXT NOT NULL,                     -- ok|robots_blocked|blocked_domain|error
  UNIQUE (url, content_hash)
);

CREATE TABLE IF NOT EXISTS people (
  id INTEGER PRIMARY KEY,
  org_id INTEGER REFERENCES organizations(id),
  full_name TEXT NOT NULL, titles TEXT, role_title TEXT, role_category TEXT, gender_hint TEXT,
  name_key TEXT NOT NULL,                   -- meno bez titulov a diakritiky, malými písmenami (deduplikácia)
  source_url TEXT NOT NULL, source_snippet TEXT NOT NULL,
  source_kind TEXT,                         -- website|rpo|evidence|procurement|search
  lawful_basis TEXT NOT NULL DEFAULT 'legitimate_interest',
  collected_for_hypothesis_id INTEGER NOT NULL REFERENCES hypotheses(id),
  is_expert INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'candidate', -- candidate|approved|contacted|replied|interviewed|declined|excluded
  note TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  last_interaction_at TEXT, purge_after TEXT,
  UNIQUE (org_id, name_key)
);

CREATE TABLE IF NOT EXISTS contact_points (
  id INTEGER PRIMARY KEY,
  person_id INTEGER REFERENCES people(id) ON DELETE CASCADE,
  org_id INTEGER REFERENCES organizations(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,                       -- email|phone|profile_url|contact_form|hidden_contact
  value TEXT NOT NULL,                      -- e-mail malými písmenami, telefón E.164, URL
  is_generic INTEGER NOT NULL DEFAULT 0,
  source_url TEXT NOT NULL, source_snippet TEXT NOT NULL,
  extraction TEXT NOT NULL,                 -- mailto|tel|regex|phonenumbers|search_result|manual|obfuscated
  checks_json TEXT,
  created_at TEXT NOT NULL
);
-- UNIQUE s NULL v SQLite neplatí (NULL sa považuje za odlišné), preto index s COALESCE:
CREATE UNIQUE INDEX IF NOT EXISTS idx_contact_unique
  ON contact_points(COALESCE(person_id, 0), COALESCE(org_id, 0), kind, value);

CREATE TABLE IF NOT EXISTS role_maps (
  hypothesis_id INTEGER PRIMARY KEY REFERENCES hypotheses(id),
  roles_json TEXT NOT NULL,                 -- [{category, examples, rationale}]
  edited_by_human INTEGER NOT NULL DEFAULT 0,
  low_agreement INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS role_fit_cache (
  hypothesis_id INTEGER NOT NULL REFERENCES hypotheses(id),
  title_key TEXT NOT NULL,                  -- normalizovaný názov funkcie
  score REAL NOT NULL, category TEXT, rationale TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (hypothesis_id, title_key)
);

CREATE TABLE IF NOT EXISTS person_problem_links (
  id INTEGER PRIMARY KEY,
  person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
  hypothesis_id INTEGER NOT NULL REFERENCES hypotheses(id),
  link_type TEXT NOT NULL,                  -- quoted_about_problem|role_owner|org_affected|procurement_contact|expert
  evidence_claim_id INTEGER REFERENCES claims(id),
  evidence_url TEXT, rationale TEXT,
  verdict TEXT,                             -- supported|weak|unsupported|undecided
  priority REAL, priority_json TEXT,        -- zložky priority
  created_at TEXT NOT NULL,
  UNIQUE (person_id, hypothesis_id, link_type)
);

CREATE TABLE IF NOT EXISTS outreach (
  id INTEGER PRIMARY KEY,
  person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
  hypothesis_id INTEGER NOT NULL REFERENCES hypotheses(id),
  contact_point_id INTEGER REFERENCES contact_points(id) ON DELETE SET NULL,
  channel TEXT NOT NULL,                    -- email|linkedin|phone
  variant TEXT NOT NULL,                    -- A|B
  subject TEXT, body TEXT NOT NULL,
  facts_json TEXT NOT NULL, checks_json TEXT,
  status TEXT NOT NULL DEFAULT 'draft',     -- R-OUT-8
  approved_at TEXT, sent_at TEXT, follow_up_due TEXT,
  follow_up_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS interviews (
  id INTEGER PRIMARY KEY,
  person_id INTEGER REFERENCES people(id) ON DELETE SET NULL,
  pseudonym TEXT NOT NULL,                  -- R1, R2…
  hypothesis_id INTEGER NOT NULL REFERENCES hypotheses(id),
  interviewee_kind TEXT NOT NULL,           -- customer|expert
  role_label TEXT, org_type TEXT,           -- funkcia a typ organizácie (do pseudonymizovaných správ)
  held_at TEXT NOT NULL,
  last_occurrence TEXT, current_workaround TEXT, cost_impact TEXT,
  decision_and_payer TEXT, tried_before TEXT,
  mentioned_unprompted INTEGER,             -- 1|0|NULL
  commitment TEXT,                          -- intro|follow_up|data|pilot|none
  notes TEXT,
  document_id INTEGER REFERENCES documents(id),  -- pseudo-dokument s poznámkami (R-CRM-2)
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS suppression (
  hash TEXT PRIMARY KEY,
  kind TEXT NOT NULL,                       -- email|phone|name_org|domain
  reason TEXT NOT NULL,                     -- objection|erasure|bounced|manual
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY,
  hypothesis_id INTEGER NOT NULL REFERENCES hypotheses(id),
  decision TEXT NOT NULL,                   -- continue|pivot|stop|pause
  rationale TEXT NOT NULL, metrics_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_judgments (
  id INTEGER PRIMARY KEY,
  task TEXT NOT NULL, object_type TEXT, object_id INTEGER,
  role TEXT NOT NULL, model TEXT, profile TEXT, prompt_version TEXT NOT NULL,
  params_json TEXT, output_json TEXT, verdict TEXT,
  prompt_tokens INTEGER, completion_tokens INTEGER, latency_s REAL,
  truncated INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_judgments_object ON model_judgments(task, object_type, object_id);

CREATE TABLE IF NOT EXISTS labels (
  id INTEGER PRIMARY KEY,
  task TEXT NOT NULL, object_type TEXT NOT NULL, object_id INTEGER NOT NULL,
  label TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL,
  UNIQUE (task, object_type, object_id)
);

CREATE TABLE IF NOT EXISTS review_queue (
  id INTEGER PRIMARY KEY,
  task TEXT NOT NULL, object_type TEXT NOT NULL, object_id INTEGER NOT NULL,
  reason TEXT NOT NULL,                     -- undecided|disagreement|truncated|low_agreement|sample|ambiguous
  detail_json TEXT,
  status TEXT NOT NULL DEFAULT 'open',      -- open|done
  created_at TEXT NOT NULL,
  UNIQUE (task, object_type, object_id)
);

CREATE TABLE IF NOT EXISTS query_stats (
  arm TEXT PRIMARY KEY,                     -- kategória zdroja alebo bunka matice dopytov
  tries INTEGER NOT NULL DEFAULT 0, successes INTEGER NOT NULL DEFAULT 0,
  last_used_at TEXT, cursor TEXT            -- pozícia rotácie matice
);

CREATE TABLE IF NOT EXISTS bench (
  id INTEGER PRIMARY KEY, role TEXT NOT NULL, model TEXT,
  load_s REAL, prompt_tps REAL, gen_tps REAL, json_ok INTEGER, tools_ok INTEGER,
  note TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS register_cache (  -- odpovede RPO/RÚZ bez osobných údajov (adresy štatutárov sa odstránia)
  url TEXT PRIMARY KEY,
  body TEXT NOT NULL,
  fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS eval_cases (     -- syntetické prípady pre eval (nastrčená chyba)
  id INTEGER PRIMARY KEY,
  task TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  source TEXT NOT NULL,                     -- synthetic | manual
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS eval_results (
  id INTEGER PRIMARY KEY,
  run_at TEXT NOT NULL,
  task TEXT NOT NULL, profile TEXT NOT NULL, model TEXT, thinking INTEGER,
  n INTEGER NOT NULL, metrics_json TEXT NOT NULL
);
"""

# ---------------------------------------------------------------- v3: testovanie hypotéz a úlohy z GUI
V3_COLUMNS = {
    "hypotheses": {
        "validation_state": "TEXT",               # NULL = neotestovaná | searched (agent hľadal, čaká sa na overenie) | tested
        "validation_started_at": "TEXT",
        "validated_at": "TEXT",
        "validation_json": "TEXT",                # verdikt kódu a jeho zložky (počty, domény, protidôkazy)
        "validation_runs": "INTEGER NOT NULL DEFAULT 0",
    },
    "claims": {
        "hypothesis_hint": "INTEGER",             # tvrdenie našiel agent pri testovaní tejto hypotézy (zaradí ho aj tak model)
    },
}

SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS jobs (           -- agentické workflowy spustené z GUI (beh na pozadí, iba lokálne)
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,                       -- scout | validate | people
  args_json TEXT NOT NULL,
  pid INTEGER,
  status TEXT NOT NULL,                     -- starting | running | done | failed | stopped | interrupted
  log_path TEXT,
  progress_json TEXT,
  stats_json TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_claims_hint ON claims(hypothesis_hint);
"""

# ---------------------------------------------------------------- v4: Brána 1 (dáta), Brána 2 (experimenty), nastavenia
V4_COLUMNS = {
    "hypotheses": {
        "gate1_status": "TEXT NOT NULL DEFAULT 'not_started'",  # not_started | running | ready | passed | paused | stopped
        "gate1_started_at": "TEXT",                              # prvý prieskum dát (lehota 6 týždňov na kľúčové pole)
        "gate1_json": "TEXT",                                    # pripravenosť dát: zložky, skóre, príznaky
        "gate2_status": "TEXT NOT NULL DEFAULT 'not_started'",  # not_started | running | passed | paused | stopped
    },
}

SCHEMA_V4 = """
CREATE TABLE IF NOT EXISTS settings (       -- nastavenia z GUI (napr. výber modelov); prepíšu config.yaml
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS data_fields (    -- Brána 1: polia potrebné na riešenie problému
  id INTEGER PRIMARY KEY,
  hypothesis_id INTEGER NOT NULL REFERENCES hypotheses(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  description TEXT,
  is_key INTEGER NOT NULL DEFAULT 0,
  rationale TEXT,
  status TEXT NOT NULL DEFAULT 'open',      -- open | found | partial | requested | partner | missing
  status_note TEXT,
  edited_by_human INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE (hypothesis_id, name)
);

CREATE TABLE IF NOT EXISTS data_sources (   -- zdroj poľa: licencia, frekvencia, prístup, kvalita (overené kódom)
  id INTEGER PRIMARY KEY,
  field_id INTEGER NOT NULL REFERENCES data_fields(id) ON DELETE CASCADE,
  hypothesis_id INTEGER NOT NULL,
  name TEXT NOT NULL, url TEXT NOT NULL, publisher TEXT,
  access TEXT NOT NULL DEFAULT 'unknown',   -- open | registration | request_211 | partner | paid | unknown
  license TEXT, update_frequency TEXT, formats TEXT,
  evidence_url TEXT, evidence_snippet TEXT,  -- stránka a doslovný úryvok, z ktorého sú licencia a frekvencia
  checks_json TEXT, quality_json TEXT,
  status TEXT NOT NULL DEFAULT 'candidate', -- candidate | verified | rejected
  origin TEXT NOT NULL,                     -- agent | catalog | manual
  created_at TEXT NOT NULL,
  UNIQUE (field_id, url)
);

CREATE TABLE IF NOT EXISTS datasets (       -- stiahnuté súbory (data/datasets, mimo gitu) a ich profil
  id INTEGER PRIMARY KEY,
  hypothesis_id INTEGER NOT NULL,
  source_id INTEGER REFERENCES data_sources(id) ON DELETE SET NULL,
  url TEXT NOT NULL, path TEXT, table_name TEXT NOT NULL, format TEXT,
  size_bytes INTEGER, sha256 TEXT, fetched_at TEXT NOT NULL,
  rows INTEGER, columns_json TEXT, profile_json TEXT,
  personal_data INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'ok',        -- ok | too_large | personal_data_blocked | error
  note TEXT,
  UNIQUE (hypothesis_id, url)
);

CREATE TABLE IF NOT EXISTS dataset_coverage (  -- ktoré organizácie (IČO) sú v datasete
  dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
  org_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  column_name TEXT NOT NULL, rows INTEGER NOT NULL,
  PRIMARY KEY (dataset_id, org_id)
);

CREATE TABLE IF NOT EXISTS reproductions (  -- Brána 1: zreprodukovať známe číslo zo zdroja (NKÚ, revízia výdavkov…)
  id INTEGER PRIMARY KEY,
  hypothesis_id INTEGER NOT NULL,
  claim_id INTEGER REFERENCES claims(id) ON DELETE SET NULL,
  target_value REAL, target_unit TEXT, target_text TEXT,
  dataset_ids TEXT, sql TEXT, explanation TEXT,
  result_value REAL, scale REAL, rel_error REAL,
  verdict TEXT NOT NULL,                    -- match | close | mismatch | failed
  note TEXT, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS info_requests (  -- žiadosti podľa zákona č. 211/2000 Z. z. (iba návrh; posiela človek)
  id INTEGER PRIMARY KEY,
  hypothesis_id INTEGER NOT NULL,
  field_id INTEGER REFERENCES data_fields(id) ON DELETE SET NULL,
  authority TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft',     -- draft | sent | answered | refused | partner
  sent_at TEXT, due_at TEXT, answered_at TEXT, note TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gate_decisions (  -- rozhodnutia Brány 1 a 2 s metrikami (rozhoduje človek)
  id INTEGER PRIMARY KEY,
  gate INTEGER NOT NULL,
  hypothesis_id INTEGER NOT NULL,
  org_id INTEGER,
  decision TEXT NOT NULL,                   -- passed | paused | stopped
  reason TEXT, metrics_json TEXT,
  decided_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experiments (    -- Brána 2: hypotéza × organizácia, predregistrácia, spätný test, demo
  id INTEGER PRIMARY KEY,
  hypothesis_id INTEGER NOT NULL,
  org_id INTEGER REFERENCES organizations(id) ON DELETE SET NULL,
  region TEXT,
  statement TEXT NOT NULL, metric TEXT NOT NULL, baseline TEXT NOT NULL, success_rule TEXT NOT NULL,
  spec_json TEXT NOT NULL,                  -- dataset, stĺpec času, hodnoty, entity, horizont
  prereg_hash TEXT, locked_at TEXT,
  status TEXT NOT NULL DEFAULT 'draft',     -- draft | locked | done | failed
  result_json TEXT, flags_json TEXT, demo_path TEXT, expert_json TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
"""

# ---------------------------------------------------------------- v5: typ vydavateľa zdroja
V5_COLUMNS = {
    "documents": {
        "source_type": "TEXT",                    # government | media | research | association | vendor | other
    },
}

SCHEMA_V5 = """
CREATE TABLE IF NOT EXISTS domain_types (   -- typ vydavateľa podľa domény (pravidlo alebo rýchly model, raz na doménu)
  domain TEXT PRIMARY KEY,
  source_type TEXT NOT NULL,
  method TEXT NOT NULL,                     -- rule | model | manual
  reason TEXT,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_domain ON documents(domain);
"""

V6_COLUMNS = {
    "hypotheses": {
        "theme_id": "INTEGER",                    # téma (zhluk podobných hypotéz); NULL = samostatná hypotéza
    },
}

SCHEMA_V6 = """
CREATE TABLE IF NOT EXISTS themes (         -- témy: zhluky podobných hypotéz podľa embeddingov; hypotézy sa nezlučujú
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  summary TEXT,
  name_method TEXT NOT NULL,                -- model | medoid (názov najtypickejšej hypotézy) | manual
  medoid_id INTEGER,                        -- najtypickejšia hypotéza témy
  size INTEGER NOT NULL,
  members_json TEXT NOT NULL,               -- ID hypotéz pri poslednom zostavení (zhoda tém medzi behmi)
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hypotheses_theme ON hypotheses(theme_id);
"""

SCHEMA_V7 = """
CREATE TABLE IF NOT EXISTS gate1_inputs (   -- dáta alebo schéma od človeka pre Bránu 1 (keď pripravenosť < prah)
  id INTEGER PRIMARY KEY,
  hypothesis_id INTEGER NOT NULL REFERENCES hypotheses(id),
  text TEXT NOT NULL,                       -- popis dát alebo schéma (voľný text)
  file_name TEXT,                           -- nahraný súbor (dataset v DuckDB), ak bol
  dataset_id INTEGER,
  status TEXT NOT NULL,                     -- pending | evaluated | failed
  score REAL,                               -- vhodnosť dát 0–1 (počíta kód z posúdenia polí)
  evaluation_json TEXT,
  created_at TEXT NOT NULL,
  evaluated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_gate1_inputs_h ON gate1_inputs(hypothesis_id, status);
"""

# ---------------------------------------------------------------- v8: problémové karty, podhypotézy, experimenty podľa úlohy
V8_COLUMNS = {
    "hypotheses": {
        "parent_id": "INTEGER",                  # podhypotéza vznikla rozdelením širokej hypotézy
        "split_at": "TEXT",                      # kedy sa hypotéza rozdelila (ostáva s všeobecnými citátmi)
    },
    "experiments": {
        "task": "TEXT",                          # ranking | forecast | anomaly | threshold | comparison (NULL = pôvodná predpoveď)
        "card_id": "INTEGER",                    # problémová karta, z ktorej experiment vychádza
        "problem_link": "TEXT",                  # ktoré rozhodnutie z karty výsledok podporí a ako
        "conclusion_json": "TEXT",               # záver pre problém (text z faktov, kontrola čísel kódom)
    },
}

SCHEMA_V8 = """
CREATE TABLE IF NOT EXISTS problem_cards (  -- problém, kto ho má, aké rozhodnutie robí, aký výstup pomôže (s citátmi)
  id INTEGER PRIMARY KEY,
  hypothesis_id INTEGER NOT NULL REFERENCES hypotheses(id),
  card_json TEXT NOT NULL,
  status TEXT NOT NULL,                     -- draft | confirmed | rejected
  model TEXT, prompt_version TEXT,
  created_at TEXT NOT NULL, confirmed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_problem_cards_h ON problem_cards(hypothesis_id, id);
"""


SCHEMA_V9 = """
CREATE TABLE IF NOT EXISTS catalog_datasets (  -- lokálny index Národného katalógu otvorených dát (metadáta, bez súborov)
  id INTEGER PRIMARY KEY,
  uri TEXT NOT NULL UNIQUE,
  title TEXT,
  description TEXT,
  publisher TEXT,
  modified TEXT,
  periodicity TEXT,
  landing TEXT,
  synced_at TEXT NOT NULL
);
"""


# ---------------------------------------------------------------- v10: hľadanie kontaktov podľa hypotézy
V10_COLUMNS = {
    "hypotheses": {
        "contacts_searched_at": "TEXT",          # posledné dokončené hľadanie kontaktov podľa hypotézy (contact-search)
        "contact_search_json": "TEXT",           # súhrn: kolá, dopyty, nájdené organizácie podľa typu cieľa
    },
    "org_hypotheses": {
        "target_id": "INTEGER",                  # cieľ plánu kontaktov, cez ktorý sa organizácia našla
    },
}

SCHEMA_V10 = """
CREATE TABLE IF NOT EXISTS contact_targets (  -- koho osloviť k hypotéze: zákazník, držiteľ dát, expert (plán modelu, upraviteľný)
  id INTEGER PRIMARY KEY,
  hypothesis_id INTEGER NOT NULL REFERENCES hypotheses(id),
  kind TEXT NOT NULL,                       -- customer | data_holder | expert
  who TEXT NOT NULL,                        -- typ organizácie a rola
  roles_json TEXT,                          -- funkcie, ako bývajú na weboch
  queries_json TEXT,                        -- [{q, done_at, results, orgs}]
  ask TEXT,                                 -- čo od nich žiadať (rozhovor, vzorka dát, posúdenie)
  why TEXT,
  round INTEGER NOT NULL DEFAULT 1,
  edited_by_human INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_contact_targets_h ON contact_targets(hypothesis_id, id);
"""

# ---------------------------------------------------------------- v11: lievik (jedna hypotéza po druhej)
SCHEMA_V11 = """
CREATE TABLE IF NOT EXISTS funnel_log (  -- kroky lievika: ktorá hypotéza, aký krok, výsledok a čo ďalej
  id INTEGER PRIMARY KEY,
  hypothesis_id INTEGER REFERENCES hypotheses(id),  -- NULL pri hľadaní nových hypotéz
  step TEXT NOT NULL,                       -- find | test | contacts | gate1 | gate2
  signature TEXT,                           -- stav hypotézy pred krokom (rovnaký stav opakovane = zaseknutá)
  outcome TEXT,                             -- výsledok krátko (verdikt, stav brány, počty)
  next_step TEXT,                           -- ďalší krok hypotézy po tomto (NULL = lievik pre ňu skončil alebo čaká)
  reason TEXT,                              -- prečo ďalej / prečo stop
  failed INTEGER NOT NULL DEFAULT 0,
  detail_json TEXT,
  job_id INTEGER,
  started_at TEXT NOT NULL, finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_funnel_h ON funnel_log(hypothesis_id, step);
"""

# ---------------------------------------------------------------- v12: dôveryhodnosť dát (audit 14. 10. 2026)
V12_COLUMNS = {
    "gate_decisions": {
        "decided_by": "TEXT",                    # rule (pravidlo prechodu) | human (človek)
        "data_fingerprint": "TEXT",              # odtlačok polí, väzieb a datasetov v čase rozhodnutia
    },
    "data_fields": {
        "availability": "TEXT",                  # public | internal (interné udalosti organizácie → partner, nie web)
    },
    "hypotheses": {
        "business_json": "TEXT",                 # obchodný stav: vlastník procesu, vzorka, úspora, pilot (zapisuje človek)
    },
}

SCHEMA_V12 = """
CREATE TABLE IF NOT EXISTS field_bindings (  -- väzba požiadavky na dáta: pole → verzia datasetu → stĺpec/výraz → význam
  id INTEGER PRIMARY KEY,
  field_id INTEGER NOT NULL REFERENCES data_fields(id) ON DELETE CASCADE,
  hypothesis_id INTEGER NOT NULL,
  dataset_id INTEGER REFERENCES datasets(id) ON DELETE SET NULL,
  dataset_sha TEXT,                         -- verzia datasetu (SHA-256 súboru), pre ktorú väzba platí
  table_name TEXT, expression TEXT,         -- stĺpec alebo výraz nad jednou tabuľkou
  unit TEXT, entity_kind TEXT, grain TEXT, meaning TEXT,
  evidence TEXT,                            -- čo podporuje význam (názov stĺpca, vzorka hodnôt, dokumentácia)
  checks_json TEXT,                         -- kontroly kódom: pokrytie, typ, vzorka, časové pokrytie, pojmy
  validation_status TEXT NOT NULL,          -- code_ok | code_failed | not_in_data | confirmed | rejected | stale
  proposed_by TEXT, confirmed_by TEXT, validated_at TEXT,
  inputs_fingerprint TEXT,                  -- odtlačok datasetov pri návrhu (nový návrh až pri zmene dát alebo parsera)
  note TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_field_bindings_f ON field_bindings(field_id, id);
CREATE INDEX IF NOT EXISTS idx_field_bindings_h ON field_bindings(hypothesis_id);
"""

# ---------------------------------------------------------------------------------- verzia 13
# Oslovenia podľa účelu (audit 15. 10. 2026): oslovenie aj organizácie cez funkčný kontakt (bez osoby), režim správy,
# návrh prínosu a reťazec „prečo → potreba → výstup → vstup“; doménové prehľady, návrhy prínosu, výsledky oslovení;
# cloudové modely (rezervácia a vyrovnanie nákladov, cache odpovedí).
OUTREACH_V13_COLUMNS = ("id, person_id, hypothesis_id, contact_point_id, channel, variant, subject, body, facts_json, "
                        "checks_json, status, approved_at, sent_at, follow_up_due, follow_up_count, created_at, updated_at")
SCHEMA_V13_OUTREACH = f"""
CREATE TABLE outreach_v13 (
  id INTEGER PRIMARY KEY,
  person_id INTEGER REFERENCES people(id) ON DELETE CASCADE,       -- NULL = organizácia cez funkčný kontakt
  org_id INTEGER REFERENCES organizations(id) ON DELETE CASCADE,
  hypothesis_id INTEGER NOT NULL REFERENCES hypotheses(id),
  contact_point_id INTEGER REFERENCES contact_points(id) ON DELETE SET NULL,
  channel TEXT NOT NULL,                    -- email|linkedin|phone
  variant TEXT NOT NULL,                    -- A|B
  subject TEXT, body TEXT NOT NULL,
  facts_json TEXT NOT NULL, checks_json TEXT,
  status TEXT NOT NULL DEFAULT 'draft',
  approved_at TEXT, sent_at TEXT, follow_up_due TEXT,
  follow_up_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  mode TEXT NOT NULL DEFAULT 'discovery',   -- discovery|data_request|expert_review|pilot
  proposal_id INTEGER,                      -- návrh prínosu (value_proposals)
  package_json TEXT,                        -- prečo tento človek → potreba → výstup → minimálny vstup, politika, stav
  readiness TEXT,                           -- ready | needs_input (zástupné polia, chýba zdroj dôvodu výberu)
  message_version INTEGER NOT NULL DEFAULT 1
);
INSERT INTO outreach_v13 ({OUTREACH_V13_COLUMNS}, org_id)
  SELECT {OUTREACH_V13_COLUMNS}, (SELECT p.org_id FROM people p WHERE p.id = outreach.person_id) FROM outreach;
DROP TABLE outreach;
ALTER TABLE outreach_v13 RENAME TO outreach;
CREATE INDEX IF NOT EXISTS idx_outreach_person ON outreach(person_id);
CREATE INDEX IF NOT EXISTS idx_outreach_org ON outreach(org_id);
"""

SCHEMA_V13 = """
CREATE TABLE IF NOT EXISTS domain_briefs (  -- päťminútový prehľad domény, zdieľaný kontaktmi domény, s verziami
  id INTEGER PRIMARY KEY,
  domain_key TEXT NOT NULL,                 -- theme:<id> alebo hypothesis:<id>
  title TEXT NOT NULL,
  version INTEGER NOT NULL,
  brief_json TEXT NOT NULL,                 -- proces, pojmy, roly, rozhodnutie, dnešný postup, dáta, otázky, neznáme
  sources_json TEXT NOT NULL,               -- citáty, z ktorých prehľad vznikol (claim_id, URL)
  inputs_hash TEXT NOT NULL,                -- podklady (tvrdenia, karta, odpovede); zmena = nová verzia
  checks_json TEXT,
  status TEXT NOT NULL,                     -- ok | needs_review | superseded
  model TEXT, reason TEXT,                  -- prečo nová verzia (napr. odpoveď kontaktu)
  created_at TEXT NOT NULL,
  UNIQUE (domain_key, version)
);
CREATE TABLE IF NOT EXISTS value_proposals (  -- návrh prínosu pre typ adresáta (hypotéza potreby → výstup → vstup)
  id INTEGER PRIMARY KEY,
  hypothesis_id INTEGER NOT NULL REFERENCES hypotheses(id) ON DELETE CASCADE,
  brief_id INTEGER REFERENCES domain_briefs(id) ON DELETE SET NULL,
  recipient_kind TEXT NOT NULL,             -- customer | data_holder | expert
  proposal_json TEXT NOT NULL,
  need_status TEXT NOT NULL DEFAULT 'hypothesis',  -- hypothesis | confirmed | refuted
  evidence_refs_json TEXT, checks_json TEXT,
  status TEXT NOT NULL,                     -- checked | needs_edit | pending_review | approved
  delivery_commitment_approved INTEGER NOT NULL DEFAULT 0,  -- konkrétny záväzok (rozsah, termín) potvrdil človek
  commercial_policy_version TEXT,
  inputs_hash TEXT, model TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_value_proposals_h ON value_proposals(hypothesis_id, recipient_kind);
CREATE TABLE IF NOT EXISTS outreach_outcomes (  -- výsledok oslovenia (osobné údaje: maže ho forget)
  id INTEGER PRIMARY KEY,
  outreach_id INTEGER REFERENCES outreach(id) ON DELETE SET NULL,
  person_id INTEGER REFERENCES people(id) ON DELETE CASCADE,
  org_id INTEGER REFERENCES organizations(id) ON DELETE SET NULL,
  hypothesis_id INTEGER NOT NULL,
  proposal_id INTEGER,
  message_version INTEGER,
  reply TEXT,                               -- none | relevant | redirect | not_relevant | declined
  problem_relevance TEXT,                   -- confirmed | refuted | unclear
  sample_obtained INTEGER NOT NULL DEFAULT 0, interview_agreed INTEGER NOT NULL DEFAULT 0,
  demo_done INTEGER NOT NULL DEFAULT 0, pilot_agreed INTEGER NOT NULL DEFAULT 0,
  value_shown INTEGER NOT NULL DEFAULT 0, paid INTEGER NOT NULL DEFAULT 0,
  rejection_reason TEXT, next_action TEXT, note TEXT,
  prep_minutes REAL, delivery_days REAL,
  recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outcomes_h ON outreach_outcomes(hypothesis_id);
CREATE TABLE IF NOT EXISTS llm_usage (  -- cloudové volania: rezervácia horného odhadu ceny pred volaním, vyrovnanie po
  id INTEGER PRIMARY KEY,
  provider TEXT NOT NULL, profile TEXT, deployment TEXT, task TEXT,
  status TEXT NOT NULL,                     -- reserved | settled | failed | uncertain | cached
  reserved_eur REAL NOT NULL DEFAULT 0, cost_eur REAL,
  prompt_tokens INTEGER, completion_tokens INTEGER, reasoning_tokens INTEGER,
  rate_json TEXT, note TEXT,
  created_at TEXT NOT NULL, settled_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_t ON llm_usage(created_at);
CREATE TABLE IF NOT EXISTS llm_cache (  -- odpoveď cloudového modelu podľa providera, nasadenia, promptu, parametrov a vstupu
  key TEXT PRIMARY KEY,
  provider TEXT, deployment TEXT, task TEXT,
  response TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""

# v14 (dodatok 16. 10. 2026): doménový výskum (stav, podklady, tvrdenia so stavom a rozsahom, otázky s blokovanou
# akciou), lokálny ESCO index, žiadosti o event log, importy event logov (udalosti sú v DuckDB organizácie), procesné
# reporty. Jedna migrácia pre celý dodatok.
SCHEMA_V14 = """
CREATE TABLE IF NOT EXISTS domain_research (  -- stav doménových podkladov: priorita, pokus, pokrytie, dôvod ukončenia
  domain_key TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 0,        -- 1 = v prioritnej dávke (poradie výpočtu, nie právo osloviť)
  priority_source TEXT,                       -- auto | manual | manual_off
  status TEXT NOT NULL DEFAULT 'not_requested', -- not_requested | queued | researching | ready | partial | needs_research
  input_hash TEXT,                            -- verzia vstupu posledného pokusu
  attempts INTEGER NOT NULL DEFAULT 0,        -- pokusy pre input_hash (limit domain_research.max_attempts_per_input_version)
  coverage_json TEXT,                         -- roly, proces, existujúce riešenia, význam dát, alternatívne vysvetlenia
  stop_reason TEXT, next_step TEXT,
  stats_json TEXT,                            -- dopyty, dokumenty, úryvky, volania modelu, čas
  requested_at TEXT, started_at TEXT, finished_at TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_evidence (  -- podklad domény (ESCO, web, dokumentácia, web organizácie, dataset)
  id INTEGER PRIMARY KEY,
  domain_key TEXT NOT NULL,
  source_kind TEXT NOT NULL,                  -- esco | web | documentation | organization | dataset
  source_url TEXT, local_reference TEXT,      -- URL alebo lokálny odkaz (esco:<uri>)
  title TEXT, excerpt TEXT NOT NULL, source_locator TEXT,
  retrieved_at TEXT NOT NULL, source_version TEXT, language TEXT, jurisdiction TEXT,
  content_hash TEXT NOT NULL,
  scope TEXT NOT NULL,                        -- occupation | sector | organization | dataset
  organization_id INTEGER REFERENCES organizations(id) ON DELETE SET NULL,
  document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
  created_at TEXT NOT NULL,
  UNIQUE (domain_key, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_domain_evidence_key ON domain_evidence(domain_key);
CREATE TABLE IF NOT EXISTS domain_claims (  -- tvrdenie prehľadu domény so stavom a rozsahom platnosti
  id INTEGER PRIMARY KEY,
  domain_key TEXT NOT NULL,
  part TEXT NOT NULL,                         -- roles | process | solutions | data | alternatives | decision | terms
  text TEXT NOT NULL,
  evidence_json TEXT NOT NULL,                -- {"claims": [id tvrdení hypotéz], "evidence": [id domain_evidence]}
  status TEXT NOT NULL,                       -- source_fact | hypothesis | contact_reported | data_observed | contradicted
  scope TEXT,                                 -- occupation | sector | organization | dataset
  applicability TEXT,                         -- na koho platí (napr. „všeobecne pre povolanie, nie fakt o firme“)
  organization_id INTEGER REFERENCES organizations(id) ON DELETE SET NULL,
  reasoning_note TEXT,
  brief_id INTEGER REFERENCES domain_briefs(id) ON DELETE SET NULL,
  active INTEGER NOT NULL DEFAULT 1,          -- 0 = nahradené novou verziou prehľadu
  history_json TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_domain_claims_key ON domain_claims(domain_key, active);
CREATE TABLE IF NOT EXISTS domain_questions (  -- otvorená otázka domény: čo blokuje, komu ju položiť, odpoveď s históriou
  id INTEGER PRIMARY KEY,
  domain_key TEXT NOT NULL,
  question TEXT NOT NULL,
  norm TEXT NOT NULL,                         -- normalizovaný text (zhoda otázky medzi verziami prehľadu)
  kind TEXT NOT NULL,                         -- falsifying | open
  part TEXT,                                  -- časť prehľadu, ktorej sa týka (roles | process | solutions | data | alternatives)
  aspect TEXT,                                -- problem_exists | already_solved | data_enables
  unknown_claim_id INTEGER REFERENCES domain_claims(id) ON DELETE SET NULL,
  target_role TEXT,                           -- customer | data_holder | expert | any
  blocks_action TEXT NOT NULL DEFAULT 'none', -- none | contact_choice | data_request | duration_analysis | prediction | quantified_savings_claim
  priority INTEGER NOT NULL DEFAULT 2,        -- 1 vysoká … 3 nízka
  status TEXT NOT NULL DEFAULT 'open',        -- open | answered | superseded
  answer TEXT, answer_source TEXT, answered_at TEXT,
  outcome_id INTEGER REFERENCES outreach_outcomes(id) ON DELETE SET NULL,
  brief_id INTEGER REFERENCES domain_briefs(id) ON DELETE SET NULL,
  history_json TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_domain_questions_key ON domain_questions(domain_key, status);
CREATE TABLE IF NOT EXISTS role_mappings (  -- rola kontaktu → kandidátne povolania (ESCO); nejednoznačné ostanú kandidátmi
  id INTEGER PRIMARY KEY,
  domain_key TEXT NOT NULL,
  role_label TEXT NOT NULL,
  candidates_json TEXT NOT NULL,              -- [{uri, label, score, basis}]
  chosen_uri TEXT, chosen_by TEXT,            -- človek | pravidlo (iba jednoznačná zhoda názvu)
  mapping_basis TEXT, source_version TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE (domain_key, role_label)
);
CREATE TABLE IF NOT EXISTS esco_concepts (  -- lokálny ESCO balík (stabilné URI, jazyk, verzia)
  uri TEXT NOT NULL,
  lang TEXT NOT NULL,
  kind TEXT NOT NULL,                         -- occupation | skill
  pref_label TEXT NOT NULL,
  alt_labels TEXT, description TEXT, isco_group TEXT, code TEXT,
  norm TEXT NOT NULL,                         -- normalizovaný text na fulltext (bez diakritiky)
  version TEXT NOT NULL,
  PRIMARY KEY (uri, lang)
);
CREATE TABLE IF NOT EXISTS esco_relations (  -- povolanie → zručnosť/znalosť
  occupation_uri TEXT NOT NULL, skill_uri TEXT NOT NULL,
  relation TEXT NOT NULL,                     -- essential | optional
  version TEXT NOT NULL,
  PRIMARY KEY (occupation_uri, skill_uri)
);
CREATE TABLE IF NOT EXISTS esco_imports (
  id INTEGER PRIMARY KEY,
  version TEXT NOT NULL, lang TEXT NOT NULL, license TEXT, source TEXT,
  files_json TEXT, counts_json TEXT,
  imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eventlog_requests (  -- žiadosť o event log (dvojstupňová) alebo alternatívny podklad
  id INTEGER PRIMARY KEY,
  hypothesis_id INTEGER REFERENCES hypotheses(id) ON DELETE CASCADE,
  organization_id INTEGER REFERENCES organizations(id) ON DELETE CASCADE,
  outreach_id INTEGER REFERENCES outreach(id) ON DELETE SET NULL,
  step INTEGER NOT NULL DEFAULT 1,            -- 1 = slovník + malá ukážka, 2 = väčší export po dohode
  process_scope TEXT, case_definition TEXT NOT NULL,
  fields_json TEXT NOT NULL, sample_scope TEXT NOT NULL, intended_output TEXT NOT NULL,
  why_event_log TEXT, unknowns_json TEXT,
  status TEXT NOT NULL DEFAULT 'draft',       -- draft | sent | received | not_available | alternative | declined
  alternative TEXT,                           -- walkthrough | state_schema | aggregate_report | manual_logging
  text TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eventlog_imports (  -- import event logu do priestoru organizácie (DuckDB organizácie)
  id INTEGER PRIMARY KEY,
  organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  request_id INTEGER REFERENCES eventlog_requests(id) ON DELETE SET NULL,
  hypothesis_id INTEGER REFERENCES hypotheses(id) ON DELETE SET NULL,
  file_name TEXT, file_hash TEXT NOT NULL, file_format TEXT,
  data_kind TEXT NOT NULL DEFAULT 'event_log', -- event_log | snapshot (snapshot sa nespracuje ako história)
  columns_json TEXT,                          -- stĺpce a typy surového súboru
  mapping_json TEXT, mapping_version INTEGER NOT NULL DEFAULT 0, mapping_confirmed INTEGER NOT NULL DEFAULT 0,
  timezone_policy TEXT,                       -- napr. offset v dátach | Europe/Bratislava (dokumentované) | neznáma
  case_definition TEXT, period TEXT,
  transformations_json TEXT, quality_json TEXT, availability_json TEXT,
  rows_total INTEGER, retained_rows INTEGER, quarantined_rows INTEGER,
  status TEXT NOT NULL,                       -- uploaded | mapped | checked | failed
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE (organization_id, file_hash)
);
CREATE TABLE IF NOT EXISTS process_reports (  -- procesný report z importu (iba podporované metriky, menovatele, obmedzenia)
  id INTEGER PRIMARY KEY,
  import_id INTEGER NOT NULL REFERENCES eventlog_imports(id) ON DELETE CASCADE,
  analysis_version TEXT NOT NULL, mapping_version INTEGER,
  supported_json TEXT NOT NULL, results_json TEXT NOT NULL, denominators_json TEXT,
  limitations_json TEXT, followups_json TEXT,
  review_json TEXT,
  status TEXT NOT NULL,                       -- ok | pending_review | needs_review
  created_at TEXT NOT NULL
);
"""
ESCO_FTS = "CREATE VIRTUAL TABLE IF NOT EXISTS esco_fts USING fts5(uri UNINDEXED, lang UNINDEXED, norm)"

# v15 (zadanie 17. 10. 2026 – od doménového prehľadu ku konkrétnej ponuke): posúdenie použiteľnosti dôkazu voči cieľu
# (doména alebo karta organizácie), karta organizácie s verziou a výskumné úlohy z medzier karty. Pridávanie stĺpcov
# a tabuliek; staré prehľady, návrhy, poznámky a odpovede ostávajú.
SCHEMA_V15 = """
CREATE TABLE IF NOT EXISTS evidence_assessments (  -- použiteľnosť zdroja voči konkrétnemu cieľu (metadáta zdroja ostávajú v claims/documents)
  id INTEGER PRIMARY KEY,
  target_key TEXT NOT NULL,                   -- domain:<kľúč> | card:<org>:<hypotéza>:<proces>:<rola>
  claim_id INTEGER REFERENCES claims(id) ON DELETE CASCADE,
  domain_evidence_id INTEGER REFERENCES domain_evidence(id) ON DELETE CASCADE,
  organization_id INTEGER REFERENCES organizations(id) ON DELETE SET NULL,  -- o kom zdroj hovorí (ak je známe)
  publisher_type TEXT,                        -- government | media | research | association | vendor | other
  meaning TEXT NOT NULL,                      -- problem | process | existing_solution | product_feature | post_deployment | counterargument | context
  relevance TEXT NOT NULL,                    -- direct | analogy | context | unrelated | unclear
  relevance_reason TEXT,
  need_state TEXT NOT NULL DEFAULT 'unknown', -- current_confirmed | historical | solved_per_source | unknown
  process TEXT, role TEXT, program TEXT, jurisdiction TEXT,
  published_at TEXT, event_period TEXT, retrieved_at TEXT,  -- samostatné dátumy; neznáme ostávajú prázdne
  assessed_by TEXT NOT NULL,                  -- code | model | human
  model TEXT, logic_version TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (target_key, claim_id, domain_evidence_id, logic_version)
);
CREATE INDEX IF NOT EXISTS idx_assess_target ON evidence_assessments(target_key);
CREATE TABLE IF NOT EXISTS org_cards (         -- karta organizácie: organizácia + hypotéza + pracovný prípad + rola adresáta
  id INTEGER PRIMARY KEY,
  organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  hypothesis_id INTEGER NOT NULL REFERENCES hypotheses(id) ON DELETE CASCADE,
  process_key TEXT NOT NULL,                  -- krátky kľúč pracovného prípadu (jedna organizácia môže mať viac kariet)
  recipient_role TEXT NOT NULL,               -- customer | data_holder | expert (+ voliteľná funkcia v card_json)
  version INTEGER NOT NULL,
  status TEXT NOT NULL,                       -- draft | checked | needs_review | pending_review | stale | superseded | edited
  card_json TEXT NOT NULL, sources_json TEXT, checks_json TEXT, readiness_json TEXT, evaluation_json TEXT,
  selected_option INTEGER,                    -- vybraná možnosť riešenia / skúška (človek)
  brief_id INTEGER REFERENCES domain_briefs(id) ON DELETE SET NULL,
  inputs_hash TEXT NOT NULL, schema_version TEXT, prompt_version TEXT, policy_version TEXT,
  provider TEXT, model TEXT,
  stale_reason TEXT, conflict_json TEXT,       -- nový rozpor (napr. odpoveď po schválení) – zobrazí sa aj pri schválenej karte
  approved_at TEXT, author TEXT NOT NULL DEFAULT 'model',  -- model | human (ručná oprava = nová verzia)
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_org_cards ON org_cards(organization_id, hypothesis_id, process_key, recipient_role, version);
CREATE TABLE IF NOT EXISTS research_tasks (    -- cielené doplnenie medzery karty (otázka → zdroj → výsledok)
  id INTEGER PRIMARY KEY,
  card_id INTEGER NOT NULL REFERENCES org_cards(id) ON DELETE CASCADE,
  question TEXT NOT NULL, gap_part TEXT,
  decision_changed TEXT NOT NULL,             -- recipient | solution | trial
  best_source TEXT NOT NULL,                  -- public_doc | tool_docs | contact | sample | event_log
  queries_json TEXT,
  status TEXT NOT NULL DEFAULT 'open',        -- open | found | partial | conflict | not_public | unavailable
  result_json TEXT, sources_json TEXT, next_step TEXT,
  priority INTEGER NOT NULL DEFAULT 2, attempts INTEGER NOT NULL DEFAULT 0, cost_json TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_research_tasks_card ON research_tasks(card_id);
"""
# ---------------------------------------------------------------- v16: obstarávanie z TED (M37)
SCHEMA_V16 = """
CREATE TABLE IF NOT EXISTS procurement_notices (  -- oznámenie o obstarávaní (TED); bez osôb, e-mailov a adries
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL DEFAULT 'ted',
  notice_id TEXT NOT NULL,                    -- číslo publikácie, napr. 570007-2025
  notice_type TEXT, procedure_type TEXT, contract_nature TEXT,
  stage TEXT NOT NULL,                        -- planned | call | awarded | modified | other (z typu oznámenia)
  title TEXT, description TEXT,
  buyer_name TEXT, buyer_ico TEXT, buyer_legal_type TEXT, buyer_country TEXT,
  cpv_json TEXT, main_cpv TEXT,
  publication_date TEXT, deadline TEXT,
  estimated_value REAL, estimated_currency TEXT, awarded_value REAL, awarded_currency TEXT,
  winners_json TEXT,                          -- [{"ico", "name" (iba právnická osoba), "size"}]
  url TEXT NOT NULL, raw_hash TEXT NOT NULL, fetched_at TEXT NOT NULL,
  UNIQUE (source, notice_id)
);
CREATE INDEX IF NOT EXISTS idx_procurement_buyer ON procurement_notices(buyer_ico);
CREATE TABLE IF NOT EXISTS procurement_matches (  -- oznámenie podobné hypotéze (embeddingy); podobnosť sa zobrazuje
  hypothesis_id INTEGER NOT NULL REFERENCES hypotheses(id) ON DELETE CASCADE,
  notice_id INTEGER NOT NULL REFERENCES procurement_notices(id) ON DELETE CASCADE,
  similarity REAL NOT NULL,
  query_hash TEXT NOT NULL,                   -- text dopytu hypotézy (názov + navrhnutý produkt); zmena → nové priradenie
  matched_at TEXT NOT NULL,
  PRIMARY KEY (hypothesis_id, notice_id)
);
"""

# ---------------------------------------------------------------- v17: obchodné stavy a ochota platiť (M35)
SCHEMA_V17 = """
CREATE TABLE IF NOT EXISTS commercial_evidence (  -- obchodný dôkaz o organizácii (zapisuje človek; nie osobný údaj)
  id INTEGER PRIMARY KEY,
  hypothesis_id INTEGER NOT NULL REFERENCES hypotheses(id) ON DELETE CASCADE,
  org_id INTEGER REFERENCES organizations(id) ON DELETE SET NULL,
  person_id INTEGER REFERENCES people(id) ON DELETE SET NULL,          -- voliteľné; forget vymaže väzbu aj text dôkazu
  outcome_id INTEGER REFERENCES outreach_outcomes(id) ON DELETE SET NULL,
  interview_id INTEGER REFERENCES interviews(id) ON DELETE SET NULL,
  stage TEXT NOT NULL,          -- buyer_identified | budget_verified | purchase_intent | payment_received | no_willingness
  evidence TEXT NOT NULL,       -- kto, čo, kedy
  conditions TEXT,              -- záujem o kúpu: čo, v akom rozsahu a za akých podmienok
  amount REAL, currency TEXT,   -- suma (záujem: navrhnutá / platba: prijatá)
  billing TEXT,                 -- one_off | monthly | annual
  contract_value REAL,          -- celková hodnota zmluvy, ak je známa
  status TEXT NOT NULL DEFAULT 'active',  -- active | withdrawn
  recorded_by TEXT NOT NULL DEFAULT 'human',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_commercial_h ON commercial_evidence(hypothesis_id, org_id);
"""
# ---------------------------------------------------------------- v18: zákazka = postup, posúdenie zhody modelom
SCHEMA_V18 = """
CREATE INDEX IF NOT EXISTS idx_procurement_procedure ON procurement_notices(procedure_id);
CREATE TABLE IF NOT EXISTS procurement_judgments (  -- posúdenie kandidáta modelom (cache; nové iba pri zmene dopytu)
  hypothesis_id INTEGER NOT NULL REFERENCES hypotheses(id) ON DELETE CASCADE,
  procedure_key TEXT NOT NULL,                -- identifikátor postupu TED (inak kupujúci + predmet)
  query_hash TEXT NOT NULL,                   -- text dopytu hypotézy (problém + navrhnutý produkt)
  verdict TEXT NOT NULL,                      -- yes | partial | no | undecided
  reason TEXT, model TEXT, judged_at TEXT NOT NULL,
  PRIMARY KEY (hypothesis_id, procedure_key, query_hash)
);
"""
# ---------------------------------------------------------------- v19: pošta (M41)
SCHEMA_V19 = """
CREATE TABLE IF NOT EXISTS mail_messages (  -- správa z pošty spárovaná s oslovením (iba metadáta, bez tela e-mailu)
  id INTEGER PRIMARY KEY,
  message_id TEXT NOT NULL UNIQUE,            -- hlavička Message-ID
  thread_id TEXT,                             -- vlákno Gmailu (X-GM-THRID)
  outreach_id INTEGER REFERENCES outreach(id) ON DELETE CASCADE,
  person_id INTEGER REFERENCES people(id) ON DELETE CASCADE,
  direction TEXT NOT NULL,                    -- out | in
  kind TEXT NOT NULL,                         -- sent | follow_up | reply | auto_reply | bounce
  sent_at TEXT, subject TEXT,                 -- predmet maskovaný
  status TEXT NOT NULL,                       -- recorded | new | extracted | applied | dismissed | failed
  proposal_json TEXT,                         -- návrh z odpovede (lokálny model + kontrola citácií kódom)
  outcome_id INTEGER REFERENCES outreach_outcomes(id) ON DELETE SET NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mail_outreach ON mail_messages(outreach_id);
"""

V20_COLUMNS = {
    "org_hypotheses": {"people_version": "INTEGER NOT NULL DEFAULT 0"},  # verzia pravidiel zberu ľudí (M42)
}

V18_COLUMNS = {
    "procurement_notices": {"procedure_id": "TEXT"},  # oznámenia jednej zákazky (výzva, zmeny, výsledok) majú spoločný
    "procurement_matches": {"procedure_key": "TEXT", "verdict": "TEXT", "reason": "TEXT"},
}

V17_COLUMNS = {
    "outreach_outcomes": {"wtp_stage": "TEXT"},  # posúdenie ochoty platiť pri výsledku (aj not_asked / unclear)
}

V15_COLUMNS = {
    "value_proposals": [("card_id", "INTEGER REFERENCES org_cards(id) ON DELETE SET NULL"), ("card_version", "INTEGER"),
                        ("recipient_role", "TEXT"), ("stale_reason", "TEXT")],
    "outreach_outcomes": [("card_id", "INTEGER REFERENCES org_cards(id) ON DELETE SET NULL"), ("card_version", "INTEGER"),
                          ("question_id", "INTEGER"), ("team_role", "TEXT"), ("last_case", "TEXT"), ("frequency", "TEXT"),
                          ("tool_used", "TEXT"), ("unsolved_part", "TEXT"), ("process_owner", "TEXT"),
                          ("buying_process", "TEXT"), ("data_availability", "TEXT"), ("decline_reason", "TEXT"),
                          ("needs_human_review", "INTEGER NOT NULL DEFAULT 0"), ("source", "TEXT")],
    "org_hypotheses": [("procurement_status", "TEXT"), ("procurement_subject", "TEXT"), ("procurement_date", "TEXT"),
                       ("procurement_url", "TEXT"), ("procurement_checked_at", "TEXT"), ("segment_fit_json", "TEXT")],
    "eventlog_requests": [("card_id", "INTEGER REFERENCES org_cards(id) ON DELETE SET NULL")],
    "outreach": [("card_id", "INTEGER"), ("card_version", "INTEGER")],
}
