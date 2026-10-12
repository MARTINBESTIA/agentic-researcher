# Problem Radar 0.2 – Brána 0 agregátor

Lokálny systém, ktorý hľadá a dokladá podnikateľské problémy a pripravuje **Bránu 0** (customer
discovery: overiť problém rozhovormi skôr, než sa začne stavať produkt). Zbiera zdroje, vyťahuje
z nich tvrdenia s doslovnými citátmi, overuje ich voči zdroju, zoskupuje do hypotéz a k vybraným
hypotézam hľadá **organizácie, ľudí a ich zverejnené pracovné kontakty** a pripraví **návrhy
oslovení**. Všetko je v lokálnej SQLite databáze a dá sa prezerať v **GUI** v prehliadači.

Beží iba na tomto počítači: modely cez **Ollama**, vyhľadávanie cez **SearXNG** v Dockeri, dáta v
**SQLite**. Žiadne platené API, žiadna telemetria. **Systém nikdy nič neodosiela** – návrh
skontroluješ, schváliš, skopíruješ alebo otvoríš v pošte a pošleš sám.

Špecifikácia: `docs/SPEC_brana0.md` · pravidlá pre Claude Code: `CLAUDE.md` · rozhodnutia a
odchýlky: `docs/DECISIONS.md` · posúdenie oprávneného záujmu: `docs/LIA_template.md` (vyplnené
v `data/LIA.md`).

## Ako to funguje

```
 zdroje: témy a projekty (config.yaml) · katalóg sources.yaml · matica „vzor bolesti × odvetvie“ · RPO, RÚZ
   │
 [bez modelu]  discover + collect          → documents (70 % témy, 30 % objavovanie)
 [rýchly]      screen → extract            → claims (neoverené)
 [kód]         verify: citát a čísla sú v zdroji?
 [rýchly+kód]  orgs: menované v dôkazoch, obstarávanie, segment → web overený cez IČO
 [rýchly+kód]  people: citované osoby, štatutári (RPO), kontaktné stránky → ľudia, kontakty
 [embed]       embeddingy tvrdení a hypotéz (CPU)
 [presný]      people-rank: mapa rolí (3 hlasy), väzby osoba–problém, priorita, výber ≤ 15
 [presný]      drafts: fakty prejdú entail_gate (3 hlasy) → návrh → kontroly kódom + draft_check
 [presný]      verify: vyplýva tvrdenie z citátu?  → cluster: zaradenie, nové problémy ako „candidate“
 [presný]      critique, market  →  report + purge
 ── človek v GUI ──
 schváli → pošle ručne → zapíše odpoveď a rozhovor → citáty z rozhovorov ako dôkazy → metriky Brány 0 → rozhodnutie
```

**Kaskáda namiesto väčšieho modelu všade.** Rýchly model (qwen3.5:9b, ~54 tok/s) hľadá kandidátov,
presný model (Qwen3.6-27B, ~4,5 tok/s na 8 GB VRAM, ~30–60 s na posúdenie) rozhoduje tam, kde na
tom záleží: overenie tvrdení, zaradenie, väzby osoba–problém, návrhy oslovení. Generátor nie je
overovateľ: čo vytiahne rýchly model, posudzuje presný model alebo kód. Každé volanie modelu je
v tabuľke `model_judgments` (úloha, model, verzia promptu, výstup, tokeny, čas).

**Ďalšie princípy (z v0.1):** nič bez doslovného citátu, ktorý overí kód; čísla počíta Python;
obsah webu je nedôveryhodný; zlyhanie modelu nezhodí beh (neistá položka ide do **Kontroly**);
beh sa dá prerušiť a pokračuje tam, kde skončil.

## Inštalácia (Windows)

Tento počítač je nastavený (11. 10. 2026). Postup pre iný počítač:

1. **Python 3.11+:**
   ```
   python -m venv .venv
   .venv\Scripts\python -m pip install -r requirements.txt
   ```
2. **Modely v Ollame** (spolu ~24 GB):
   ```
   ollama pull qwen3.5:9b
   ollama pull qwen3.6:27b-q4_K_M
   ollama pull qwen3-embedding:0.6b
   ollama create radar-qwen3.5-9b -f ollama/Modelfile.qwen3.5-9b
   ollama create radar-qwen3.6-27b -f ollama/Modelfile.qwen3.6-27b
   ollama create radar-qwen3-embedding -f ollama/Modelfile.qwen3-embedding
   ```
   Modelfiles nastavujú kontext 16k tokenov (Ollama inak dlhší vstup potichu oreže) a počet vrstiev
   na GPU. Alternatíva bez Ollamy: llama-swap (`llama-swap.example.yaml`, `start_llm_swap.bat`).
3. **SearXNG:** skopíruj `searxng/settings.example.yml` do `searxng/settings.yml` a doplň `secret_key`
   (náhodný reťazec, súbor nejde do gitu). Spusti Docker Desktop (ak spadne pri štarte, najprv
   `scripts\fix_docker_sockets.ps1`), potom `docker compose up -d` (alebo `docker start radar-searxng`,
   ak kontajner už existuje zo starej kópie). Modely: aj `ollama create radar-qwen3.6-35b-a3b -f
   ollama/Modelfile.qwen3.6-35b-a3b` (rola agent, výber v GUI → Správca akcií → Modely).
4. **Kontrola a benchmark:** `.venv\Scripts\python -m radar doctor --bench`
5. **Posúdenie oprávneného záujmu:** doplň meno a kontakt v `data/LIA.md` a skontroluj text.
   Bez `privacy.lia_completed: true` sa ľudia nehľadajú.
6. **Profil odosielateľa** v `config.local.yaml` (skopíruj `config.local.example.yaml`; git ho ignoruje,
   hodnoty prepíšu `config.yaml`): meno, e-mail, afiliácia iba ak je pravdivá, URL informácie o spracúvaní.
   `config.yaml` je v repozitári – osobné údaje doň nepíš. Kým sú v profile placeholdery `{…}`, schvaľovanie
   je zablokované; návrhy oslovení sa vytvoria aj tak (pätička s placeholdermi).

## Každodenné použitie

Príkazy: `.venv\Scripts\python -m radar <príkaz>` z priečinka projektu.

| Príkaz | Čo robí |
|---|---|
| `doctor [--bench]` | kontrola modelov, kontextu, nástrojov, embeddingov, SearXNG; `--bench` zmeria rýchlosť rolí |
| `run-all [--minutes 480] [--profile balanced]` | celý beh v poradí podľa modelov + správa + retencia |
| `gui` | GUI na <http://127.0.0.1:8501> (tlačidlá agentických workflowov sú v Prehľade a pri hypotéze) |
| `workflow scout --minutes 90` | nové hypotézy z internetu: objavovanie + agent prieskumu → overenie → kandidáti |
| `workflow validate --minutes 180 [--hypotheses 4,11]` | test uložených hypotéz po dávkach (všetky postupne): zdroje za, proti, existujúce riešenia → verdikt kódu |
| `workflow people --hypotheses 4 --minutes 90` | firmy, ľudia a kontakty (aj LinkedIn) pre zvolený problém; nič sa neodosiela |
| `profiles` | doplní URL profilov LinkedIn už vybraným ľuďom (z výsledkov vyhľadávania, nesťahuje sa) |
| `select --hypothesis 3 --people on` / `select --auto` | zapne hľadanie ľudí pre hypotézu / automaticky top 3 |
| `people --hypothesis 3` | organizácie → ľudia a kontakty → väzby a priorita |
| `.venv\Scripts\python scripts\gate0_run.py --minutes 300` | celá Brána 0 pre vybrané hypotézy (mapy rolí → organizácie → ľudia → poradie → návrhy); `--skip-orgs`, `--recollect`, `--refresh-drafts` |
| `drafts --hypothesis 3` | návrhy oslovení (najprv najvyššia priorita, max. 24 za beh) |
| `orgs --hypothesis 3 [--import zoznam.csv]` | iba organizácie (aj z verejného zoznamu CSV: name;ico;website) |
| `discover`, `sources --check` | objavovanie nových problémov; kontrola katalógu zdrojov |
| `eval --task entail --sample 30` / `--synthetic 20` / `--profile all` | labely do Kontroly, syntetické prípady, porovnanie profilov |
| `forget --email x@y.sk` (`--person ID`, `--phone …`) | výmaz osoby a zápis do zoznamu potlačenia |
| `purge`, `privacy [reapply]`, `export --what people --format xlsx [--include-personal]`, `export --person ID` | retencia, stav súkromia, exporty (predvolene pseudonymizované) |
| `list`, `progress`, `note`, `status`, `merge`, `deep-dive` | ako v0.1 |
| `collect`, `screen`, `extract`, `verify-code`, `verify`, `embed`, `cluster`, `people-rank`, `prepare-next`, `critique`, `market`, `report`, `scout`, `validate-search`, `validate-finish` | jednotlivé kroky |

### Brána 0 krok za krokom

1. **Vyber hypotézy** (GUI → Problémy → „Hľadať ľudí“, alebo `select`). Automaticky sa berú top 3
   podľa sily dôkazov; ručná voľba má prednosť.
2. **`people`** nájde organizácie (menované v dôkazoch, obstarávanie, segment cez register a web
   s IČO), ľudí (citovaných v dôkazoch, štatutárov z RPO, z kontaktných stránok) a ich zverejnené
   pracovné kontakty. Ku každej osobe uloží zdroj a úryvok, v ktorom kontakt doslova je.
3. **GUI → Ľudia a kontakty:** karta „Všetky problémy“ = ľudia a firmy ku všetkým vybraným problémom
   so značkami (citovaný v dôkaze, rola sedí, web overený, IČO v RPO, zdroj zaradenia firmy,
   zverejnený kontakt, LinkedIn, expert, obstarávanie), e-mailom, telefónom a odkazom na LinkedIn.
   Karta „Detail problému“: priorita so zložkami (role_fit, väzba, kanál, zhoda organizácie),
   kontakty s úryvkom, ručné pridanie alebo odstránenie LinkedIn, akcie „chcem osloviť“, „vylúčiť“,
   „vymazať (forget)“. Skrytý e-mail („meno(at)firma.sk“) systém nedekóduje – otvor stránku ručne.
4. **`drafts`** → **GUI → Oslovenia:** e-mail (varianty A podľa dôkazu a B podľa funkcie), poznámka
   pre LinkedIn (≤ 200 znakov), skript na telefón. Výsledky kontrol (dĺžky, zakázané frázy, čísla
   mimo faktov, pätička) a viet, ktoré model považuje za nepodložené. Uprav, „Uložiť a skontrolovať“,
   „Schváliť“ (max. 10 denne), „Kopírovať“ / „Otvoriť v pošte“, pošli sám a označ „odoslané“.
   Follow-up pripadne na 5. pracovný deň. Námietka = okamžitý výmaz a potlačenie.
5. **GUI → Rozhovory:** formulár podľa The Mom Test; citáty z poznámok sa stanú dôkazmi (iba ak sú
   v poznámkach doslova). **GUI → Brána 0:** rozhovory/cieľ, „spomenul sám“, platiteľ, lievik,
   návrh verdiktu; rozhodnutie zapisuješ ty.

LinkedIn: ukladá sa iba URL profilu (kanonická, bez parametrov) s označením „over ručne“ – z odkazu
na webe organizácie v bloku osoby, z výsledku vyhľadávania (celé meno v titulku a názov organizácie)
alebo zadaná ručne v GUI. Stránka LinkedIn sa nikdy nesťahuje (zakazujú to podmienky); bezplatný
účet má limit 200 znakov na poznámku a približne 5 personalizovaných pozvánok mesačne.

### Nočný beh

`run_nightly.bat` (alebo `scripts\run_weekly.ps1 -Minutes 480`) spustí Docker a SearXNG, skontroluje
modely a spustí `run-all` ako úlohu Správcu akcií (`python -m radar job run-all --mode queue --wait`):
v GUI vidíš priebeh a ak práve beží akcia z GUI, nočný beh sa zaradí do poradia za ňu. Profil a modely
platia z GUI (Správca akcií → Modely). Naplánovanie (registruješ ty, mení nastavenia Windows):
`scripts\register_weekly_task.ps1 -Daily -Time 23:00 -Wake` (každý deň s prebudením; vo Windows musia byť
povolené časovače prebudenia), bez `-Daily` raz týždenne (`-Day Sunday`). Na batériu sa beh nespustí;
prerušenú akciu spustíš v Správcovi akcií tlačidlom „Spustiť znova“.
Rozdelenie času: ~45 % rýchly model a práca bez modelu, zvyšok presný model v poradí priority
(ľudia a návrhy → overenie dôkazov → zaradenie → kritika a trh). Čo sa nestihne, pokračuje
pri ďalšom behu.

## Presnosť a eval

- Profily: `fast` (všetko rýchly model), `balanced` (tabuľka v `config.yaml → models.tasks`),
  `accurate` (všetko presný, napr. víkend): `run-all --profile accurate`.
- Self-consistency: `entail_gate` a `role_map` majú 3 vzorky pri teplote 0,7; 2:1 = prijaté
  s príznakom „nízka zhoda“, nezhoda alebo neurčený výstup → Kontrola.
- Uvažovanie (thinking) je v profile balanced vypnuté: Ollama nemá rozpočet uvažovania a Qwen3.6-27B
  pri mape rolí premýšľal > 3 000 tokenov (~12 min na vzorku) a aj tak skončil orezaný. Zapína sa
  v eval profiloch B a D, kde sa dá zmerať, či sa oplatí (pozri `docs/DECISIONS.md`).
- Eval porovná profily A–D (rýchly/presný, s uvažovaním a bez) na položkách s gold labelom:
  presnosť, úplnosť, F1, 95 % Wilsonov interval, čas a tokeny na položku. Labely vznikajú
  v GUI → Kontrola; pred záverom treba aspoň 50 na úlohu.

## Správca akcií (GUI)

Stránka **Správca akcií** má všetky akcie, ktoré zbierajú alebo spracúvajú dáta lokálnymi modelmi:
hľadať nové hypotézy, otestovať všetky hypotézy, zbierať ľudí ku overeným hypotézam (tieto tri aj
v Prehľade), spracovať zásobník dôkazov, zbierať dôkazy k témam, kritika a odhad trhu, návrhy
oslovení, LinkedIn profily a celý beh. Naraz beží jedna akcia (GPU); ďalšie sa dajú **zaradiť do
poradia** alebo bežiacu **zastaviť a spustiť inú**. Stránka ukazuje aj model načítaný v GPU (aj
z iných aplikácií) a históriu. Z príkazového riadku: `python -m radar workflow <akcia> --minutes N`
(`scout`, `validate`, `people`, `contacts`, `drafts`, `evidence`, `topics`, `critique`, `gate1`, `gate2`, `bench`, `run-all`).

## Brána 1 (dáta) a Brána 2 (experimenty)

**Brána 1** beží pre hypotézy vybrané na Bránu 0, súbežne s rozhovormi (GUI → Brána 1 – dáta, alebo akcia
„Brána 1: prieskum dát“): polia potrebné na riešenie (kľúčové polia) → zdroje (licencia a frekvencia iba
z prečítanej stránky) → kontrola a kvalita → stiahnutie do DuckDB (`data/datasets`, limit 2 GB na súbor,
50 GB spolu) → zreprodukovanie známeho čísla z overeného tvrdenia (model navrhne SQL, počíta DuckDB) →
návrhy žiadostí podľa zákona 211/2000 a pripravenosť dát so zložkami. Chýbajúce dáta sú príznak, nič sa
nevyraďuje; „Brána 1 prešla“ zapisuješ ty.

Zdroje hľadá agent aj v **Národnom katalógu otvorených dát** (data.slovensko.sk, verejný SPARQL, ~22 000
datasetov) – bez vyhľadávača: poskytovateľ, licencia z podmienok užitia, periodicita, formáty a odkazy na
stiahnutie priamo z metadát. Pre každé pole sa ešte pred agentom načítajú 2 kandidáti; kontrola zdroja berie
súbory z distribúcií v katalógu. Licencie **NC/ND** (iba nekomerčné použitie, bez úprav) majú v kvalite zdroja
nižšie skóre a príznak – over, či sa hodia pre tvoj produkt. Vypnutie: `gate1.nkod_enabled: false`.

**Brána 2** (po tvojom rozhodnutí): dvojice hypotéza × organizácia z nájdených dát → predregistrácia
(uzamkneš ju pred testom) → spätný test proti naivnému odhadu (iba Python) → demo HTML pre organizáciu →
spätná väzba expertov a rozhodnutie.

**Modely:** Správca akcií → Modely – výber modelu pre každú rolu (rýchly, presný, agent, embeddingy) a
profil; akcia „Zmerať rýchlosť modelov“. Na 8 GB VRAM + 32 GB RAM je praktické maximum ~30–35B
(MoE `qwen3.6:35b-a3b` ~21 GB, `ollama/Modelfile.qwen3.6-35b-a3b`).

## Agentické workflowy (tlačidlá v GUI)

- **Hľadať nové hypotézy** (Prehľad): objavovanie (15 dokumentov) a agent prieskumu (6 sedení),
  ktorý pre bunku „odvetvie × vzor bolesti“ hľadá na webe konkrétne problémy a ukladá doslovné
  citáty. Citáty overí kód a presný model; nový problém vznikne ako kandidát.
- **Otestovať ďalšie hypotézy** (Prehľad) a **Otestovať teraz** (Problémy → detail): agent ku
  každej hypotéze hľadá nové nezávislé zdroje, protidôkazy a existujúce riešenia. Verdikt počíta
  kód zo zložiek (podložená / sporná / slabá / nepodložená); zložky sú pri hypotéze. Fronta
  postupne prejde všetky uložené hypotézy (aj zamietnuté, tie nakoniec), nočný beh 6 za noc.
- **Hľadať firmy a ľudí k tomuto problému** (Problémy → detail, po LIA) a **Zbierať ľudí ku overeným
  hypotézam** (Prehľad, Správca akcií): vyberie problémy na Bránu 0 a spustí organizácie → ľudia
  a kontakty → priorita → LinkedIn. Návrhy oslovení sú samostatná akcia.
- **Zoskupiť hypotézy do tém** (Správca akcií; v celom behu automaticky): podobné hypotézy (embeddingy)
  dostanú spoločnú tému, napr. všetky o e-faktúrach; názov navrhne rýchly model. Hypotézy sa nezlučujú.
  Problémy → **Témy** ukazujú silu dôkazov za celú tému (každé tvrdenie raz), počty podložených
  a neotestovaných hypotéz; tému sa dá premenovať (ručný názov sa zachová). V zozname hypotéz je filter „Téma“.
  V detaile témy: **Otestovať neotestované hypotézy témy** a **Zbierať ľudí k téme** (3 najsilnejšie
  hypotézy témy na Bránu 0).

Úloha beží ako samostatný proces (aj po zatvorení prehliadača), naraz iba jedna (GPU), dá sa
zastaviť. Priebeh a koniec výstupu (maskovaný) sú v Prehľade, celý výstup v `logs/jobs/`.

## Objavovanie nových problémov

30 % dokumentov behu ide mimo tém: katalóg `sources.yaml` (NKÚ, revízie výdavkov, legislatíva,
EUR-Lex, ÚVO a PTK, CRZ, zväzy, výročné správy z RÚZ) a rotujúca matica „vzor bolesti × odvetvie“.
Medzi kategórie sa delí Thompson samplingom (úspech = dokument s overeným tvrdením). Zdroj z
katalógu sa použije až po tvojom potvrdení (`sources --check`, potom `verified: true`).
Nový problém je najprv „candidate“; hypotézou sa stane až s 2 overenými tvrdeniami z 2 domén.
Regulačné povinnosti s termínom a signály dopytu (PTK, výzvy) majú v správe vlastné sekcie.

## Súkromie a bezpečnosť

- Iba pracovné kontakty zverejnené organizáciou alebo osobou, vždy so zdrojovou URL a úryvkom.
  E-maily sa nehádajú, schránky sa neoverujú (SMTP), obfuskované adresy sa nedekódujú.
- Sociálne siete sa nesťahujú ani po presmerovaní. Z RPO sa ukladá iba meno a funkcia štatutára
  (adresa sa odstráni ešte pred uložením do cache).
- Surové HTML kontaktných stránok sa neukladá; nevybraní kandidáti sa vymažú v tom istom behu.
- Retencia: neoslovení 90 dní, po poslednej interakcii 12 mesiacov, odmietnutí 30 dní, exporty
  30 dní (`purge`, aj na konci `run-all`). Námietka/výmaz: `forget` + hash v zozname potlačenia
  (soľ v `data/.salt`; po obnove zo zálohy `privacy reapply`).
- Logy maskujú e-maily a telefóny; správy a exporty predvolene pseudonymizujú osoby.
- GUI aj služby počúvajú iba na 127.0.0.1; Streamlit bez telemetrie (`.streamlit/config.toml`).
- Pri prebiehajúcom súvisiacom obstarávaní sa návrh nevytvorí – komunikuj iba oficiálnou cestou.
- Firmy, ktoré nemajú byť oslovené (napr. konflikt záujmov so stážou): `outreach.exclude_orgs`.
- Do gitu nepatria `data/`, `logs/`, `reports/`, `exports/`. Projekt drž mimo OneDrive a zapni
  šifrovanie disku. Právne časti sú technický podklad, nie právna rada.

## Limity

- Stolový výskum nie je validácia; rozhodujú rozhovory a ochota platiť.
- Presný model je na 8 GB VRAM pomalý (~4,5 tok/s): za noc stihne rádovo stovky posúdení.
- Weby organizácií sú rôzne; časť ľudí a kontaktov sa nenájde (JavaScript, PDF, obrázky).
- Hľadanie organizácií podľa segmentu ide cez vyhľadávač a IČO na webe; RÚZ nefiltruje podľa SK NACE.
- Malý model hypotézy rozdeľuje príliš jemne (v DB z v0.1 je ich 273); zlučuj ich (`merge`, GUI).

## Štruktúra projektu

| Súbor | Obsah |
|---|---|
| `radar/config.py` | konfigurácia a predvolené hodnoty (roly modelov, ľudia, oslovenia, súkromie, objavovanie) |
| `radar/db/` | SQLite: `evidence.py` (register dôkazov), `gate0.py` (organizácie, ľudia, oslovenia, rozhovory), `ops.py` (provenance, kontrola, labely, eval), `schema.py` + `migrations.py` |
| `radar/models.py`, `radar/llm.py`, `radar/prompts.py` | roly a routing úloh, self-consistency, provenance; klient Ollama/OpenAI; prompty s verziou |
| `radar/collect.py`, `discover.py`, `fetch.py`, `search.py` | zber, objavovanie, sťahovanie (robots.txt, blokované domény) |
| `radar/extract.py`, `verify.py`, `cluster.py`, `critic.py`, `market.py`, `report.py` | pipeline dôkazov |
| `radar/registers.py`, `orgs.py`, `contacts.py`, `names.py`, `people.py` | RPO/RÚZ, organizácie, kontakty, ľudia a priorita |
| `radar/outreach.py`, `crm.py`, `privacy.py`, `review.py`, `evals.py`, `bench.py` | oslovenia, Brána 0, súkromie, kontrola, eval, benchmark |
| `radar/pipeline.py`, `cli.py`, `commands.py` | run-all, príkazový riadok |
| `radar/gui/` | Streamlit (`app.py`, `app_pages/`) |
| `tests/` | testy bez modelu a siete (`python -m pytest -q`) |
