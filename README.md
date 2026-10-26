# Problem Radar 0.2 – Brána 0 agregátor

Lokálny systém, ktorý hľadá a dokladá podnikateľské problémy a pripravuje **Bránu 0** (customer
discovery: overiť problém rozhovormi skôr, než sa začne stavať produkt). Zbiera zdroje, vyťahuje
z nich tvrdenia s doslovnými citátmi, overuje ich voči zdroju, zoskupuje do hypotéz a k vybraným
hypotézam hľadá **organizácie, ľudí a ich zverejnené pracovné kontakty** a pripraví **návrhy
oslovení**. Všetko je v lokálnej SQLite databáze a dá sa prezerať v **GUI** v prehliadači.

Predvolene beží iba na tomto počítači: modely cez **Ollama**, vyhľadávanie cez **SearXNG** v Dockeri, dáta v
**SQLite**. Žiadna telemetria. Voliteľne môže vybraná rola modelu (napr. hodnotiteľ) bežať v **Azure OpenAI**
s vlastným rozpočtom – pozri [Cloudové modely](#cloudové-modely-azure-openai-voliteľné). **Systém nikdy nič
neodosiela** – návrh skontroluješ, schváliš, skopíruješ alebo otvoríš v pošte a pošleš sám.

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
 [rýchly+kód]  contact-search: plán koho osloviť (zákazník, držiteľ dát, expert) → dopyty → web cez IČO;
               držitelia dát z Brány 1 a kontaktný bod datasetu z Národného katalógu
 [rýchly+kód]  people: citované osoby, štatutári (RPO), kontaktné stránky → ľudia, kontakty
 [embed]       embeddingy tvrdení a hypotéz (CPU)
 [presný]      people-rank: mapa rolí (3 hlasy), väzby osoba–problém, priorita, výber (zákazníci, držitelia dát,
               experti zvlášť; limity v config.yaml) → GUI Brána 0 → Koho osloviť
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
   .venv\Scripts\python -m pip install -r requirements-optional.txt   # voliteľne: PM4Py (licencia AGPL v3)
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
| `drafts --hypothesis 3` | návrhy oslovení (najprv najvyššia priorita, max. 24 za beh; nie pre držiteľov dát a expertov) |
| `contacts-add --hypothesis 3 --url URL [--name "Meno"] [--kind zakaznik\|data\|expert] [--ask "…"]` | kontakt z verejnej stránky organizácie (meno a e-mail overí kód na stránke; bez mena generické kontakty organizácie) |
| `contact-search` | hľadanie kontaktov podľa hypotézy: plán (zákazník, držiteľ dát, expert), dopyty, overenie webu cez IČO, držitelia dát z Brány 1 a katalógu |
| `orgs --hypothesis 3 [--import zoznam.csv]` | iba organizácie (aj z verejného zoznamu CSV: name;ico;website) |
| `discover`, `sources --check` | objavovanie nových problémov; kontrola katalógu zdrojov |
| `eval --task entail --sample 30` / `--synthetic 20` / `--profile all` | labely do Kontroly, syntetické prípady, porovnanie profilov |
| `forget --email x@y.sk` (`--person ID`, `--phone …`) | výmaz osoby a zápis do zoznamu potlačenia |
| `purge`, `privacy [reapply]`, `export --what people --format xlsx [--include-personal]`, `export --person ID` | retencia, stav súkromia, exporty (predvolene pseudonymizované) |
| `catalog-sync`, `workflow catalog` | lokálny index Národného katalógu (metadáta a vektory; Brána 1 hľadá podľa významu) |
| `job autopilot --minutes 480 --mode queue --wait --resume` | plánovaný beh; `--resume` najprv dokončí prerušené poradie a úlohy |
| `job funnel --minutes 0` | lievik: jedna hypotéza po druhej od testu po Bránu 2, donekonečna (0) alebo N minút |
| `workflow eval`, `eval-all` | eval presnosti profilov modelov na úlohách s ≥ 50 gold labelmi |
| `dossier --hypothesis 582 [--personal] [--data full\|sample\|none] [--no-related]` | dosiér hypotézy: všetko k hypotéze v jednom ZIP – README, CSV, Excel, JSON, texty zdrojov, Brány 0–2, volania modelov, dáta (Parquet), manifest so SHA-256; predvolene pseudonymizovaný (aj GUI: Problémy → Export všetkého) |
| `list`, `progress`, `note`, `status`, `merge`, `deep-dive` | ako v0.1 |
| `collect`, `screen`, `extract`, `verify-code`, `verify`, `embed`, `cluster`, `people-rank`, `prepare-next`, `critique`, `market`, `report`, `scout`, `validate-search`, `validate-finish` | jednotlivé kroky |

### Prehľad príležitosti (začni tu)

GUI → **Problémy** → vyber hypotézu → prvá záložka **Prehľad príležitosti**, alebo
`python -m radar problem-brief --all` (súbory `reports/prehlady_problemov/INDEX.md` a `H<číslo>.md`). Jedna strana na
problém: odkazy na overené zdroje rozdelené podľa pôvodu (nezávislý dôkaz problému / dodávatelia riešení / iba kontext –
0 nezávislých znamená, že problém zatiaľ tvrdia hlavne predajcovia), čo problém znamená (prehľad domény), navrhnutý
produkt ako hypotéza riešenia (výstup z problémovej karty, návrh prínosu, ponuky z kariet organizácií, konkurencia,
kritika, hrubý trh), čo je podložené dátami a čo nie, koho a s čím osloviť (poradie, čo žiadať, otázky na posledný
prípad) a čo chýba s presnou akciou, ktorá to doplní. Skladá sa z uložených údajov – model sa nevolá; osoby sú
pseudonymizované. Pre nového používateľa: `docs/PRE_NOVEHO_POUZIVATELA.md`.

### Brána 0 krok za krokom

1. **Vyber hypotézy** (GUI → Problémy → „Hľadať ľudí“, alebo `select`). Automaticky sa berú top 3
   podľa sily dôkazov a **každá hypotéza, ktorej test skončil „podložená“** (`people.auto_select_validated`);
   ručná voľba (aj vypnutie) má prednosť.
2. **`people`** nájde organizácie (menované v dôkazoch, obstarávanie, segment cez register a web
   s IČO), potom **hľadá kontakty podľa hypotézy** (`contact-search`): rýchly model navrhne, koho osloviť –
   **zákazník** (vlastník procesu, rozhovor), **držiteľ dát** (schéma a vzorka), **expert** (zväz, výskum,
   watchdog) – s funkciami, dopytmi a tým, čo žiadať; weby overí kód cez IČO. Držitelia dát pribudnú aj
   z Brány 1 (vydavatelia zdrojov, kontaktný bod datasetu v Národnom katalógu). Potom ľudia (citovaní
   v dôkazoch, štatutári z RPO, z kontaktných stránok a zo stránky, kde sa organizácia našla) a ich
   zverejnené pracovné kontakty. Ku každej osobe uloží zdroj a úryvok, v ktorom kontakt doslova je.
   Ďalšie kolo dopytov príde, kým je kontaktovateľných menej ako `people.contact_goal_per_hypothesis`.
3. **GUI → Brána 0 → Koho osloviť:** ľudia a organizácie k hypotéze s e-mailom, odporúčaním („oslov
   najprv“, „oslov“, „doplň kontakt“, „neoslovovať“) a jeho dôvodmi, **stavom komunikácie naprieč
   hypotézami** (nové, návrh, pripravené na odoslanie, čaká na odpoveď, nadviazanie, uzavreté) a ďalšou
   akciou; prepínač „iba fronta prvého oslovenia“ skryje oslovených, odmietnutých, spoločné e-maily
   a druhý kontakt tej istej organizácie (organizácia oslovená za posledných 21 dní čaká). Stĺpce čo
   dostanú, potrebný vstup a čo ešte nevieme pochádzajú z návrhu prínosu; filter
   podľa typu (zákazník, držiteľ dát, expert), „Chcem osloviť“, odkaz `mailto:`. Tu je aj plán kontaktov
   (dá sa navrhnúť znova) a **pridanie kontaktu zo stránky** (URL + meno; meno aj kontakt overí kód na
   stránke) – rovnako `python -m radar contacts-add --hypothesis 1 --url … --name "…" --kind data`.
   **v15:** stĺpce *zhoda so segmentom*, *stav potreby*, *riešenie* a *obstarávanie*; zákazník mimo segmentu (napr.
   NBS pri malých realitných kanceláriách) ide do úrovne **„preveriť rolu“** mimo fronty, s dátovou alebo expertnou
   väzbou ostane v tej roli. Všeobecná stránka obstarávania je iba **kandidát**: návrh smie vzniknúť, schválenie čaká,
   kým v sekcii „Obstarávanie na overenie“ označíš *nič súvisiace neprebieha* alebo *potvrdené* (potom iba oficiálna
   cesta).
4. **GUI → Ľudia a kontakty:** karta „Všetky problémy“ = ľudia a firmy ku všetkým vybraným problémom
   so značkami (citovaný v dôkaze, rola sedí, web overený, IČO v RPO, zdroj zaradenia firmy,
   zverejnený kontakt, LinkedIn, expert, obstarávanie), e-mailom, telefónom a odkazom na LinkedIn.
   Karta „Detail problému“: priorita so zložkami (role_fit, väzba, kanál, zhoda organizácie),
   kontakty s úryvkom, ručné pridanie alebo odstránenie LinkedIn, akcie „chcem osloviť“, „vylúčiť“,
   „vymazať (forget)“. Skrytý e-mail („meno(at)firma.sk“) systém nedekóduje – otvor stránku ručne.
5. **`value`** → **GUI → Brána 0 → Doména a prínos:** prehľad domény na 5 minút (proces, pojmy, kto
   problém zažíva, spravuje dáta a rozhoduje, dnešný postup, úskalia významu dát, 3 otázky, ktoré môžu
   nápad vyvrátiť, neznáme) – každé tvrdenie s citátom, inak ide medzi neznáme; spoločný pre tému,
   nová verzia iba pri nových podkladoch (aj odpovediach kontaktov), staršie ostávajú; export Markdown.
   K tomu **návrh prínosu** pre zákazníka, držiteľa dát a experta: hypotéza potreby, malý výstup,
   rozhodnutie, ktoré podporí, minimálny vstup, obmedzenia – bez sľubov a čísel bez podkladu; konkrétny
   záväzok (rozsah, termín) potvrdzuješ ty. Kontroluje ho kód a hodnotiteľ (rola `reviewer`).
   **Doménové podklady neblokujú Bránu 0:** prvé 3 domény (podľa sily dôkazov, ďalšie zaradíš ručne) dostanú
   výskum skôr; stav `pripravené / čiastočné / treba doplniť` s pokrytím (roly, proces, riešenia, význam dát,
   alternatívne vysvetlenia) a dôvodom ukončenia. Ten istý vstup sa znova neskúša – iba nové podklady alebo
   „Obnoviť doménové podklady“. Tvrdenia majú stav (zo zdroja, hypotéza, uviedol kontakt, pozorované v dátach,
   vyvrátené) a rozsah – opis povolania nie je fakt o konkrétnej firme. Otázky ukazujú, komu ich položiť a čo
   neznáma blokuje (napr. bez histórie termínov žiadna analýza meškania, bez merania žiadne tvrdenie o úspore).
   **Zdroje:** lokálny ESCO index (stiahni balík CSV zo stránky ESCO → Download a spusti
   `python -m radar esco-import --dir <priečinok>`; hľadanie `python -m radar esco-search "nákupca"`) – roly kontaktov
   sa namapujú na kandidátne povolania (nejednoznačné vyberieš v GUI); a web cez SearXNG: najviac 6 dopytov,
   10 stránok a 15 doslovných úryvkov na doménu (bez e-mailov a telefónov). Ručne: `python -m radar domain-research
   --hypothesis 1`.
   **Karta organizácie (v15, `docs/organization_opportunities.md`):** v tej istej záložke vyber organizáciu a rolu →
   *Vytvoriť / obnoviť kartu* → *Spustiť akciu Karty organizácií* (úloha na pozadí). Karta odpovie na päť otázok
   (komu pomáham → v akom úkone → čo sa zmení → aký minimálny vstup → ako overíme prínos), ukáže tri nezávislé
   pripravenosti (**na rozhovor / na návrh skúšky / na tvrdenie o prínose**), ďalší krok, hodnotenie položiek
   (jasné s podkladom / jasná hypotéza / chýba / rozpor), dôkazy s relevantnosťou pre túto organizáciu a medzery
   s cieleným výskumom (iba verejné zdroje; interné otázky idú do rozhovoru). Každý výrok má stav poznania; všeobecný
   fakt o odvetví je pre organizáciu hypotéza, úspora dodávateľa sa neprenáša, referencia „používa produkt“ nie je
   aktuálny problém. Ručná oprava = nová verzia; odpoveď zapísaná ku karte označí na obnovu iba karty tejto
   organizácie. **Sústredený režim** (najviac 3 organizácie): autopilot najprv dopĺňa ich karty a návrhy. CLI:
   `python -m radar org-cards --run --target 4:4 --target 100:4` (ORG:HYP), `--list`, `--export exports/karty.md`,
   `--compare reports/porovnanie.md`, `--focus on --hypothesis 4 --org 4`.
6. **`drafts`** → **GUI → Oslovenia:** návrh podľa účelu – **zákazník** (rozhovor o skúsenosti, prípadne
   ponuka malého výstupu), **držiteľ dát** (vysvetlenie polí, vzorka), **expert** (posúdenie postupu),
   **pilot** až po potvrdení problému kontaktom; aj organizácia cez funkčný kontakt bez vymyslenej osoby.
   Pri ponuke výstupu komerčnému adresátovi kód pridá vetu: „Príprava dema a dohodnutá skúška sú
   bezplatné. O platenom používaní by sme sa dohodli až vtedy, keď by riešenie fungovalo vo vašej
   prevádzke a preukázateľne vám prinášalo hodnotu.“ (`outreach.value_offer`). Každý návrh ukazuje
   reťazec prečo tento kontakt → predpokladaná potreba → čo môže dostať → čo minimálne potrebuješ → čo
   ešte nevieme (v15: **ponuka zákazníkovi pochádza z karty jeho organizácie** s verziou; bez karty iba situačná
   otázka, všeobecný návrh hypotézy je len označený podklad) a pripravenosť: **obstarávanie neoverené** (schválenie
   čaká na tvoje overenie), **chýba podklad o kontakte** (zdroj dôvodu výberu – schválenie zablokované),
   **čaká na hodnotiteľa** (napr. výpadok Azure; môžeš potvrdiť „Skontroloval som ručne“), **návrh na tvoju
   kontrolu**, **pripravené na ručné odoslanie**, **blokované** (odmietnutie, potlačenie, vylúčená organizácia).
   Prvé oslovenie má jednu hlavnú otázku; ostatné sú v „Príprave rozhovoru“. E-mail (varianty A podľa dôkazu
   a B podľa funkcie), poznámka pre LinkedIn (≤ 200 znakov), skript na telefón. Výsledky kontrol (dĺžky,
   zakázané frázy, potreba podaná ako fakt, čísla mimo faktov, „nejde o ponuku“ pri ponuke, pätička)
   a viet, ktoré hodnotiteľ považuje za nepodložené. Uprav, „Uložiť a skontrolovať“,
   „Schváliť“ (max. 10 denne), „Kopírovať“ / „Otvoriť v pošte“, pošli sám a označ „odoslané“.
   Follow-up pripadne na 5. pracovný deň. Námietka = okamžitý výmaz a potlačenie. Pri odoslanom oslovení
   **zapíš výsledok**: odpoveď, či kontakt problém potvrdil alebo vyvrátil, vzorka, rozhovor, demo, pilot,
   hodnota, platba, ďalší krok, čas prípravy a dodania. Potvrdenie mení stav potreby (iba pre danú
   organizáciu – ostatným ide naďalej podmienená otázka) a pri najbližšom kroku `value` vznikne nová verzia
   prehľadu domény. Metriky („Učenie z odpovedí“, Prehľad) sa počítajú po organizáciách, nie po správach.
7. **Event log** (GUI → Event log a procesy, alebo tlačidlo pri oslovení, kde kontakt potvrdil problém): žiadosť
   o históriu krokov procesu – prečo práve event log, definícia prípadu, minimálna schéma (ID prípadu, udalosť,
   čas vzniku udalosti; termín pri hypotéze o meškaní), slovník a malá ukážka niekoľkých celých prípadov, čo z toho
   pripravíš a podmienky bezplatnej skúšky. Správa je pevná šablóna a ide do Oslovení na kontrolu a ručné
   odoslanie. Ak históriu nemajú: prehliadka prípadu, schéma stavov, agregovaný report alebo manuálne zaznamenávanie.
   **Import** (karta „Import a kvalita“): CSV alebo Parquet do oddeleného priestoru organizácie
   (`data/eventlogs/org_<id>.duckdb`, nič neopúšťa počítač), rovnaký súbor sa nezdvojí. Systém navrhne mapovanie
   (case_id, activity, timestamp, …; čas exportu ani mená pracovníkov nie), ty ho potvrdíš spolu so zónou, obdobím,
   začiatočnými a koncovými aktivitami. Report kvality: chýbajúce hodnoty, neplatné časy, prechody letného času,
   duplicity, zhodné časy, start/complete, otvorené a odrezané prípady, výber, premenovania, snapshot – s počtami,
   podielmi a rozhodnutím; nič sa nemaže potichu. Tabuľka „dostupná“ povie, ktoré analýzy dáta podporia a prečo.
   **Analýza** (karta „Analýza“ alebo `python -m radar process-report --import 1 [--review]`): počty, frekvencie,
   najčastejšie priebehy a priamo nadväzujúce kroky (iba z jednoznačne usporiadaných prípadov), odstupy a trvania
   (kalendárny čas; dokončené zvlášť od otvorených a odrezaných), opakovanie kroku, čas aktivity a odchýlka od
   termínu, ak to dáta podporia – s menovateľmi a obmedzeniami a najviac 3 miestami na preverenie v rozhovore.
   Report nedokazuje príčinu ani úsporu. Hodnotiteľ posúdi iba formulácie (agregáty, nie surový log).
8. **GUI → Rozhovory:** formulár podľa The Mom Test; citáty z poznámok sa stanú dôkazmi (iba ak sú
   v poznámkach doslova). **GUI → Brána 0:** rozhovory/cieľ, „spomenul sám“, platiteľ, lievik,
   návrh verdiktu; rozhodnutie zapisuješ ty.

LinkedIn: ukladá sa iba URL profilu (kanonická, bez parametrov) s označením „over ručne“ – z odkazu
na webe organizácie v bloku osoby, z výsledku vyhľadávania (celé meno v titulku a názov organizácie)
alebo zadaná ručne v GUI. Stránka LinkedIn sa nikdy nesťahuje (zakazujú to podmienky); bezplatný
účet má limit 200 znakov na poznámku a približne 5 personalizovaných pozvánok mesačne.

### Nočný beh

`run_nightly.bat` (alebo `scripts\run_weekly.ps1 -Minutes 480`) spustí Docker a SearXNG, skontroluje
modely a spustí autopilota „Nechať bežať všetko“ ako úlohu Správcu akcií (`python -m radar job autopilot
--mode queue --wait`; `-Action run-all` pre jeden celý beh):
v GUI vidíš priebeh a ak práve beží akcia z GUI, nočný beh sa zaradí do poradia za ňu. Profil a modely
platia z GUI (Správca akcií → Modely). Naplánovanie (registruješ ty, mení nastavenia Windows):
`scripts\register_weekly_task.ps1 -Daily -Time 23:00 -Wake` (každý deň s prebudením; vo Windows musia byť
povolené časovače prebudenia), bez `-Daily` raz týždenne (`-Day Sunday`). Na batériu sa beh nespustí;
prerušenú akciu spustíš v Správcovi akcií tlačidlom „Spustiť znova“.
**Bez čakania na nočný beh:** Správca akcií → „Nechať bežať všetko“ → **Spustiť všetko hneď (nečakať na nočný
beh)** – to isté ako nočný beh, len teraz: rozbehne zastavené poradie, znova zaradí prerušené úlohy z posledných 12 h
a zaradí autopilot na zvolený počet hodín (Docker a Ollama musia bežať; nočný beh ich zapína sám). Keď už jeden
autopilot čaká v poradí, tlačidlo ani nočný beh nepridajú druhý – iba mu predĺžia koniec.
Rozdelenie času: ~45 % rýchly model a práca bez modelu, zvyšok presný model v poradí priority
(ľudia a návrhy → overenie dôkazov → zaradenie → kritika a trh). Čo sa nestihne, pokračuje
pri ďalšom behu.

## Stav hypotézy a postup k zákazníkovi

Overený citát nie je overený trh. Stránka **Problémy → Osi a obchodný stav** ukazuje štyri samostatné osi –
literatúra (overené tvrdenia z nezávislých domén, bez dodávateľov), zákazník (rozhovory, nevyžiadané spomenutie),
dáta (Brána 1 s potvrdenými stĺpcami) a ochota platiť – a obchodný stav, ktorý zapisuješ ty: preukázaný problém,
vlastník procesu, vzorka, nameraná úspora, pilot, platiteľ (každý krok s dôkazom). **Prehľad** ukazuje postup
k zákazníkovi (kontakty, odpovede, rozhovory, vzorky, piloty); počet hypotéz je iba prevádzková metrika.

**Ochota platiť (M35):** „kto platí“ nie je „ochota platiť“. Os má stupne s dôkazom po organizáciách – platiteľ
identifikovaný → rozpočet overený → záujem o kúpu za konkrétnych podmienok (čo, rozsah, cena alebo rozpätie, termín) →
zaplatené (suma, mena, jednorazovo / mesačne / ročne, hodnota zmluvy); zvlášť aj „nezaplatia / nemajú rozpočet“.
**Potvrdená** je až záujem o kúpu alebo platba. Výsledok oslovenia s potvrdeným problémom musí mať zapísané aj
posúdenie ochoty platiť (aspoň „nepýtali sme sa“); „platené“ bez sumy sa neuloží. Zapisuješ vo výsledku oslovenia,
v odpovedi organizácie (karta), v rozhovore alebo v Problémy → Osi a obchodný stav (aj odvolanie zápisu). Prehľad
príležitosti má otázky na každý stupeň (podľa The Mom Test – dnešné náklady a konkrétny ďalší krok, nie „kúpili by
ste…“) a Koho osloviť stĺpec „ochota platiť“. `forget` zmaže pri obchodnom dôkaze väzbu na osobu aj text, fakt
o organizácii ostane.

V **Koho osloviť** má každý kontakt účel s dôkazom: vlastník procesu (funkcia zodpovednosti zverejnená organizáciou),
používateľ riešenia, správca dát, schvaľovateľ rozpočtu, expert. Politici a členovia predstavenstiev nie sú vlastníci
procesu; osoba spomenutá v článku nemá doloženú kompetenciu.

**Autopilot** plánuje najprv tvoje dáta a rozpracované vetvy (najviac 3 prioritné hypotézy), nový zber dostane
vyhradený podiel kola (25 %); akcia, ktorá sa 2 kolá nezmestila, ide dopredu.

## Presnosť a eval

**Referenčná vzorka** (Eval): 30 mapovaní polí, 20 importov, 20 účelov kontaktov a 30 rozhodnutí o dôkazoch, ktoré
označíš ty; 20 % je kontrolná časť, podľa ktorej sa neladia prompty.

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
(`scout`, `validate`, `people`, `contacts`, `drafts`, `evidence`, `topics`, `critique`, `themes`, `gate1`, `gate1-input`,
`gate2`, `bench`, `autopilot`, `funnel`, `run-all`).

### Lievik: hypotéza po hypotéze (donekonečna)

Na rozdiel od autopilota (kolá akcií pre všetky hypotézy naraz) **lievik** vedie jednu hypotézu čo najďalej a až potom
berie ďalšiu. Jedna úloha = jeden krok jednej hypotézy; na konci zaradí ďalšiu úlohu, takže beží **donekonečna**
(alebo do zvoleného času):

```
nájsť (scout) → otestovať (agent + posúdenie; ďalej iba „podložená“) → kontakty (Brána 0, s LIA)
            → Brána 1 (dáta; pripravenosť ≥ 0,5 → Brána 2, inak čaká na tvoje dáta) → Brána 2 (karta, experimenty, záver)
```

- **Ktorú hypotézu:** rozpracovanú (fokus) → tie, čo už prešli testom (najďalej v lieviku najskôr) → neotestované
  (poradie fronty testu) → keď niet čo testovať, hľadá nové; po každých `workflows.funnel_find_every` (3) testoch hľadá
  nové aj tak.
- **Kde zastaví a čaká na teba:** Brána 1 „čaká na tvoje dáta“, návrh experimentu čaká na uzamknutie (ak nemáš zapnuté
  automatické uzamknutie), v dátach chýba, čo experiment potrebuje, ručne pozastavená brána. Zoznam „Čaká na teba“
  je v Správcovi akcií. Lievik medzitým pokračuje ďalšou hypotézou.
- **Zaseknutý krok:** krok, ktorý dvakrát nezmení stav hypotézy (`funnel_max_attempts`) alebo beží 4× (`funnel_max_runs_per_step`),
  sa pre ňu odloží; lievik sa k nej vráti, keď sa stav zmení (napr. vložíš dáta).
- **Zastavenie:** „Zastaviť úlohu“ pri bežiacom kroku (ďalší sa nezaradí), čas z „Ako dlho“, alebo 3 zlyhané kroky za
  sebou (nebeží Ollama alebo SearXNG – `doctor`).
- Spustenie: Správca akcií → „Lievik: hypotéza po hypotéze“ (0 h = donekonečna), alebo
  `python -m radar job funnel --minutes 0` (`--minutes 480` = 8 h). Časové limity krokov: `workflows.funnel_minutes`.
- Každý krok je v tabuľke `funnel_log` (hypotéza, krok, výsledok, čo ďalej) – posledné kroky ukazuje Správca akcií.
- Na 8 GB VRAM sa modely pri jednej hypotéze striedajú častejšie ako v autopilotovi (rádovo 6–8 výmen na hypotézu,
  ~1 min každá). Údržbu (index katalógu, témy, kritika, návrhy oslovení) robí autopilot alebo jednotlivé akcie.

## Brána 1 (dáta) a Brána 2 (experimenty)

**Brána 1** beží pre hypotézy vybrané na Bránu 0, súbežne s rozhovormi (GUI → Brána 1 – dáta, alebo akcia
„Brána 1: prieskum dát“): polia potrebné na riešenie (kľúčové polia) → zdroje (licencia a frekvencia iba
z prečítanej stránky) → kontrola a kvalita → stiahnutie do DuckDB (`data/datasets`, limit 2 GB na súbor,
50 GB spolu) → zreprodukovanie známeho čísla z overeného tvrdenia (model navrhne SQL, počíta DuckDB) →
návrhy žiadostí podľa zákona 211/2000 a pripravenosť dát so zložkami. Chýbajúce dáta sú príznak, nič sa
nevyraďuje.

**Dôvera v dáta (od 15. 10. 2026):** súbor zdroja sa overí skutočnou požiadavkou (nefunkčný odkaz ani stránka
prihlásenia nie je zdroj); import ide podľa obsahu (HTML sa odmietne; SpreadsheetML, RDF/JSON-LD dátové kocky
s dimenziami a jednotkou, výkazy XLSX/CSV s nadpisom a dvojriadkovou hlavičkou) a každý dataset má importný report
(načítané a odmietnuté riadky, typy, kľúče, jednotky, vzorka). Import s problémom čaká **na tvoju kontrolu**
(Brána 1 → Datasety) a nič sa z neho nepočíta. Stav poľa je rebrík: iba odkaz → súbor dostupný → dáta načítané →
stĺpec overený kódom. **Mapovanie polí** (Brána 1 → Mapovanie polí, krok `gate1-bindings`): presný model navrhne
stĺpec, kód overí pokrytie, typ a pojem (dátum exspirácie nie je dátum patentu), význam kľúčového poľa potvrdíš ty
(alebo zadáš stĺpec sám). Interné dáta organizácie (napr. tikety) sú „od partnera“ – web sa pre ne neprehľadáva.

**Prechod do Brány 2:** pripravenosť dát ≥ 0,5 **a každé kľúčové pole má stĺpec potvrdený tebou** na aktuálnej
verzii datasetu → Brána 1 prejde automaticky (rozhodnutie s dôvodom, metrikami, autorom a odtlačkom dát). Keď
podmienky prechodu pravidlom neskôr prestanú platiť (zamietnutý dataset alebo stĺpec), hypotéza dostane „treba znova
overiť“; tvoje ručné rozhodnutie ostane s príznakom (`python -m radar gate1-revalidate` alebo tlačidlo
„Preveriť prechod s aktuálnymi dátami“). Podhypotéza dedí dáta, nie prechod. Inak hypotéza „čaká na tvoje dáta“: v GUI (Brána 1 → Tvoje dáta) napíš popis dát alebo aspoň schému (stĺpce,
jednotky, obdobie, odkiaľ) a prípadne nahraj súbor (CSV, XLSX, JSON, Parquet, ZIP, XML). Akcia „Brána 1: posúdiť tvoje
dáta“ (aj v autopilotovi) súbor naimportuje do DuckDB (tabuľka s osobnými údajmi sa neuloží, súbor sa zmaže), sudca
posúdi každé pole (áno / čiastočne / nie) a kód spočíta vhodnosť (kľúčové polia spolu 0,7, ostatné 0,3); polia
dostanú stav „dáta načítané“ (súbor) alebo „iba opis schémy“ a prechod ide cez potvrdenie stĺpcov. Kedy Brána 2 naozaj beží, určuje akcia, ktorú spustíš ty alebo autopilot. Samotná schéma na prechod
stačí, spätný test však potrebuje skutočné dáta. Nevkladaj dáta zamestnávateľa. Prahy: `gate1.auto_gate2_readiness`,
`gate1.user_data_threshold`; ručné rozhodnutie (prešla, pozastaviť, zastaviť) má vždy prednosť.

Zdroje hľadá agent aj v **Národnom katalógu otvorených dát** (data.slovensko.sk, verejný SPARQL, ~22 000
datasetov) – bez vyhľadávača: poskytovateľ, licencia z podmienok užitia, periodicita, formáty a odkazy na
stiahnutie priamo z metadát. Pre každé pole sa ešte pred agentom načítajú 2 kandidáti; kontrola zdroja berie
súbory z distribúcií v katalógu. Licencie **NC/ND** (iba nekomerčné použitie, bez úprav) majú v kvalite zdroja
nižšie skóre a príznak – over, či sa hodia pre tvoj produkt. Vypnutie: `gate1.nkod_enabled: false`.

**Brána 2** (hypotézy, ktoré prešli Bránou 1):

1. **Problémová karta** – problém jednou vetou, kto ho má, aké rozhodnutie robí, ako dnes, aký výstup pomôže,
   obchodná metrika; každé tvrdenie s citátom, sudca overí kľúčové polia, ty kartu potvrdíš alebo upravíš.
   Široká hypotéza (citáty o rôznych problémoch) sa **rozdelí na podhypotézy**: citáty sa presunú, dáta Brány 1 sa
   zdedia (zdieľaná DuckDB). Akcia „Problémové karty a rozdelenie hypotéz“ (aj v autopilotovi).
2. **Návrh experimentov** – model (presná rola) z karty a profilu dát navrhne niekoľko experimentov
   **podľa hypotézy**: typ úlohy (poradie, predpoveď, anomália, prah, porovnanie) a prípravu dát z všeobecných
   operácií (filter, odvodený stĺpec, kategória textu, prepojenie záznamov, história do dňa riadku, budúca udalosť
   ako štítok). Kód návrh skúšobne spustí (stĺpce, únik budúcich dát, počet pozitívnych prípadov, dĺžka radu)
   a chyby vráti modelu na opravu.
3. **Predregistrácia** – uzamkneš ty (hlavná metrika, hlavný naivný odhad, prah úspechu).
4. **Spätný test** (Python: scikit-learn, statsmodels) – výsledková tabuľka **všetky postupy × všetky metriky**,
   model vybraný na validačnom období, interval bootstrap, položky na kontrolu s rozpadom, protokol prípravy dát.
5. **Záver pre problém** – čo výsledok znamená pre rozhodnutie z karty, obmedzenia, otázky do rozhovorov
   Brány 0; čísla iba z faktov (inak záver poskladá kód). Správa HTML.

**Výsledok má štyri osi** (od 15. 10. 2026): (a) predregistrované pravidlo, (b) prínos oproti silnému jednoduchému
postupu vybranému na validácii pred testom (napr. doterajšia miera entity), (c) metodická platnosť a (d) výsledok
u používateľa (zapíšeš ho v Bráne 2 po pilote). Verdikt „prínos“ znamená iba prínos v dátach; experiment s metodickou
chybou (neznámy čas zverejnenia štítka, štítok subjektu pri rozhodovaní o zmluvách, subjekty bez kontroly ako
negatívne prípady) sa nedá uzamknúť. Príznaky zo štítkov sa počítajú po časových foldoch a štítok sa použije, až keď
je známy (okno + oneskorenie zverejnenia) – bez úniku validačných a testovacích odpovedí.

Pôvodné experimenty (predpoveď sumy pre organizáciu) ostávajú zobrazené s postupom krok po kroku.
`python -m radar crz-extend --hypothesis 11 --months 36` doplní staršie dávky CRZ (po blokoch, dá sa prerušiť;
`--retry` znova stiahne dni, ktoré zlyhali pre výpadok siete).

**Štítky z kontrol:** `python -m radar dataset-add --hypothesis 11 --url https://www.nku.gov.sk/datasety` (alebo GUI
Brána 1 → Datasety → Pridať dataset z odkazu) stiahne kontrolné zistenia NKÚ s IČO subjektu. Operácia `lookup` ich v
experimentoch pripojí k riadkom podľa IČO a času (napr. „zistenie NKÚ u objednávateľa do 2 rokov po podpise“).
**Automatické uzamknutie** bezchybných návrhov (Brána 2 → Nastavenie) je predvolene vypnuté.

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

- **Nechať bežať všetko** (Správca akcií, navrchu; predvolene 8 h): autopilot v kolách – v každom kole
  zistí, kde je práca, a zaradí samostatné akcie v poradí nové hypotézy → zber k témam → test hypotéz → posúdenie
  dôkazov → témy → ľudia → kontakty → návrhy oslovení → Brána 1 → Brána 2 → kritika; akcie bez práce preskočí.
  Ľudí zbiera iba pre hypotézy zapnuté na Bránu 0. Zastavíš ho „Zastaviť všetko vrátane poradia“.

Úloha beží ako samostatný proces (aj po zatvorení prehliadača), naraz iba jedna (GPU), dá sa
zastaviť. Priebeh a koniec výstupu (maskovaný) sú v Prehľade, celý výstup v `logs/jobs/`.

## Objavovanie nových problémov

30 % dokumentov behu ide mimo tém: katalóg `sources.yaml` (NKÚ, revízie výdavkov, legislatíva,
EUR-Lex, ÚVO a PTK, CRZ, zväzy, výročné správy z RÚZ) a rotujúca matica „vzor bolesti × odvetvie“.
Medzi kategórie sa delí Thompson samplingom (úspech = dokument s overeným tvrdením). Zdroj z
katalógu sa použije až po potvrdení (`sources --check`, potom `verified: true`); od 12. 10. 2026 je povolených
146 overených zdrojov (úrady, inšpekcie, analytické útvary, zväzy, komory, kraje, odborné médiá, EÚ), 69 cez RSS.
Zoznam a výťažnosť (dokumenty a overené tvrdenia podľa kategórie a zdroja) sú v GUI na stránke **Zdroje**.
Nový problém je najprv „candidate“; hypotézou sa stane až s 2 overenými tvrdeniami z 2 domén.
Regulačné povinnosti s termínom a signály dopytu (PTK, výzvy) majú v správe vlastné sekcie.

### Obstarávanie z TED (M37)

Akcia **„Obstarávanie z TED (kto čo kupuje)“** (Správca akcií; autopilot raz za týždeň; `python -m radar job
procurement`) stiahne štruktúrované oznámenia slovenských kupujúcich z TED Search API (verejné, bez kľúča; CPV v
`config.yaml` → `procurement.cpv`, predvolene IT služby 72, softvér 48, podnikové služby 79; prvý beh 3 roky ≈ 4 100
oznámení). Ukladá sa iba zákazka a organizácie (kupujúci s IČO, predmet, CPV, dátumy, hodnoty, víťaz – názov iba pri
právnickej osobe); osoby, e-maily, telefóny a adresy nie. Potom k hypotézam priradí podobné zákazky podľa významu
(embeddingy na CPU; podobnosť je pri každej zhode, prah `procurement.match_threshold`) a vypočíta signály dopytu:
opakovaný nákup u kupujúceho, počet nezávislých kupujúcich, otvorené a plánované zákazky, služba vs. softvér, hodnoty
v EUR, menší kupujúci s podobnou potrebou (Problémy → **Obstarávanie (TED)**, riadok v Prehľade príležitosti,
`python -m radar procurement --hypothesis N`). Stav obstarávania organizácie v Bráne 0 sa určí z konkrétneho záznamu
podľa IČO kupujúceho: otvorená výzva → potvrdené (iba oficiálna cesta), predbežné oznámenie → kandidát, zadaná →
ukončené; tvoje overenie sa neprepíše a odhad z vyhľadávania nahradí iba otvorená výzva. **TED má iba zákazky nad
limitmi EÚ** – chýbajúci záznam neznamená, že nikto nekupuje (ÚVO a EKS ostávajú cez vyhľadávanie).

## Cloudové modely (Azure OpenAI, voliteľné)

Bez nastavenia beží všetko lokálne ako doteraz. Cloud sa zapína po rolách: napr. generátor ostane lokálny
a **hodnotiteľ** (`reviewer`) beží v Azure – generátor nie je overovateľ.

1. **Kľúč** daj do premennej prostredia (PowerShell: `setx AZURE_OPENAI_API_KEY "<kľúč>"`, potom nový terminál).
   Do `config.yaml`, databázy ani GUI sa kľúč nezadáva – iba názov premennej.
2. **GUI → Správca akcií → Cloudové modely:** profil (endpoint `https://<resource>.openai.azure.com/openai/v1/`,
   názov premennej s kľúčom). Nasadenia: **„Obnoviť nasadenia“** ich iba prečíta cez Azure Resource Manager
   (vyžaduje subscription, resource group, názov resource a token s rolou aspoň Reader v premennej
   `AZURE_MGMT_TOKEN`, napr. `az account get-access-token --query accessToken -o tsv`), alebo **pridaj existujúce
   nasadenie ručne** (názov deploymentu z Azure portálu). Nič sa v Azure nevytvára ani nemení.
3. **Ktorá rola beží kde:** výber podľa modelu, verzie a názvu nasadenia (dve nasadenia toho istého modelu
   sú rozlíšené); prežije reštart. Do Azure sa posiela názov nasadenia. Reasoning model dostane
   `max_completion_tokens` bez `temperature`; lokálne prepínače uvažovania a uvoľňovanie GPU sa do Azure neposielajú.
4. **Rozpočet a sadzby:** tvoj rozpočet (napr. 85,87 € podľa zostatku kreditu; 0 = cloud vypnutý), denný limit
   a limit na beh; sadzby vstup/výstup za 1M tokenov pre každé nasadenie z cenníka Azure s dátumom overenia.
   Bez sadzby sa nasadenie nevolá (cena sa nenahrádza nulou).
5. **Otestovať pripojenie:** jedno krátke volanie (desiatky tokenov, započíta sa); text a JSON schéma zvlášť.
   Do zoznamu sa zapíše iba úspešne overená schopnosť.

**Čo znamenajú čísla o nákladoch:** pred volaním sa rezervuje horný odhad (vstup + maximálny výstup) v jednej
transakcii s limitmi – aj pri súbežných volaniach a opakovaniach; po odpovedi sa vyrovná podľa skutočných
tokenov (reasoning tokeny sú súčasťou výstupu). Timeout = neistý výsledok, rezervácia ostáva započítaná.
Zobrazená spotreba je **vlastný odhad, nie overený zostatok ani faktúra Azure** – zostatok over v Azure portáli.

**Chyby:** autentifikácia, nenájdené nasadenie, nepodporovaný parameter, limit rýchlosti (najviac
`cloud.max_retries` opakovaní), kvóta, rozpočet – položka čaká na ďalší beh, nikdy sa automaticky neschváli
a neprejde sa na iný (drahší) model. Jednoduchší JSON režim iba pri potvrdenej nepodpore `response_format`.
Posledný dôvod prerušenia je v Správcovi akcií. Rovnaký vstup, nasadenie a verzia promptu = odpoveď z cache.

**Čo smie opustiť počítač:** iba podklady úloh z `cloud.allowed_tasks` – verejné zdroje a citáty (prehľad
domény, návrh prínosu a ich kontrola, väzby polí Brány 1, posúdenie dôkazov, karty, závery experimentov,
kritika). Úlohy s menami a kontaktmi (návrhy oslovení, ich kontrola, kontakty) idú do cloudu iba
s `privacy.cloud_personal_data: true` (vtedy doplň LIA o spracovateľa); inak čakajú. `doctor` cloudové roly iba
vypíše (kľúč, sadzba, rozpočet), nevolá ich.

**Azure for Students:** kredit a podmienky sú určené na štúdium; komerčné použitie (napr. pilot so zákazníkom)
nepredpokladaj – over ho v podmienkach predplatného.

## Súkromie a bezpečnosť

- Iba pracovné kontakty zverejnené organizáciou alebo osobou (alebo v úradnom zverejnení – napr. kontaktný bod
  datasetu v Národnom katalógu), vždy so zdrojovou URL a úryvkom. E-maily sa nehádajú, schránky sa neoverujú
  (SMTP), obfuskované adresy sa nedekódujú. Ručne zadaný kontakt sa uloží, iba ak ho kód nájde na zadanej stránke.
- Osobné údaje iba k hypotézam na Bráne 0: ručne zvolené, top 3 podľa sily a každá s testom „podložená“
  (`people.auto_select_validated: false` to vypne). Pri tejto zmene treba revidovať `data/LIA.md`
  (šablóna `docs/LIA_template.md`: nový zdroj, účel „vzorka dát“, vyšší objem).
- Sociálne siete sa nesťahujú ani po presmerovaní. Z RPO sa ukladá iba meno a funkcia štatutára
  (adresa sa odstráni ešte pred uložením do cache).
- Surové HTML kontaktných stránok sa neukladá; nevybraní kandidáti sa vymažú v tom istom behu.
- Retencia: neoslovení 90 dní, po poslednej interakcii 12 mesiacov, odmietnutí 30 dní, exporty
  30 dní (`purge`, aj na konci `run-all`). Námietka/výmaz: `forget` + hash v zozname potlačenia
  (soľ v `data/.salt`; po obnove zo zálohy `privacy reapply`).
- Logy maskujú e-maily a telefóny; správy a exporty predvolene pseudonymizujú osoby.
- GUI aj služby počúvajú iba na 127.0.0.1; Streamlit bez telemetrie (`.streamlit/config.toml`).
- Cloudový model (voliteľný) dostane iba podklady povolených úloh; kľúč iba v premennej prostredia, nikdy
  v configu, databáze, logoch ani promptoch (pozri Cloudové modely).
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
| `radar/db/` | SQLite: `evidence.py` (register dôkazov), `gate0.py` (organizácie, ľudia, oslovenia, rozhovory), `ops.py` (provenance, kontrola, labely, eval), `value.py` (prehľady domén, návrhy prínosu, výsledky oslovení, náklady a cache cloudu), `schema.py` + `migrations.py` |
| `radar/value.py` | prehľad domény a návrh prínosu (citáty, neznáme, hodnotiteľ, verzie) |
| `radar/outcomes.py` | výsledky oslovení, stav potreby, metriky po organizáciách |
| `radar/domain.py` | stav doménových podkladov, priority domén, tvrdenia a otázky s blokovanou akciou, hlavná otázka |
| `radar/domain_sources.py` | lokálny ESCO index a fulltext, rola → povolanie, ohraničený doménový výskum (web) |
| `radar/event_logs.py` | žiadosť o event log (šablóna, minimálna schéma, alternatívy) a import s kontrolou kvality |
| `radar/process_analysis.py` | procesný report (podporované metriky, menovatele, obmedzenia, miesta na preverenie) |
| `radar/cloud.py` | Azure OpenAI: klient, triedenie chýb, rozpočet s rezerváciou, nasadenia, test pripojenia |
| `radar/models.py`, `radar/llm.py`, `radar/prompts.py` | roly a routing úloh, self-consistency, provenance; klient Ollama/OpenAI; prompty s verziou |
| `radar/collect.py`, `discover.py`, `fetch.py`, `search.py` | zber, objavovanie, sťahovanie (robots.txt, blokované domény) |
| `radar/extract.py`, `verify.py`, `cluster.py`, `critic.py`, `market.py`, `report.py` | pipeline dôkazov |
| `radar/registers.py`, `orgs.py`, `contacts.py`, `names.py`, `people.py` | RPO/RÚZ, organizácie, kontakty, ľudia a priorita |
| `radar/funnel.py` | lievik: ďalší krok hypotézy podľa jej stavu, výber hypotézy, jeden krok na úlohu, reťaz úloh |
| `radar/contact_scout.py`, `radar/shortlist.py` | hľadanie kontaktov podľa hypotézy (plán, dopyty, držitelia dát, ručné pridanie zo stránky); Koho osloviť (odporúčanie s dôvodmi, stav komunikácie, fronta prvého oslovenia, čo dostanú, potrebný vstup) |
| `radar/outreach.py`, `crm.py`, `privacy.py`, `review.py`, `evals.py`, `bench.py` | oslovenia podľa účelu (zákazník, držiteľ dát, expert, pilot) s reťazcom dôvodov a pripravenosťou, Brána 0, súkromie, kontrola, eval, benchmark |
| `radar/pipeline.py`, `cli.py`, `commands.py` | run-all, príkazový riadok |
| `radar/gui/` | Streamlit (`app.py`, `app_pages/`) |
| `tests/` | testy bez modelu a siete (`python -m pytest -q`) |
