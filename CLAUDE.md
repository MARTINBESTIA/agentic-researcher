# Problem Radar – pravidlá pre Claude Code

Lokálny systém, ktorý hľadá podnikateľské problémy (verejné dáta SR, priemysel, verejná správa),
dokladá ich doslovnými citátmi a pripravuje rozhovory **Brány 0** (customer discovery: overiť
problém rozhovormi skôr, než sa začne stavať produkt). Beží na notebooku používateľa; predvolene bez platených API.
Voliteľne smie vybraná rola (napr. hodnotiteľ `reviewer`) bežať v Azure OpenAI s rozpočtom používateľa a iba pre úlohy
z `cloud.allowed_tasks` (`radar/cloud.py`, README → Cloudové modely).

- Hardvér: Windows 11, RTX 5070 Laptop (8 GB VRAM), 32 GB RAM, Intel Core 7 240H. Naraz sa zmestí
  iba jeden veľký model.
- Lokálne služby: Ollama na `127.0.0.1:11434` (roly fast/accurate/embed; alternatíva llama-swap na `127.0.0.1:8080`),
  SearXNG (Docker) na `127.0.0.1:8888`.
- Používateľ je informatik (Ing. štúdium), nie strojár. Pri doménových pojmoch pridaj krátke vysvetlenie.
- Jazyk: s používateľom, v CLI, GUI a správach po slovensky. Identifikátory v kóde po anglicky,
  docstringy a komentáre po slovensky (ako doteraz).

**Stav:** verzia 0.2 „Brána 0 agregátor“ podľa `docs/SPEC_brana0.md` je implementovaná (M1–M8,
11. 10. 2026), rozšírená o Správcu akcií a výber modelov v GUI, LinkedIn a kontakty k osobe, Brány 1 a 2
(DuckDB; schéma v9 – vstupy človeka pre Bránu 1, problémové karty, podhypotézy, experimenty podľa úlohy, index katalógu) – M9–M13 v `docs/ACCEPTANCE.md`;
hľadanie kontaktov podľa hypotézy a „Koho osloviť“ v Bráne 0 (schéma v10, M18); lievik „hypotéza po hypotéze“
od testu po Bránu 2, donekonečna (schéma v11, M19); dôvera v dáta Brány 1 podľa auditu 14. 10. 2026 – overenie súborov
požiadavkou, import podľa obsahu s kontrolou, väzby pole → stĺpec, pravidlo prechodu s potvrdením človeka, preverenie
prešlých (schéma v12, M20); experimenty bez úniku, so silným porovnaním a štyrmi osami výsledku (M21); priority autopilota, účel kontaktov,
osi stavu, postup k zákazníkovi a referenčná vzorka (M22); podľa auditu 15. 10. 2026 fronta kontaktov so stavom komunikácie
(schéma v13, M23), prehľad domény a návrh prínosu (M24), návrhy oslovení pre zákazníka, držiteľa dát, experta a pilot
s obchodnou politikou (M25), Azure OpenAI pre vybrané roly s nasadeniami v GUI a rozpočtom (M26), výsledky oslovení
a učenie z odpovedí po organizáciách (M27); podľa dodatku 16. 10. 2026 (doménové vyhľadávanie a event log) stavy
doménových podkladov, priority domén, tvrdenia a otázky s blokovanou akciou, pripravenosť oslovenia (schéma v14, M28);
lokálny ESCO index, fulltext, rola → povolanie a ohraničený doménový výskum (M29).
Stav dodatku: `docs/STAV_domenovy_search_event_log.md`. Nápady na neskôr zapisuj do `improvements_logs.md`. Rozhodnutia a odchýlky od špecifikácie sú v
`docs/DECISIONS.md`. Pred zmenou si prečítaj príslušnú kapitolu špecifikácie a kód, ktorý budeš meniť.

## Príkazy

```
python -m venv .venv
.venv\Scripts\activate                   # PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m pytest -q                      # musí prejsť offline: bez modelu, bez internetu
python -m radar --help
python -m radar doctor                   # model, JSON, nástroje, SearXNG
python -m radar doctor --bench          # + rýchlosť rolí (fast, accurate, embed)
python -m radar run-all --minutes 480    # celý beh v poradí podľa modelov + reports/latest.md
python -m radar gui                      # GUI na 127.0.0.1:8501
docker compose up -d                     # SearXNG
```

## Mapa kódu (v0.2)

| Súbor | Úloha |
|---|---|
| `radar/config.py` | pydantic konfigurácia; `config.yaml` obsahuje len zmeny voči predvoleným hodnotám; `models` (roly, routing úloh, profily), `discovery`, `people`, `outreach`, `privacy`, `gui` |
| `radar/db/` | SQLite, jediné miesto s SQL: `evidence.py` (dokumenty, tvrdenia, hypotézy), `gate0.py` (organizácie, ľudia, kontakty, väzby, oslovenia, rozhovory, rozhodnutia, potlačenie), `ops.py` (model_judgments, review_queue, labels, bench, query_stats, cache registrov, eval, úlohy), `gates.py` (Brány 1 a 2, väzby polí, nastavenia; plán kontaktov je v `gate0.py`, záznam lievika v `ops.py`), `value.py` (prehľady domén, návrhy prínosu, výsledky oslovení, `llm_usage` a `llm_cache`), `domain.py` (stav a podklady domén, tvrdenia, otázky, mapovanie rolí, ESCO), `dossier.py` (čítanie pre dosiér), `schema.py` + `migrations.py` (PRAGMA user_version, záloha pred migráciou) |
| `radar/models.py` | `ModelRouter`: úloha → rola (fast/accurate/judge/reviewer/agent), uvažovanie, k hlasov; `run_task` (undecided, kontrola, provenance), `run_samples`; pri výmene modelu uvoľní predošlý z GPU; rola u cloudového poskytovateľa → `CloudLLM`, ochrana súkromia (`cloud_allowed`), cache odpovedí, dôvod prerušenia `cloud_pause` |
| `radar/cloud.py` | Azure OpenAI v1: `CloudLLM` (názov nasadenia v `model`, parametre podľa schopností, triedenie chýb, obmedzené opakovanie, JSON fallback iba pri nepodpore formátu), `CloudBudget` (rezervácia pred volaním, vyrovnanie, neistý timeout; limity celkom/deň/beh), kľúč cache, nasadenia (čítanie cez ARM, ručné pridanie, overené schopnosti), test pripojenia, nastavenia z GUI bez tajomstiev |
| `radar/llm.py` | klient OpenAI-kompatibilného servera (Ollama); JSON podľa schémy; `LLMTruncated` pri orezaní; `make_exclusive` |
| `radar/prompts.py` | všetky prompty s verziou (`TASK_PROMPTS`, `prompt_version`); inštrukcie po anglicky, výstup v jazyku `{lang}` |
| `radar/search.py`, `radar/fetch.py` | SearXNG; sťahovanie s robots.txt, pauzami a blokovanými doménami aj po presmerovaní; `fetch_html`; `probe` (overenie súboru s dátami: začiatok obsahu, HTML namiesto dát) |
| `radar/collect.py`, `radar/discover.py` | zber k témam; objavovanie (sources.yaml, matica dopytov, Thompson sampling) |
| `radar/extract.py` → `verify.py` → `cluster.py` → `critic.py` → `market.py` → `report.py` | pipeline dôkazov (screen, kontrola kódom → entail, kandidáti a novosť, správa so sekciami Brány 0) |
| `radar/bindings.py` | väzba poľa Brány 1: pole → verzia datasetu → stĺpec/výraz → jednotka → význam → dôkaz; návrh presným modelom (`field_binding`), kontroly kódom (pokrytie, typ, pojmová poistka), potvrdenie človekom, podmienky prechodu (`key_conditions`), odtlačok dát rozhodnutia, smerovanie poľa |
| `radar/gate1.py`, `radar/gate2.py`, `radar/datasets.py` | Brána 1 (polia s dostupnosťou, zdroje overené požiadavkou, DuckDB, reprodukcia, infozákon, pripravenosť; rebrík stavu poľa; pravidlo prechodu ≥ 0,5 a kľúčové polia s potvrdeným stĺpcom, inak „čaká na tvoje dáta“; `needs_revalidation` a autor rozhodnutia; kontrola importu človekom) a Brána 2 (predregistrácia, spätný test, demo); import podľa obsahu (HTML odmietnuté, SpreadsheetML, RDF/JSON-LD kocky, hlavička výkazu, odmietnuté riadky, importný report, `PARSER_VERSION`, pôvodný súbor podľa SHA-256); DuckDB iba na čítanie bez externého prístupu |
| `radar/cards.py` | problémová karta (problém, kto, rozhodnutie, výstup, metrika – s citátmi, sudca) a rozdelenie širokej hypotézy na podhypotézy |
| `radar/exp_ops.py`, `radar/exp_plan.py`, `radar/exp_tasks.py`, `radar/exp_conclude.py` | univerzálne experimenty Brány 2: operácie nad dátami (DuckDB, bez únikov budúcich dát; dostupnosť štítka z inej tabuľky a jeho dozretie), návrh modelom + skúšobný beh + metodické kontroly (jednotka štítka, entita, populácia, dostupnosť), testy podľa úlohy (poradie bez úniku po foldoch, predpoveď, anomália, prah, porovnanie) s výsledkovou tabuľkou, silné porovnanie zmrazené na validácii, štyri osi a verdikt, verzie; záver pre problém |
| `radar/gate2_trace.py` | postup experimentu Brány 2 krok po kroku (výber organizácie, návrh, uzamknutie, SQL a rad, kolá testu, výber auto, výpočet, demo) – GUI a Markdown |
| `radar/autopilot.py` | „Nechať bežať všetko“: kolá akcií – najprv tvoje dáta a vetvy prioritných hypotéz (najviac 3), nový zber s vyhradeným podielom, starnutie preskočených akcií; každá akcia samostatná úloha |
| `radar/progress.py` | osi stavu hypotézy (literatúra, zákazník, dáta, ochota platiť), obchodný stav so zápisom človeka a dôkazom, postup k zákazníkovi (prevádzkové metriky zvlášť) |
| `radar/funnel.py` | lievik (akcia `funnel`): ďalší krok hypotézy podľa stavu (test → kontakty → Brána 1 → Brána 2), výber (fokus, rozpracované, fronta testu, hľadanie nových), odloženie zaseknutých, jeden krok na úlohu a ďalšia úloha do poradia; záznam `funnel_log` |
| `radar/nku.py` | NKÚ: otvorené dáta kontrolných zistení + zoznam subjektov (IČO), bez adries – štítky pre experimenty |
| `radar/eta.py` | odhady časov pre dashboard (história akcií, poradie, miestny čas) |
| `radar/dossier.py` | dosiér hypotézy: všetko k hypotéze (aj rodič a podhypotézy) v ZIP; predvolene pseudonymizovaný (`Scrubber`), s menami a kontaktmi iba na voľbu so záznamom do poznámok |
| `radar/crz.py` | Centrálny register zmlúv: denné dávky, vybrané stĺpce bez mien a adries, zlúčenie podľa ID |
| `radar/catalogs.py` | Národný katalóg otvorených dát (SPARQL): hľadanie datasetov, metadáta ako prečítaná stránka, súbory z distribúcií; lokálny index (schéma v9, vektor na sériu datasetov) a hľadanie podľa významu |
| `radar/themes.py` | témy: zhluky podobných hypotéz (average linkage nad embeddingmi), názov z rýchleho modelu, stabilné ID medzi behmi; hypotézy sa nezlučujú |
| `radar/publishers.py` | typ vydavateľa zdroja (pravidlá podľa domény, model raz na doménu); dodávatelia sa nepočítajú do sily dôkazov |
| `radar/contact_scout.py` | hľadanie kontaktov podľa hypotézy (krok `contact-search`): plán cieľov rýchlym modelom (zákazník, držiteľ dát, expert – funkcie, dopyty, čo žiadať; tabuľka `contact_targets`), dopyty a overenie webu cez IČO kódom, ďalšie kolo so spätnou väzbou; držitelia dát z Brány 1 a kontaktný bod datasetu z katalógu; ručné pridanie kontaktu zo stránky (`contacts-add`) |
| `radar/shortlist.py` | Koho osloviť (GUI Brána 0): odporúčanie pravidlom kódu s dôvodmi (oslov najprv / oslov / doplň kontakt / neoslovovať), e-mail, prečo, čo žiadať; stav komunikácie naprieč hypotézami, ďalšia akcia, fronta prvého oslovenia (unikátne kanály, jedna organizácia naraz, cooldown), čo dostanú / potrebný vstup / čo nevieme z návrhu prínosu |
| `radar/value.py` | prehľad domény (doména = téma alebo hypotéza; citované tvrdenia, neznáme, 3 vyvracajúce otázky, verzie podľa hashu podkladov, Markdown) a návrh prínosu podľa typu adresáta (kontrola kódom a hodnotiteľom, záväzok potvrdí človek); krok `value` |
| `radar/person_contacts.py` | e-mail a telefón ku konkrétnej osobe (web organizácie, úradné domény; bez modelu); lokálny index webu organizácie zo sitemap |
| `radar/registers.py`, `radar/orgs.py` | RPO a RÚZ (cache, tempo, bez adries štatutárov); organizácie k hypotéze, overenie webu cez IČO |
| `radar/contacts.py`, `radar/names.py`, `radar/people.py` | deterministická extrakcia kontaktov z HTML; mená, tituly, rod; ľudia, mapa rolí, väzby, priorita; účel kontaktu s dôkazom (vlastník procesu, používateľ, správca dát, schvaľovateľ rozpočtu, expert; politici a členovia orgánov nie) |
| `radar/domain.py` | doménové podklady (neblokujúce obohatenie): priority domén (prvá dávka + ručne), stav `not_requested → queued → researching → ready/partial/needs_research` podľa pokrytia častí, jeden pokus na verziu vstupu, tvrdenia so stavom a rozsahom (opis povolania nie je fakt o firme), otázky s cieľovou rolou a blokovanou akciou, hlavná otázka prvého oslovenia, odpovede s históriou |
| `radar/domain_sources.py` | ESCO import z balíka CSV (verzia, jazyk, väzby; `esco-import`), fulltext FTS5 so slovenskými kmeňmi (`esco-search`), rola → kandidátne povolania (jednoznačné pravidlom, inak výber človekom), ohraničený doménový výskum (`research_domain`, `domain-research`): dopyty podľa účelu, doslovné úryvky bez kontaktov, limity, jeden pokus na verziu vstupu |
| `radar/outcomes.py` | výsledok oslovenia zapísaný človekom → stav oslovenia, stav potreby návrhov prínosu (po organizáciách a type adresáta), dôvod novej verzie prehľadu domény; metriky po unikátnych organizáciách (čas prípravy, dodania, režimy) |
| `radar/outreach.py`, `radar/crm.py` | návrhy oslovení podľa režimu (discovery, data_request, expert_review, pilot) s obchodnou politikou `outreach.value_offer`, reťazcom dôvodov (`package_json`) a pripravenosťou; kontroly, stavy, follow-up, mailto; rozhovory, metriky a verdikt Brány 0, exporty |
| `radar/privacy.py`, `radar/review.py`, `radar/evals.py`, `radar/bench.py` | maskovanie logov, LIA, potlačenie, forget, purge; kontrola → gold label; eval (Wilson) a referenčná vzorka s kontrolnou časťou (holdout); benchmark a plán behu |
| `radar/pipeline.py`, `radar/cli.py`, `radar/commands.py` | run-all v poradí podľa modelov; príkazy `python -m radar …` |
| `radar/agent.py` | agent s nástrojmi len na čítanie a `record_evidence` (úlohy deep_dive, validate, scout) |
| `radar/scout.py`, `radar/validate.py` | agent hľadá nové problémy (bunky matice); test uložených hypotéz (fronta, verdikt kódu) |
| `radar/workflows.py`, `radar/jobs.py` | akcie (`ACTIONS`: scout, validate, people, drafts, evidence, topics, critique, profiles, run-all; `python -m radar workflow …`); úlohy na pozadí z GUI (tabuľka `jobs`, jedna naraz, poradie, stav GPU) |
| `radar/gui/` | Streamlit: `app.py` (navigácia), `app_pages/*.py` (12 stránok vrátane Správcu akcií), `common.py` (escape textu zo zdrojov, karty akcií, panel úloh a poradia), `cloud_panel.py` (Cloudové modely: profily, nasadenia, smerovanie rolí, rozpočet a sadzby, test pripojenia, spotreba) |
| `tests/` | `fakes.py` (Recorder, BoundFake, FakeSearx, FakeFetcher), `fixtures/` (HTML, RPO JSON); žiadne sieťové volania |

## Invarianty (neporušiť)

1. **Nič bez citátu.** Každé tvrdenie má dokument (URL, čas stiahnutia, hash) a doslovný citát.
   Či citát a čísla v zdroji naozaj sú, overuje kód (`verify.py`), nie model. Model smie
   tvrdenie zamietnuť alebo zoslabiť, nikdy nepovýši neoverené na overené.
2. **Čísla počíta Python.** Model navrhuje štruktúru výpočtu, nie výsledok.
3. **Generátor nie je overovateľ.** Výstup rýchleho modelu posudzuje iný model alebo kód.
4. **Obsah webu je nedôveryhodný.** Vždy v `<untrusted_document>`. Agent nedostane nástroj,
   ktorý niečo mení alebo odosiela.
5. **Zlyhanie modelu nezhodí beh.** `LLMError`, timeout alebo orezaný výstup: položka ostane
   na dokončenie alebo ide do kontroly (review queue) a beh pokračuje.
6. **Behy sa dajú prerušiť a obnoviť.** Stav je v DB, nie v pamäti.
7. **Žiadne vymyslené hodnoty ani falošne presné skóre.** Chýbajúci údaj je „chýba“. Ak sa niečo
   zoraďuje podľa skóre, GUI a správa vždy ukážu jeho zložky.
8. **Testy sú offline a deterministické** (fakes, fixtures, pevný seed).

## Súkromie a etika (tvrdé pravidlá)

- **Systém nikdy nič neodosiela.** Žiadny SMTP, žiadne API sociálnych sietí, žiadne automatické
  vypĺňanie formulárov ani sledovanie otvorenia e-mailov. Iba návrh, kopírovanie a odkaz `mailto:`.
  Odosiela používateľ ručne.
- **Kontakty iba pracovné a zverejnené** samotnou organizáciou alebo osobou na jej webe, alebo
  v úradnom zverejnení. Vždy so zdrojovou URL a úryvkom, v ktorom sa hodnota doslova nachádza.
- **Nikdy** nehádaj e-maily (meno.priezvisko@…), neoveruj schránky cez SMTP, nedekóduj
  obfuskované adresy („(at)“, Cloudflare). Takú stránku označ „kontakt otvor ručne“.
- **LinkedIn a sociálne siete sa nesťahujú** (blokované domény vo fetcheri). URL profilu sa smie
  uložiť len z odkazu na webe organizácie, z výsledku vyhľadávania alebo ručne, s označením „over ručne“.
- **Osobné údaje** iba v tabuľkách pre ľudí, kontakty, oslovenia a rozhovory, a iba pre hypotézy
  vybrané na Bránu 0. Citáty v `claims` s e-mailom alebo telefónom sa naďalej neukladajú.
  Pred uložením kontrola zoznamu potlačenia (suppression). Retencia a príkaz `forget` sú povinné.
- **Logy** maskujú e-maily a telefóny. **Správy a exporty** osoby predvolene pseudonymizujú.
- **GUI a všetky služby** počúvajú iba na `127.0.0.1`. Žiadna telemetria (Streamlit:
  `browser.gatherUsageStats = false`).
- **Žiadne dáta, systémy ani kontakty zamestnávateľa** používateľa. Projekt pracuje iba
  s verejnými zdrojmi. Návrhy oslovení zamestnávateľa nespomínajú.
- **Do repozitára nepatria** `data/`, `logs/`, `reports/`, `exports/`, `*.gguf`, skutočné meno ani
  e-mail odosielateľa. V configu sú placeholdery v tvare `{MENO_PRIEZVISKO}`.

## Konvencie

- Python 3.11+, typové anotácie, pydantic modely pre každý štruktúrovaný výstup modelu.
- SQL iba v `db.py` (pri rozdelení v balíku `radar/db/`). Schéma sa mení iba migráciou cez
  `PRAGMA user_version`. Existujúcu migráciu nikdy neupravuj, pridaj novú.
- Prompty iba v `prompts.py`, každý s verziou. Každé volanie modelu sa zapíše do
  `model_judgments` (úloha, model, verzia promptu, výstup, tokeny, čas).
- Každé nové nastavenie má predvolenú hodnotu v `config.py` a komentár v `config.yaml`.
- Výber modelu ide vždy cez routing úloh (`task → role`), nikdy natvrdo v kóde fázy.
- Závislosti: povolené navyše `streamlit`, `phonenumbers`, `openpyxl`, `numpy`, `duckdb`, `scikit-learn`, `statsmodels`. HTML parsuj cez
  `lxml` (už je závislosťou trafilatura). Inú závislosť pridaj len s odôvodnením v `docs/DECISIONS.md`.
  Žiadne cloudové SDK (Azure OpenAI ide cez existujúci klient `openai` a `httpx`).
- Krátke správy pre používateľa, bez zbytočného textu.

## Postup práce

1. Najprv testy na akceptačné kritériá míľnika (fakes, fixtures), potom implementácia.
2. `python -m pytest -q` musí prejsť. Existujúce testy nemeň bez dôvodu zapísaného v `docs/DECISIONS.md`.
3. Pri nejasnosti použi predvolenú hodnotu zo špecifikácie a zapíš ju do `docs/DECISIONS.md`.
   Otázky z kapitoly „Otvorené otázky“ polož používateľovi naraz, nie priebežne.
4. Na konci míľnika aktualizuj `README.md` (príkazy, bezpečnosť) a mapu kódu v tomto súbore a stručne
   zhrň, čo sa zmenilo.
5. Reálne služby (model, SearXNG, web) púšťaj iba cez `doctor` alebo na výslovnú žiadosť.
   V testoch nikdy.
6. Ak je projekt v gite, commituj po míľnikoch a nikdy necommituj súbory z „Do repozitára nepatria“.
