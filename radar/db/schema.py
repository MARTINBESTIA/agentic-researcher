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
