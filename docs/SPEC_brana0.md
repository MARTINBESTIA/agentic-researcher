# Špecifikácia v0.2 – Brána 0 agregátor

Stav: návrh na implementáciu · dátum: 10. 10. 2026 · nadväzuje na v0.1 (kód v `radar/`).
Záväzné pravidlá projektu sú v `CLAUDE.md`. Tento dokument hovorí, čo postaviť a ako overiť, že to funguje.

Kľúčové slová: **MUSÍ** a **NESMIE** sú povinné. **MALO BY** je odporúčanie; odchýlku zapíš do
`docs/DECISIONS.md`. **MÔŽE** je voliteľné.

## Obsah

1. Kontext a ciele
2. Čo nerobíme
3. Slovník
4. Architektúra v0.2
5. Presnosť: roly modelov, kaskáda, eval (R-ACC)
6. Organizácie (R-ORG)
7. Ľudia a kontakty (R-PPL)
8. Návrhy oslovení (R-OUT)
9. Brána 0: rozhovory, metriky, rozhodnutia (R-CRM)
10. Objavovanie nových problémov (R-DISC)
11. GUI (R-GUI)
12. Súkromie, právo, bezpečnosť (R-PRIV)
13. Dátový model (migrácia 2)
14. Príkazy CLI
15. Konfigurácia
16. Míľniky a akceptačné kritériá
17. Otvorené otázky
18. Hardvér a výkon
19. Prílohy: A llama-swap · B sources.yaml · C príklady oslovení · D otázky na rozhovor

---

## 1. Kontext a ciele

### 1.1 Východisko (v0.1)

Pipeline `collect → extract → verify → cluster → critique → market → report` nad SQLite, jeden lokálny
model (Qwen3.6-35B-A3B cez llama-server), vyhľadávanie cez SearXNG. Výstup `reports/latest.md`
obsahuje hypotézy, overené dôkazy s citátmi, protiargumenty, otázky na rozhovory a rádový odhad trhu.
Témy sú pevne v `config.yaml` (4 témy).

### 1.2 Požiadavky používateľa

- **A. Brána 0 agregátor.** Ku každému problému nájsť ľudí, ktorí s ním súvisia (vedúci, zástupca…),
  ich priamy pracovný kontakt (telefón, e-mail, LinkedIn) a pripraviť návrhy oslovenia. Všetko
  v lokálnych tabuľkách s grafickým prehliadaním (GUI).
- **B. Nielen doterajšie témy.** Systém má hľadať aj iné problémy a ukladať ich do tabuľky.
  Ľudí hľadať aj k pôvodným 4 témam.
- **C. Vyššia presnosť** s väčším modelom; spomalenie 10–30× je prijateľné.

### 1.3 Ciele v0.2

| ID | Cieľ | Kritérium |
|---|---|---|
| G1 | Zoznam ľudí na rozhovor pre vybrané hypotézy | cieľ: pre každú z 3 vybraných hypotéz ≥ 10 kandidátov z ≥ 5 organizácií; každý kontakt má zdrojovú URL a úryvok |
| G2 | Návrhy oslovení na ručné odoslanie | každý kandidát s kontaktom má ≥ 1 návrh, ktorý prešiel kontrolami; 0 automaticky odoslaných správ |
| G3 | Evidencia Brány 0 | rozhovory, citáty z nich ako dôkazy, metriky brány a rozhodnutie pre každú hypotézu |
| G4 | Nové problémy mimo tém | ≥ 30 % dokumentov za beh z objavovania; nové hypotézy majú pôvod `discovery` |
| G5 | Merateľná presnosť | eval na ≥ 50 ručne označených položkách na úlohu porovná 4 profily modelov |
| G6 | GUI | všetky tabuľky prehliadateľné a filtrovateľné v prehliadači na 127.0.0.1 |

## 2. Čo nerobíme

- Automatické odosielanie e-mailov, správ a pozvánok, sekvencie, sledovanie otvorení, hromadné kampane.
- Sťahovanie LinkedInu a sociálnych sietí, platené databázy kontaktov, „data brokers“, hádanie
  e-mailov, SMTP overovanie schránok.
- Predajné CRM. Brána 0 slúži na učenie sa od ľudí, nie na predaj.
- Cloudové API, telemetria, prístup ku GUI zo siete.
- Právne poradenstvo. Právne časti sú technický podklad; pred väčším objemom oslovení sa poraď s právnikom.
- Akékoľvek dáta, systémy alebo kontakty zamestnávateľa používateľa.

## 3. Slovník

| Pojem | Význam |
|---|---|
| Brána 0 | Kontrolný bod pred stavaním produktu: 8–12 rozhovorov so zákazníkmi; aspoň 3 z 10 opíšu problém sami, bez navádzania; je jasné, kto by platil. |
| Hypotéza (problém) | Jeden problém jedného segmentu (`hypotheses`). |
| Tvrdenie (claim) | Výrok s doslovným citátom zo zdroja (`claims`), stav `pending / verified / partial / rejected`. |
| Organizácia | Firma alebo inštitúcia, ktorá môže mať problém (`organizations`). |
| Osoba | Človek v organizácii s väzbou na problém (`people`). |
| Kontaktný bod | E-mail, telefón, URL profilu alebo formulára so zdrojom (`contact_points`). |
| Väzba osoba–problém | Prečo je osoba relevantná: typ a dôkaz (`person_problem_links`). |
| Oslovenie | Návrh správy pre jeden kanál a jeho stav (`outreach`). |
| Potlačenie (suppression) | Hash identifikátora osoby, ktorú systém už nesmie zbierať ani oslovovať. |
| Rola modelu | `fast` (rýchly MoE model), `accurate` (väčší hustý model), `embed` (embeddingy). |
| Profil | Mapovanie úloh na roly a nastavenia (napr. `balanced`). |
| Kontrola (review queue) | Položky, o ktorých musí rozhodnúť človek: nezhoda modelov, orezaný výstup, neistota. |
| Gold label | Ručné rozhodnutie človeka, slúži ako pravda pre eval. |
| PTK | Prípravné trhové konzultácie podľa § 25 zákona č. 343/2015 Z. z. o verejnom obstarávaní. |

## 4. Architektúra v0.2

```
 zdroje: config.yaml témy · sources.yaml · registre (RPO, RÚZ) · vyhľadávanie (SearXNG)
   │
 [bez modelu]  collect + discover                    → documents
 [fast]        screen → extract                      → claims (pending)
 [kód]         verify: citát a čísla v zdroji?       → rejected / ďalej
 [embed]       embeddingy tvrdení a hypotéz
 [accurate]    verify: vyplýva tvrdenie z citátu?    → verified / partial / rejected / kontrola
 [accurate]    cluster + novosť                      → hypotheses (origin topic / discovery / manual / interview)
 [accurate]    critique, market (štruktúra; čísla počíta Python)
 ── iba hypotézy vybrané na Bránu 0 (people_enabled) ──
 [kód + fast]  orgs: z dôkazov, obstarávania, registrov   → organizations, org_hypotheses
 [kód + fast]  people: kontaktné stránky, citované osoby  → people, contact_points
 [accurate]    mapa rolí, väzby osoba–problém, priorita   → person_problem_links
 [accurate]    návrhy + kontrola faktov                   → outreach (draft)
 ── človek v GUI ──
 schváli → odošle ručne → zapíše odpoveď a rozhovor → interviews → claims (origin interview)
        → metriky Brány 0 → rozhodnutie (decisions)
```

Poradie fáz nočného behu MUSÍ minimalizovať výmeny modelov: (1) práca bez modelu, (2) všetko pre
`fast`, (3) `embed` (malý model, môže bežať súbežne na CPU), (4) všetko pre `accurate`, (5) správa a
`purge`. Jedna výmena veľkého modelu trvá desiatky sekúnd až minúty.

Fázy v0.1 sa rozdelia podľa rolí: napríklad `verify` beží deterministicky vo fáze (1) alebo (2) a
posúdenie modelom až vo fáze (4). Tvrdenia čakajúce na posúdenie ostávajú `pending`.

### 4.1 Nové moduly (návrh; MÔŽE sa upraviť, zmenu zapíš do DECISIONS.md)

| Modul | Obsah |
|---|---|
| `radar/migrations.py` | migrácie cez `PRAGMA user_version`, záloha pred migráciou |
| `radar/models.py` | roly, routing úloh, profily, `run_task()`, self-consistency, zápis do `model_judgments` |
| `radar/bench.py` | `doctor --bench`, odhad kapacity behu |
| `radar/evals.py` | eval harness, metriky, Wilsonov interval |
| `radar/registers.py` | klienti RPO a RÚZ (otvorené API), cache, tempo |
| `radar/orgs.py` | organizácie k hypotéze, overenie webu |
| `radar/contacts.py` | deterministická extrakcia kontaktov z HTML |
| `radar/people.py` | kontaktné stránky, osoby, väzby, priorita |
| `radar/outreach.py` | fakty, návrhy, kontroly, stavy, follow-up |
| `radar/crm.py` | rozhovory, metriky Brány 0, rozhodnutia |
| `radar/privacy.py` | potlačenie, `forget`, `purge`, maskovanie logov, pseudonymizácia, brány (LIA, placeholdery) |
| `radar/discover.py` | katalóg zdrojov, matica dopytov, regulačný radar, signály dopytu, bandit |
| `radar/novelty.py` | embeddingy, podobnosť, novosť |
| `radar/gui/` | Streamlit aplikácia (`app.py`, `pages/…`) |

---

## 5. Presnosť (R-ACC)

### 5.1 Roly modelov

| Rola | Odporúčaný model (rozhodne eval) | Na čo |
|---|---|---|
| `fast` | Qwen3.6-35B-A3B Q4_K_M (MoE, `--n-cpu-moe`), bez uvažovania | triedenie, extrakcia, kandidáti; dôraz na úplnosť |
| `accurate` | hustý model 24–32B v Q4_K_M; kandidáti Qwen3.6-27B a Gemma 4 31B | posudzovanie, zhlukovanie, kritika, väzby, návrhy; dôraz na presnosť |
| `embed` | bge-m3 alebo iný viacjazyčný embedding model v GGUF | podobnosť tvrdení a hypotéz |

- **R-ACC-1** Klient MUSÍ posielať v poli `model` názov modelu podľa roly (predvolene `fast`,
  `accurate`, `embed`). Výmenu modelov rieši server: llama-swap (príloha A) alebo router mód
  llama-servera. Ak v configu chýba sekcia `models`, systém sa MUSÍ správať ako v0.1 (všetky roly = `llm`).
- **R-ACC-2** Uvažovanie (thinking) sa zapína pre každú požiadavku zvlášť
  (`chat_template_kwargs.enable_thinking` pri šablónach Qwen; pri iných modeloch over). Server MUSÍ
  mať explicitný rozpočet `--reasoning-budget N` (napr. 0 pre `fast`, 1024–2048 pre `accurate`).
  NESMIE bežať s neobmedzeným uvažovaním: pri novších zostavách llama.cpp sú hlásené zaseknutia a
  zaplnenie pamäte (over na svojej verzii cez `llama-server --help`).
- **R-ACC-3** Orezaný výstup (`finish_reason == "length"`) alebo neplatný JSON po opravách je výsledok
  `undecided` a ide do kontroly. Nikdy sa nehádá.

### 5.2 Routing úloh a profily

Každá úloha má v configu rolu, uvažovanie a počet hlasov `k`. Fázy volajú `run_task(task, …)`,
nikdy konkrétny model.

| Úloha | Profil `balanced` (predvolený) | Poznámka |
|---|---|---|
| `screen` | fast | opisuje dokument problém niekoho? |
| `extract` | fast | tvrdenia + citáty |
| `number_extract` | fast | čísla pre odhad trhu |
| `entail` | accurate | vyplýva tvrdenie z citátu? |
| `entail_gate` | accurate, uvažovanie, k=3 | tvrdenia použité ako fakty v osloveniach |
| `cluster_assign` | accurate | zaradenie k hypotéze alebo nová |
| `critique` | accurate, uvažovanie | |
| `market_structure` | accurate, uvažovanie | |
| `deep_dive` | fast | agent s nástrojmi; volanie nástrojov pri hustom modeli over |
| `org_mentions` | fast | názvy organizácií v dôkazoch; kód overí |
| `person_mentions` | fast | citované osoby v dôkazoch; kód overí |
| `segment_filter` | accurate | segment → filter pre registre |
| `contact_assoc` | fast; accurate pri nejednoznačnosti | priradenie kontaktu k osobe; kód overí |
| `role_map` | accurate, uvažovanie, k=3 | ktoré roly vlastnia proces s problémom; raz na hypotézu |
| `role_fit` | accurate | funkcia osoby mimo mapy rolí; cache podľa normalizovaného názvu |
| `draft` | accurate | návrh oslovenia |
| `draft_check` | accurate, uvažovanie | tvrdí návrh niečo mimo faktov? |

Profily: `fast` (všetko fast), `balanced` (tabuľka), `accurate` (všetko accurate, napr. víkendový
beh) a eval profily A–D (5.6). Výber: `run-all --profile P`.

### 5.3 Kaskáda

1. Deterministické filtre (citát, čísla, dĺžky, regexy) idú prvé, sú zadarmo.
2. `fast` hľadá kandidátov; radšej viac.
3. `accurate` rozhoduje; radšej presne. Tvrdenie extrahované modelom `fast` MUSÍ posudzovať
   `accurate`, nie ten istý model (generátor nie je overovateľ).
4. Self-consistency iba pre rozhodnutia, ktoré spúšťajú akciu (`entail_gate`, `role_map`, povýšenie
   hypotézy z `candidate`): k=3 vzorky pri teplote 0,7, rozhoduje väčšina. Pomer 2:1 = prijaté
   s príznakom „nízka zhoda“; bez väčšiny alebo s `undecided` → kontrola.
5. Kontrola: človek rozhodne v GUI, rozhodnutie sa uloží ako gold label.

### 5.4 Provenance

- **R-ACC-4** Každé volanie modelu MUSÍ zapísať riadok do `model_judgments`: úloha, objekt, rola,
  názov modelu, profil, verzia promptu (napr. prvých 8 znakov SHA-256 šablóny), parametre, výstup,
  verdikt, tokeny, čas, `truncated`. Bez toho sa nedajú porovnať modely ani znovu spracovať položky
  po zmene promptu.

### 5.5 Plánovanie behu

- **R-ACC-5** `doctor --bench` zmeria pre každú rolu čas načítania, spracovanie promptu (tok/s),
  generovanie (tok/s), test JSON a test volania nástrojov. Ak server vracia `timings`, použi ich,
  inak čas a `usage`. Výsledok sa uloží do tabuľky `bench`.
- **R-ACC-6** Pred behom sa z benchmarku a priemerných tokenov úloh (z `model_judgments`) odhadne, čo
  sa stihne. Ak sa nestihne všetko, poradie priorít je:
  1. práca pre hypotézy s `people_enabled` (ľudia, väzby, návrhy, `entail_gate`);
  2. overenie dôkazov aktívnych hypotéz;
  3. extrakcia z objavovania;
  4. kritika a odhad trhu.

  Žiadna všeobecná fronta úloh: každá fáza si berie prácu z DB podľa stavu a priority, ako v0.1.

### 5.6 Eval harness

- **R-ACC-7** `python -m radar eval --task T --profile P` (alebo `--profile all`, voliteľne
  `--limit N`) spustí úlohu nad položkami s gold labelom a porovná výsledky.
- Profily:
  - **A** fast bez uvažovania;
  - **B** fast s uvažovaním;
  - **C** accurate bez uvažovania;
  - **D** accurate s uvažovaním.
- Úlohy: `entail`, `extract` (je tvrdenie správne vytiahnuté?), `cluster_assign`, `role_fit`,
  `contact_assoc`, `draft_check`.
- Metriky:
  - presnosť, úplnosť a F1 (pri binárnych úlohách), inak podiel správnych;
  - 95 % Wilsonov interval, pretože pri malých vzorkách je rozdiel profilov často v rámci šumu;
  - priemerný čas a tokeny na položku.
- Výstup: `reports/eval_<dátum>.md` a riadky v DB. Odporúčaný routing = najlacnejší profil, ktorého
  interval sa prekrýva s najlepším.
- Labely pochádzajú z troch zdrojov:
  1. z kontroly;
  2. z náhodnej stratifikovanej vzorky z `model_judgments` (kladné aj záporné verdikty);
  3. zo syntetických prípadov s nastrčenou chybou (vymyslený citát, zmenené číslo, cudzí kontakt).

  Pred záverom treba aspoň 50 labelov na úlohu, inak report píše „nedostatok dát“.

---

## 6. Organizácie (R-ORG)

Beží iba pre hypotézy s `people_enabled = 1`: automaticky top 3 podľa sily dôkazov (aspoň stredná,
stav iný ako `rejected`) alebo ručne v GUI či cez `select`. Hypotézy pôvodných 4 tém sú oprávnené rovnako.

- **R-ORG-1** Zdroje organizácií v poradí priority:
  1. **Menované v overených dôkazoch** hypotézy (`org_mentions`). Kód MUSÍ overiť, že názov je doslova
     v citáte alebo v okne okolo neho.
  2. **Obstarávanie a signály dopytu**: obstarávatelia s podobnými zákazkami alebo PTK (zdroje CRZ a vestník ÚVO).
  3. **Segment**: `segment_filter` prevedie segment na filter `{sk_nace, size, sector_kind, region}`,
     človek ho môže v GUI upraviť. Kandidátov hľadaj takto: vyhľadávanie → web firmy → IČO zo stránky
     (regex typu `IČO[:\s]*\d{2}\s?\d{3}\s?\d{3}`) → detail v RPO → filter podľa hlavnej činnosti a
     právnej formy.
  4. **Verejné zoznamy** (obce, nemocnice, školy…) importované z CSV.
- **R-ORG-2** **RPO** (Register právnických osôb, ŠÚ SR) má otvorené API s licenciou CC BY 4.0, preto
  pri zobrazení a exporte uvádzaj zdroj.
  - `GET https://api.statistics.sk/rpo/v1/search?identifier=<IČO>` (hľadanie podľa názvu: over parametre).
  - `GET https://api.statistics.sk/rpo/v1/entity/<id>` vracia polia `fullNames`, `addresses`,
    `legalForms`, `activities`, `statutoryBodies` (`personName`, `stakeholderType`, `address`) a
    `statisticalCodes.mainActivity` (kód hlavnej činnosti).
  - Zo `statutoryBodies` sa smie uložiť iba meno a funkcia. Adresu fyzickej osoby NESMIEŠ uložiť ani zalogovať.
- **R-ORG-3** **RÚZ** (Register účtovných závierok) má Open API s licenciou CC0, základ
  `https://www.registeruz.sk/cruz-public`.
  - `/api/uctovne-jednotky?zmenene-od=…&ico=…` vráti ID.
  - `/api/uctovna-jednotka?id=…` vráti `skNace`, `velkostOrganizacie`, `druhVlastnictva`, `kraj`,
    `okres` a `idVyrocnychSprav`.
  - `/api/vyrocna-sprava?id=…` vráti výročnú správu. Jej časť o rizikách je zdroj dôkazov, že
    konkrétna organizácia problém má (URL príloh over v dokumentácii API).
  - Zoznamové endpointy nefiltrujú podľa SK NACE. Celý register NESŤAHUJ cez detailové volania.
    Filter podľa odvetvia rieš postupom z R-ORG-1 bod 3, hromadnými otvorenými dátami (over, či ich
    ŠÚ SR alebo data.slovensko.sk ponúka) alebo datahubom Slovensko.Digital (platený, pre študentov po dohode).
- **R-ORG-4** Overenie webu: oficiálnu doménu nájdi vyhľadávaním (`"<názov>" <IČO>`,
  `"<názov>" kontakt`). Stránka MUSÍ obsahovať IČO alebo presný názov (kontroluje sa deterministicky).
  Inak `website_verified = 0` a ľudia sa z webu nezbierajú.
- **R-ORG-5** Limit 30 organizácií na hypotézu (config). Poradie: menované v dôkazoch, potom
  obstarávanie, potom segment (veľkosť, región). Zhoda so segmentom sa zobrazuje po zložkách
  (odvetvie, veľkosť, región, dôkaz o probléme), nie jedným číslom.
- **R-ORG-6** Odpovede registrov sa cachujú (napr. 30 dní). Tempo je zdvorilé: aspoň 1 s medzi
  volaniami (konfigurovateľné).
- **R-ORG-7** Organizácie v `outreach.exclude_orgs` (IČO alebo domény) sa nikdy neoslovujú,
  napríklad kvôli konfliktu záujmov. Rešpektuje ich `orgs` aj `people`.
- **R-ORG-8** ORSR a komerčné agregátory bez API sa nesťahujú.

---

## 7. Ľudia a kontakty (R-PPL)

### 7.1 Povolené a zakázané zdroje

| Povolené | Zakázané |
|---|---|
| web organizácie: kontakty, vedenie, organizačná štruktúra, tlačové správy | sťahovanie linkedin.com, facebook.com, instagram.com, x.com, twitter.com, tiktok.com |
| úradné zverejnenia: kontaktná osoba v oznámení o obstarávaní alebo PTK, profil obstarávateľa | platené databázy kontaktov, „data brokers“, uniknuté databázy |
| RPO: meno a funkcia štatutára, bez adresy | hádanie e-mailov podľa vzoru, SMTP overovanie |
| overené dôkazy: osoba citovaná k problému v článku alebo správe | dekódovanie obfuskovaných adries |
| programy konferencií a podujatí: rečník a téma vystúpenia | súkromné kontakty z neoficiálnych zdrojov; adresa bydliska, fotografie, dátum narodenia, rodinné údaje |
| profesijný web osoby, kde sama zverejnila kontakt | obchádzanie robots.txt, prihlásenia, CAPTCHA alebo limitov |
| výsledky vyhľadávania: URL a titulok profilu, bez stiahnutia stránky | |

### 7.2 Kontaktné stránky

- **R-PPL-1** Pre organizáciu s overeným webom nájdi najviac 5 stránok: odkazy z úvodnej stránky a
  zo `sitemap.xml` s kľúčovými slovami kontakt, kontakty, o-nas, vedenie, manazment, management, team,
  tim, organizacna-struktura, urad, zamestnanci, impressum. robots.txt a pauzy ako vo fetcheri.
- **R-PPL-2** Surové HTML kontaktných stránok sa spracuje v pamäti a NEUKLADÁ sa celé, pretože
  obsahuje celý zoznam zamestnancov. Ukladajú sa iba metadáta stránky (`contact_pages`: URL, čas,
  hash, stav) a úryvky (≤ 300 znakov) pre vybrané osoby a generické kontakty.
- **R-PPL-3** Fetcher MUSÍ odmietnuť blokované domény (7.1) aj po presmerovaní a MUSÍ mať metódu
  `fetch_html()` pre kontaktné stránky.

### 7.3 Deterministická extrakcia kontaktov

- **R-PPL-4** E-mail sa berie z odkazov `mailto:` a regexom z viditeľného textu, normalizuje sa
  (malé písmená, bez medzier) a kontroluje sa syntax. Obfuskované formy („meno(at)firma.sk“, „[at]“,
  „zavináč“, Cloudflare `/cdn-cgi/l/email-protection`) sa NEDEKÓDUJÚ: uloží sa `kind = hidden_contact`
  s URL stránky a poznámkou „otvor ručne“.
- **R-PPL-5** Telefón sa berie z odkazov `tel:` a cez `phonenumbers.PhoneNumberMatcher(text, "SK")`,
  ukladá sa vo formáte E.164.
- **R-PPL-6** Každá uložená hodnota MUSÍ byť (po normalizácii) doslova v zdrojovom úryvku. E-mail,
  ktorý na stránke nie je, sa NESMIE uložiť, ani keby „sedel“ vzor ostatných adries.
- **R-PPL-7** Generické kontakty (info@, kontakt@, podatelna@, sekretariat@, ústredňa) sa ukladajú
  k organizácii (`is_generic = 1`), nie k osobe.
- **R-PPL-8** Voliteľná kontrola MX záznamu domény cez DNS je predvolene vypnutá. SMTP nikdy.

### 7.4 Osoby a priradenie kontaktov

- **R-PPL-9** Kandidáti na osoby pochádzajú z HTML blokov (`tr`, `li`, `article`, `p`, `div`
  s triedou typu card/person/contact), ktoré obsahujú meno, funkciu alebo kontakt. `contact_assoc`
  navrhne trojice `{meno, funkcia, e-mail, telefón, úryvok}`.
- **R-PPL-10** Kód MUSÍ overiť, že meno, funkcia aj kontakt sú v tom istom HTML bloku alebo v okne
  ≤ 400 znakov. Ak nie sú, kontakt ostane generický pri organizácii. Ak je v bloku viac mien,
  rozhoduje `accurate`, pri neistote kontrola.
- **R-PPL-11** Meno sa ukladá presne tak, ako je zverejnené, tituly zvlášť (`titles`). Rod
  (`gender_hint`) iba z výslovného textu zdroja: „pán“, „pani“ alebo ženská podoba funkcie
  (riaditeľka, starostka). Koncovka -ová sama nestačí.
- **R-PPL-12** Citované osoby (`person_mentions`): v overených dôkazoch hypotézy hľadaj osoby
  citované k problému (meno, funkcia, organizácia). Kód overí, že meno je v citáte alebo v okne.
  Kontakt potom hľadaj na webe ich organizácie (zhoda mena bez diakritiky a titulov).
- **R-PPL-13** URL profilu (napr. LinkedIn) z výsledku vyhľadávania sa uloží iba vtedy, keď titulok
  obsahuje celé meno osoby a titulok alebo úryvok obsahuje názov organizácie. Označí sa „over ručne“
  a stránka sa nesťahuje.

### 7.5 Väzba osoba–problém

| Typ | Význam | Povinný dôkaz | Sila |
|---|---|---|---|
| `quoted_about_problem` | osoba verejne hovorila alebo písala o probléme | `claim_id`; meno je v citáte alebo okne | 1,0 |
| `role_owner` | funkcia zodpovedá za proces s problémom | mapa rolí hypotézy + `role_fit` | 0,7 |
| `org_affected` | organizácia má problém podľa dôkazu, osoba je v jej vedení | `org_hypotheses.evidence_claim_id` | 0,5 |
| `procurement_contact` | kontaktná osoba v oznámení o obstarávaní alebo PTK | URL oznámenia | 0,4 |
| `expert` | výskumník, konzultant, zástupca zväzu; rozumie problému, nie je zákazník | zdroj odbornosti | 0,3 |

- **R-PPL-14** `role_map` raz na hypotézu navrhne kategórie rolí s odôvodnením. Príklad pre meškanie
  dodávok v zákazkovej výrobe: vedúci nákupu, plánovač výroby, projektový manažér, vedúci výroby,
  konateľ malej firmy. Človek ich môže v GUI upraviť.
- **R-PPL-15** `role_fit` osoby sa počíta zhodou funkcie s mapou (rapidfuzz): presná 1,0, príbuzná
  0,5, všeobecné vedenie 0,3. Funkcie mimo mapy posúdi úloha `role_fit` s cache podľa normalizovaného
  názvu funkcie.
- **R-PPL-16** Experti sa vedú zvlášť, najviac 3 na hypotézu, a nepočítajú sa do zákazníckych metrík
  Brány 0.

### 7.6 Priorita a limity

- **R-PPL-17** Priorita je kľúč na zoradenie, nie hodnotenie človeka:
  `0,35·role_fit + 0,25·sila_väzby + 0,20·kanál + 0,20·zhoda_org`. Váhy sú v configu, GUI MUSÍ
  ukazovať zložky. Hodnoty kanála:
  - osobný pracovný e-mail 1,0;
  - osobný pracovný telefón 0,7;
  - `hidden_contact` 0,6;
  - generický e-mail 0,4;
  - iba URL profilu 0,3.
- **R-PPL-18** Najviac 15 aktívnych kandidátov na hypotézu a 2 na organizáciu. Prednosť majú rôzne organizácie.
- **R-PPL-19** Pred uložením osoby alebo kontaktu prebehne kontrola potlačenia (hash e-mailu,
  telefónu a mena s organizáciou).
- **R-PPL-20** `people` NESMIE bežať, kým nie je `privacy.lia_completed: true` (pozri 12.2).

---

## 8. Návrhy oslovení (R-OUT)

### 8.1 Vstupy a výstupy

Vstupy: osoba (meno, funkcia, organizácia), väzba a jej dôkaz, hypotéza (problém slovami zákazníka),
profil odosielateľa (config), jazyk (sk).

- **R-OUT-1** Generátor dostane očíslovaný zoznam povolených faktov `F1…Fn` (overené dôkazy s URL,
  údaje o osobe a organizácii zo zdroja). Fakty z webu idú do promptu v `<untrusted_document>`.
  Výstup obsahuje `facts_used`. Iné fakty o príjemcovi návrh NESMIE tvrdiť.
- **R-OUT-2** Kanály a dĺžky (config):
  - e-mail: predmet ≤ 70 znakov, telo ≤ 150 slov bez pätičky;
  - poznámka k pozvánke na LinkedIne: ≤ 200 znakov vrátane krátkeho odkazu na informáciu
    o spracúvaní. Toľko má bezplatný účet, Premium 300; bezplatné účty majú aj mesačný limit
    personalizovaných poznámok (približne 5, over aktuálne podmienky);
  - telefón: úvod ≤ 80 slov, jedna otázka a odpoveď na „pošlite mi e-mail“.
- **R-OUT-3** Dve varianty:
  - **A** odkazuje na verejný výrok alebo dôkaz (iba pri `quoted_about_problem` a `org_affected`);
  - **B** vychádza z funkcie a odvetvia.

### 8.2 Pravidlá obsahu (Mom Test)

- Pýtať sa na skúsenosť a minulé správanie („kedy sa to stalo naposledy, ako ste to riešili“), nie na
  názor na nápad.
- Nepredávať: žiadna ponuka, produkt, demo, cena ani zľava. Zakázané frázy sú v configu.
- Konkrétny dôvod „prečo práve vy“ smie pochádzať iba z povolených faktov.
- Jasná prosba: 20–30 minút telefonicky alebo online.
- Ľahké odmietnutie: „Ak to pre vás nie je téma, stačí odpísať ‚nie‘ a viac sa neozvem.“
- Pravdivý profil: kto píše a že ide o osobný výskum problému. Univerzitu uvádzať iba vtedy, ak je to
  pravda, a nevydávať to za výskum univerzity. Ak sa pýtajú na zámer, nezatajovať možný budúci produkt.
- Žiadne lichôtky, vymyslení spoloční známi, falošná naliehavosť ani zmienka o zamestnávateľovi.

### 8.3 Pätička a oslovenie

- **R-OUT-4** Povinná pätička (pri e-maile celá, pri LinkedIne aspoň odkaz) uvádza:
  - identitu a kontakt odosielateľa;
  - kde sa kontakt našiel (URL);
  - účel (dohodnutie rozhovoru);
  - právny základ (oprávnený záujem);
  - dobu uchovania;
  - právo namietať a žiadať výmaz;
  - odkaz na plnú informáciu podľa čl. 14 GDPR.
- **R-OUT-5** Oslovenie vychádza iba z overených údajov:
  - „Dobrý deň, pán Novák,“ alebo „Dobrý deň, pani Nováková,“ iba pri výslovnom `gender_hint`;
  - funkčné oslovenie verejných funkcionárov („Vážený pán starosta,“ / „Vážená pani starostka,“)
    pri overenej funkcii;
  - inak „Dobrý deň,“.

  Akademické tituly sa v oslovení nevyžadujú; ak sa použijú, tak presne podľa zdroja.

### 8.4 Kontroly návrhu

- **R-OUT-6** Deterministické kontroly:
  - dĺžky;
  - zakázané frázy;
  - prítomnosť pätičky a odkazu;
  - žiadna URL okrem povolených (zdroj a informácia o spracúvaní);
  - každé číslo v návrhu musí byť vo faktoch (`numbers_missing_in_source`);
  - v schválenom návrhu žiadny placeholder `{…}`.
- **R-OUT-7** `draft_check` posúdi, či návrh netvrdí niečo, čo nie je vo faktoch. Výstupom je verdikt
  a zoznam problémových viet. Ak návrh neprejde, dostane stav `needs_edit` a GUI vety zvýrazní.

### 8.5 Stavy, limity, odoslanie

- **R-OUT-8** Stavy:
  `draft → needs_edit | approved → sent → replied | no_reply | bounced | declined | objected → scheduled → interviewed | no_show`.
  Stav `sent` nastavuje človek po ručnom odoslaní.
- **R-OUT-9** Jeden follow-up po 5–7 pracovných dňoch od `sent` (víkendy sa nepočítajú, sviatky
  voliteľne v configu). Potom `no_reply` a koniec.
- **R-OUT-10** Denný limit schválení na odoslanie je 10 (config). GUI ukazuje počítadlo.
- **R-OUT-11** Stav `objected` (námietka alebo žiadosť o výmaz) okamžite spustí `forget` a
  potlačenie. Pri `declined` (zdvorilé „nie“) ostane minimálny záznam a osobné údaje sa vymažú po 30 dňoch.
- **R-OUT-12** Odoslanie: tlačidlá „Kopírovať“ a „Otvoriť v pošte“ (`mailto:` s predmetom a telom,
  URL-encoded; ak je odkaz dlhší ako približne 1 800 znakov, iba kopírovanie). Žiadny SMTP.
- **R-OUT-13** Ak organizácia práve obstaráva vec súvisiacu s hypotézou (z dôkazov alebo oznámení),
  návrh sa nevytvorí a GUI upozorní, že komunikovať sa smie iba oficiálnou cestou (PTK, vysvetľovanie
  súťažných podkladov). Neformálny kontakt počas súťaže môže viesť ku konfliktu záujmov alebo k vylúčeniu.
- **R-OUT-14** Pre telefón vzniká iba skript na ručné volanie; volať iba na pracovné čísla v pracovnom čase.
- **R-OUT-15** Schválenie je zablokované, kým profil odosielateľa obsahuje placeholdery.

---

## 9. Brána 0: rozhovory, metriky, rozhodnutia (R-CRM)

- **R-CRM-1** Formulár rozhovoru v GUI obsahuje:
  - dátum, osobu alebo pseudonym, typ (`customer` alebo `expert`), hypotézu;
  - kedy sa problém stal naposledy a ako ho riešia dnes;
  - čo ich to stojí (čas, peniaze, chyby);
  - kto rozhoduje a kto by platil, čo už skúšali;
  - či problém spomenul sám, bez navádzania (áno / nie);
  - ďalší krok alebo záväzok: predstavenie kolegu, ďalšie stretnutie, dáta, pilot, nič;
  - poznámky a doslovné citáty.
- **R-CRM-2** Citáty z rozhovoru sa ukladajú ako `claims` s `origin = interview` na pseudo-dokumente
  (`documents.source_kind = interview`, URL `interview://<id>`, text = poznámky). Citát MUSÍ prejsť
  rovnakou deterministickou kontrolou voči poznámkam.
- **R-CRM-3** V správach a exportoch sú respondenti pseudonymizovaní (R1, R2… + funkcia a typ organizácie).
- **R-CRM-4** Prehľad Brány 0 pre hypotézu:
  - rozhovory so zákazníkmi: počet / cieľ 8–12;
  - „spomenul sám“: zlomok, prah ≥ 3 z 10;
  - platiteľ: človek potvrdí, kto platí, s odkazmi na rozhovory;
  - záväzky: počet a typ;
  - lievik: kandidáti → schválení → odoslaní → odpovede → dohodnuté → rozhovory, a miera odpovedí;
  - návrh verdiktu, deterministicky:
    - `nedostatok dát` pri menej ako 8 rozhovoroch;
    - `fail`, ak je „spomenul sám“ pod prahom;
    - `chýba platiteľ`, ak je prah splnený, ale platiteľ nie je potvrdený;
    - inak `pass`.
- **R-CRM-5** Rozhodnutie robí človek a zapíše ho do `decisions` (pokračovať / pivot / ukončiť /
  pozastaviť) s odôvodnením a snímkou metrík. Hypotéza dostane `gate0_status`.
- **R-CRM-6** Experti sa zobrazujú zvlášť a do metrík sa nepočítajú.

---

## 10. Objavovanie nových problémov (R-DISC)

- **R-DISC-1** Katalóg zdrojov `sources.yaml` (príloha B) má pre každý zdroj kategóriu, názov, URL,
  metódu (`rss`, `sitemap`, `search`, `api`, `seed`), voliteľné dopyty (`queries`), frekvenciu v dňoch,
  jazyk a príznak `verified`.
  Kategórie:
  - kontrolné správy (NKÚ), revízie výdavkov (ÚHP), fiškálne analýzy (RRZ), ombudsman;
  - legislatíva (slov-lex vrátane pripomienok v medzirezortnom pripomienkovom konaní) a nové
    povinnosti EÚ (EUR-Lex);
  - obstarávanie (vestník ÚVO, PTK, CRZ);
  - zväzy a komory (prieskumy podnikateľského prostredia, bariéry);
  - výročné správy z RÚZ (časť o rizikách);
  - odborné médiá, verejné výzvy na inovácie, programy podujatí.

  `python -m radar sources --check` overí dostupnosť a robots.txt a vypíše výsledok. Zdroj sa použije
  až vtedy, keď ho používateľ potvrdí (`verified: true`); inak sa preskakuje.
- **R-DISC-2** Matica dopytov: vzory bolesti × odvetvia (sekcie a vybrané divízie SK NACE ľudskými
  názvami), voliteľne × región. Je deterministická a rotuje: každý beh berie ďalšiu časť matice, stav
  je v `query_stats`.
- **R-DISC-3** Regulačný radar: z legislatívnych zdrojov vznikajú tvrdenia typu `regulation` s poľami
  kto, čo, odkedy a sankcia. Povinnosti s účinnosťou o 3–24 mesiacov sú prioritné signály.
- **R-DISC-4** Signály dopytu: oznámenia PTK, opakované zákazky na to isté, zrušené súťaže bez ponúk
  a výzvy na inovácie dávajú nový typ tvrdenia `demand_signal`. Niekto aktívne hľadá riešenie, čo je
  silnejší signál ako opis bolesti.
- **R-DISC-5** Na objavovanie ide podiel `exploration_share` (predvolene 0,3) z rozpočtu dokumentov
  na beh. Medzi kategórie zdrojov sa delí Thompson samplingom s Beta(1 + úspechy, 1 + neúspechy);
  úspech = dokument dal aspoň 1 overené tvrdenie. Každá kategória má minimálny podiel 5 %. V testoch
  je pevný seed.
- **R-DISC-6** Pri dokumentoch z objavovania beží najprv `screen` na prvej časti textu; plná
  extrakcia iba pri kladnom výsledku. Extrakcia beží bez témy (`topic = NULL`), povinné sú
  `affected_party` a `sector_hint`.
- **R-DISC-7** Novosť a zhlukovanie:
  - embeddingy tvrdení a hypotéz (`embed`);
  - kandidátske hypotézy = top-k podľa kosínusovej podobnosti (nahrádza rapidfuzz v `cluster.py`),
    rozhoduje `cluster_assign`;
  - ak je najvyššia podobnosť pod `novelty_threshold` (kalibruj na označených dátach, začni 0,80),
    vznikne hypotéza so stavom `candidate`;
  - na `new` sa povýši, až keď má aspoň 2 overené tvrdenia z aspoň 2 domén, aby jeden článok
    nevyrobil hypotézu;
  - `cluster_assign` MUSÍ zvažovať aj hypotézy v stave `candidate`, inak by sa nikdy nepovýšili.
    Kritika, odhad trhu a hľadanie ľudí ich preskakujú.
- **R-DISC-8** Pôvod hypotézy sa ukladá: `hypotheses.origin` (`topic`, `discovery`, `manual`,
  `interview`), `topic` (môže byť NULL), `sector`, `first_seen_at`. GUI podľa neho filtruje.
- **R-DISC-9** Nové problémy sa zoraďujú bez váženého desatinného skóre, lexikograficky:
  1. sila dôkazov;
  2. má `demand_signal` alebo `regulation` s termínom;
  3. počet nezávislých domén;
  4. čerstvosť.

  „Zhoda so zakladateľom“ je ručný štítok používateľa v GUI, nie výpočet.

---

## 11. GUI (R-GUI)

- **R-GUI-1** Streamlit (≥ 1.40). `python -m radar gui` spustí cez `sys.executable -m streamlit`:
  `run radar/gui/app.py --server.address 127.0.0.1 --server.port 8501 --server.headless true --browser.gatherUsageStats false`.
  Rovnaké nastavenia sú aj v `.streamlit/config.toml`. Bez `server.address` Streamlit počúva na
  všetkých sieťových rozhraniach.
- **R-GUI-2** Stránky:

| Stránka | Obsah a akcie |
|---|---|
| Prehľad | počty, posledné behy, čakajúce kontroly, prehľad Brány 0 |
| Problémy | hypotézy s filtrom pôvod, stav, sila; detail s dôkazmi (citát zvýraznený v okne zdroja), kritikou, trhom a poznámkami; prepínač `people_enabled`; štítok „zhoda so zakladateľom“ |
| Dôkazy | tvrdenia s filtrom stav, typ, doména, pôvod; odkaz na zdroj |
| Organizácie | tabuľka, zdroj zaradenia, overený web, zložky zhody; vylúčenie |
| Ľudia a kontakty | osoba, funkcia, organizácia, kontakty s úryvkom a URL, väzba s dôkazom, priorita po zložkách, stav; akcie schváliť na oslovenie, vylúčiť, `forget` |
| Oslovenia | návrhy A/B podľa kanála, výsledky kontrol, úprava, „Kopírovať“, „Otvoriť v pošte“, označiť odoslané, termíny follow-upu, denné počítadlo |
| Rozhovory | formulár R-CRM-1, zoznam, citáty |
| Brána 0 | metriky R-CRM-4, rozhodnutia |
| Kontrola | fronta položiek; tlačidlá správne / nesprávne / neviem + poznámka → gold label |
| Eval | výsledky profilov, intervaly, čas na položku |
| Súkromie | potlačenie, `forget`, `purge`, retencia, stav LIA, export |

- **R-GUI-3** Úpravy cez `st.data_editor` sú povolené iba v určených stĺpcoch (stav, poznámka,
  `people_enabled`, schválenie). Zápis ide iba cez metódy DB vrstvy, mazanie s potvrdením.
- **R-GUI-4** Text zo zdrojov je nedôveryhodný: pred zobrazením sa vždy escapuje (`html.escape`),
  zvýraznenie citátu sa vkladá až potom. Ako odkazy sa zobrazujú iba URL s `http` alebo `https`.
- **R-GUI-5** GUI a nočný beh môžu bežať naraz: WAL (už je), `busy_timeout` 5000 ms, krátke
  transakcie a `st.cache_data` s TTL a invalidáciou po zápise. Veľké tabuľky sa stránkujú.
- **R-GUI-6** Export CSV alebo XLSX ide do `exports/` (gitignored) a je predvolene pseudonymizovaný.
  Osobné údaje iba po výslovnom zaškrtnutí s varovaním. Pri dátach z RPO sa uvádza zdroj (CC BY 4.0).
- **R-GUI-7** MÔŽE: graf vzťahov hypotéza–organizácia–osoba; Datasette ako prehliadač iba na čítanie
  na 127.0.0.1.

---

## 12. Súkromie, právo, bezpečnosť (R-PRIV)

Toto nie je právna rada. Pred väčším objemom oslovení sa poraď s právnikom.

### 12.1 Právny rámec (technický podklad)

- GDPR platí, pretože príprava podnikania nie je čisto osobná činnosť (čl. 2 ods. 2 písm. c).
- Právny základ je oprávnený záujem (čl. 6 ods. 1 písm. f) so zdokumentovaným testom
  (LIA, `docs/LIA_template.md`).
- Údaje nezískané od osoby: informácia najneskôr pri prvej komunikácii (čl. 14 ods. 3 písm. b),
  preto pätička a odkaz v každom prvom oslovení.
- Práva osôb:
  - námietka (čl. 21) → okamžite `forget`;
  - výmaz (čl. 17) → `forget`;
  - prístup (čl. 15) → `export --person`.
- Nevyžiadaná komunikácia: zákon č. 452/2021 Z. z., § 116. Priamy marketing e-mailom vyžaduje
  súhlas; výnimka v § 116 ods. 15 sa podľa dostupného výkladu týka zverejnených kontaktov podnikateľov
  a právnických osôb. Systém marketing neposiela: prvá správa je prosba o rozhovor bez ponuky. Každá
  správa obsahuje identitu odosielateľa, kontakt a možnosť odmietnuť.
- LinkedIn: používateľská zmluva zakazuje automatizovaný zber dát (scraping), preto iba ručné použitie.
- Obstarávanie: R-OUT-13.
- Licencie: RPO CC BY 4.0 (uvádzať zdroj), RÚZ CC0.

### 12.2 Technické opatrenia

- **R-PRIV-1** Brána LIA: `people` a `drafts` nebežia, kým nie je `privacy.lia_completed: true`.
- **R-PRIV-2** Minimalizácia: ukladá sa iba meno, tituly, funkcia, organizácia, pracovné kontakty,
  zdrojová URL, úryvok ≤ 300 znakov, väzba, stav a časy. Nič iné.
- **R-PRIV-3** Obmedzenie účelu: osoby sa zbierajú iba pre hypotézy s `people_enabled`;
  `collected_for_hypothesis_id` je povinné.
- **R-PRIV-4** Retencia (config):
  - neoslovení kandidáti 90 dní;
  - po poslednej interakcii 12 mesiacov;
  - `declined` 30 dní;
  - exporty 30 dní.

  Vykonáva ju `python -m radar purge`, ktorý beží aj na konci `run-all`.
- **R-PRIV-5** `forget --person ID`, `--email X` alebo `--phone X` zmaže osobu, kontakty, oslovenia a
  osobné polia rozhovorov (pseudonymizované citáty ostanú) a pridá hashe do potlačenia.
- **R-PRIV-6** Potlačenie používa `sha256(soľ + normalizovaný identifikátor)`. Soľ je v `data/.salt`
  (vytvorí sa pri prvom spustení, gitignored). Kontroluje sa pred uložením aj pred návrhom. Po obnove
  zo zálohy treba spustiť `python -m radar privacy reapply`.
- **R-PRIV-7** Logy maskujú e-maily a telefóny cez `logging.Filter`, aj v `logs/radar.log`.
- **R-PRIV-8** Správy a exporty predvolene pseudonymizujú osoby.
- **R-PRIV-9** Prevádzka:
  - služby iba na 127.0.0.1;
  - projekt mimo priečinkov synchronizovaných cez OneDrive (napr. `C:\dev\problem_radar`);
  - šifrovanie disku (BitLocker alebo Šifrovanie zariadenia);
  - zálohy iba šifrované.
- **R-PRIV-10** Konflikt záujmov rieši `outreach.exclude_orgs`. Pri témach blízkych odvetviu, kde
  používateľ pracuje alebo stážuje, si má overiť vlastné povinnosti (mlčanlivosť, konkurencia).
  Systém o zamestnávateľovi nič nevie a nemá vedieť.

---

## 13. Dátový model (migrácia 2)

Mechanizmus: `migrations.py` so zoznamom funkcií. Pri štarte platí, že ak `user_version = 0` a
tabuľky v0.1 existujú, ide o verziu 1. Pred migráciou sa vytvorí kópia
`data/backup/radar-v<n>-<čas>.sqlite`, po migrácii je `user_version = 2`. Cudzie kľúče s
`ON DELETE CASCADE` fungujú, lebo DB zapína `PRAGMA foreign_keys = ON`.

```sql
-- zmeny existujúcich tabuliek
ALTER TABLE documents  ADD COLUMN source_category TEXT;  -- audit|spending_review|fiscal|ombudsman|legislation|eu_obligations|procurement|association|annual_report|media|innovation_call|event|other
-- documents.source_kind pribudne: discovery | register | interview   (EXTRACTABLE_KINDS += discovery)
ALTER TABLE claims     ADD COLUMN sector_hint TEXT;
ALTER TABLE claims     ADD COLUMN effective_date TEXT;   -- pri claim_type = regulation
-- claims.claim_type pribudne: demand_signal;  claims.origin pribudne: interview
ALTER TABLE hypotheses ADD COLUMN origin TEXT NOT NULL DEFAULT 'topic';      -- topic|discovery|manual|interview
ALTER TABLE hypotheses ADD COLUMN topic TEXT;
ALTER TABLE hypotheses ADD COLUMN sector TEXT;
ALTER TABLE hypotheses ADD COLUMN first_seen_at TEXT;
ALTER TABLE hypotheses ADD COLUMN people_enabled INTEGER NOT NULL DEFAULT 0;
ALTER TABLE hypotheses ADD COLUMN founder_fit TEXT;                          -- high|medium|low|unknown (ručne)
ALTER TABLE hypotheses ADD COLUMN gate0_status TEXT NOT NULL DEFAULT 'not_started';  -- not_started|in_progress|passed|failed|paused
ALTER TABLE hypotheses ADD COLUMN payer_confirmed TEXT;                      -- kto platí (ručne), NULL = nepotvrdené
-- hypotheses.status pribudne: candidate (HYPOTHESIS_STATUSES; ACTIVE_STATUSES ho neobsahuje)

CREATE TABLE organizations (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  ico TEXT UNIQUE,
  legal_form TEXT, sk_nace TEXT, size_category TEXT,
  sector_kind TEXT,                         -- public|private|nonprofit
  region TEXT, municipality TEXT,
  website TEXT, website_verified INTEGER NOT NULL DEFAULT 0, website_evidence TEXT,
  source TEXT NOT NULL,                     -- evidence|procurement|rpo|ruz|search|public_list|manual
  source_url TEXT,
  excluded INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE org_hypotheses (
  org_id INTEGER NOT NULL REFERENCES organizations(id),
  hypothesis_id INTEGER NOT NULL REFERENCES hypotheses(id),
  relation TEXT NOT NULL,                   -- mentioned_with_problem|procured_related|segment_match|manual
  evidence_claim_id INTEGER REFERENCES claims(id),
  fit_json TEXT,                            -- zložky zhody so segmentom
  status TEXT NOT NULL DEFAULT 'candidate', -- candidate|website_checked|people_done|excluded
  PRIMARY KEY (org_id, hypothesis_id)
);

CREATE TABLE contact_pages (                -- iba metadáta, obsah stránky sa neukladá (R-PPL-2)
  id INTEGER PRIMARY KEY,
  org_id INTEGER NOT NULL REFERENCES organizations(id),
  url TEXT NOT NULL, fetched_at TEXT NOT NULL, content_hash TEXT,
  status TEXT NOT NULL,                     -- ok|robots_blocked|blocked_domain|error
  UNIQUE (url, content_hash)
);

CREATE TABLE people (
  id INTEGER PRIMARY KEY,
  org_id INTEGER REFERENCES organizations(id),
  full_name TEXT NOT NULL, titles TEXT, role_title TEXT, role_category TEXT, gender_hint TEXT,
  name_key TEXT NOT NULL,                   -- meno bez titulov a diakritiky, malými písmenami (deduplikácia)
  source_url TEXT NOT NULL, source_snippet TEXT NOT NULL,
  lawful_basis TEXT NOT NULL DEFAULT 'legitimate_interest',
  collected_for_hypothesis_id INTEGER NOT NULL REFERENCES hypotheses(id),
  is_expert INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'candidate', -- candidate|approved|contacted|replied|interviewed|declined|excluded
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  last_interaction_at TEXT, purge_after TEXT,
  UNIQUE (org_id, name_key)
);

CREATE TABLE contact_points (
  id INTEGER PRIMARY KEY,
  person_id INTEGER REFERENCES people(id) ON DELETE CASCADE,
  org_id INTEGER REFERENCES organizations(id),
  kind TEXT NOT NULL,                       -- email|phone|profile_url|contact_form|hidden_contact
  value TEXT NOT NULL,                      -- e-mail malými písmenami, telefón E.164, URL
  is_generic INTEGER NOT NULL DEFAULT 0,
  source_url TEXT NOT NULL, source_snippet TEXT NOT NULL,
  extraction TEXT NOT NULL,                 -- mailto|tel|regex|phonenumbers|search_result|manual
  checks_json TEXT,
  created_at TEXT NOT NULL
);
-- UNIQUE s NULL v SQLite neplatí (NULL sa považuje za odlišné), preto index s COALESCE:
CREATE UNIQUE INDEX idx_contact_unique
  ON contact_points(COALESCE(person_id, 0), COALESCE(org_id, 0), kind, value);

CREATE TABLE role_maps (
  hypothesis_id INTEGER PRIMARY KEY REFERENCES hypotheses(id),
  roles_json TEXT NOT NULL,                 -- [{category, examples, rationale}]
  edited_by_human INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);

CREATE TABLE person_problem_links (
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

CREATE TABLE outreach (
  id INTEGER PRIMARY KEY,
  person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
  hypothesis_id INTEGER NOT NULL REFERENCES hypotheses(id),
  contact_point_id INTEGER REFERENCES contact_points(id),
  channel TEXT NOT NULL,                    -- email|linkedin|phone
  variant TEXT NOT NULL,                    -- A|B
  subject TEXT, body TEXT NOT NULL,
  facts_json TEXT NOT NULL, checks_json TEXT,
  status TEXT NOT NULL DEFAULT 'draft',     -- R-OUT-8
  approved_at TEXT, sent_at TEXT, follow_up_due TEXT,
  follow_up_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE interviews (
  id INTEGER PRIMARY KEY,
  person_id INTEGER REFERENCES people(id) ON DELETE SET NULL,
  pseudonym TEXT NOT NULL,                  -- R1, R2…
  hypothesis_id INTEGER NOT NULL REFERENCES hypotheses(id),
  interviewee_kind TEXT NOT NULL,           -- customer|expert
  held_at TEXT NOT NULL,
  last_occurrence TEXT, current_workaround TEXT, cost_impact TEXT,
  decision_and_payer TEXT, tried_before TEXT,
  mentioned_unprompted INTEGER,             -- 1|0|NULL
  commitment TEXT,                          -- intro|follow_up|data|pilot|none
  notes TEXT,
  document_id INTEGER REFERENCES documents(id),  -- pseudo-dokument s poznámkami (R-CRM-2)
  created_at TEXT NOT NULL
);

CREATE TABLE suppression (
  hash TEXT PRIMARY KEY,
  kind TEXT NOT NULL,                       -- email|phone|name_org|domain
  reason TEXT NOT NULL,                     -- objection|erasure|bounced|manual
  created_at TEXT NOT NULL
);

CREATE TABLE decisions (
  id INTEGER PRIMARY KEY,
  hypothesis_id INTEGER NOT NULL REFERENCES hypotheses(id),
  decision TEXT NOT NULL,                   -- continue|pivot|stop|pause
  rationale TEXT NOT NULL, metrics_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE model_judgments (
  id INTEGER PRIMARY KEY,
  task TEXT NOT NULL, object_type TEXT, object_id INTEGER,
  role TEXT NOT NULL, model TEXT, profile TEXT, prompt_version TEXT NOT NULL,
  params_json TEXT, output_json TEXT, verdict TEXT,
  prompt_tokens INTEGER, completion_tokens INTEGER, latency_s REAL,
  truncated INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_judgments_object ON model_judgments(task, object_type, object_id);

CREATE TABLE labels (
  id INTEGER PRIMARY KEY,
  task TEXT NOT NULL, object_type TEXT NOT NULL, object_id INTEGER NOT NULL,
  label TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL,
  UNIQUE (task, object_type, object_id)
);

CREATE TABLE review_queue (
  id INTEGER PRIMARY KEY,
  task TEXT NOT NULL, object_type TEXT NOT NULL, object_id INTEGER NOT NULL,
  reason TEXT NOT NULL,                     -- undecided|disagreement|truncated|low_agreement|sample
  status TEXT NOT NULL DEFAULT 'open',      -- open|done
  created_at TEXT NOT NULL,
  UNIQUE (task, object_type, object_id)
);

CREATE TABLE embeddings (
  object_type TEXT NOT NULL, object_id INTEGER NOT NULL, model TEXT NOT NULL,
  dim INTEGER NOT NULL, vector BLOB NOT NULL,   -- float32, normalizovaný na dĺžku 1
  created_at TEXT NOT NULL,
  PRIMARY KEY (object_type, object_id, model)
);

CREATE TABLE query_stats (
  arm TEXT PRIMARY KEY,                     -- kategória zdroja alebo bunka matice dopytov
  tries INTEGER NOT NULL DEFAULT 0, successes INTEGER NOT NULL DEFAULT 0,
  last_used_at TEXT, cursor TEXT            -- pozícia rotácie matice
);

CREATE TABLE bench (
  id INTEGER PRIMARY KEY, role TEXT NOT NULL, model TEXT,
  load_s REAL, prompt_tps REAL, gen_tps REAL, json_ok INTEGER, tools_ok INTEGER,
  created_at TEXT NOT NULL
);
```

---

## 14. Príkazy CLI

Existujúce príkazy ostávajú: `collect`, `extract`, `verify`, `cluster`, `critique`, `market`,
`report`, `deep-dive`, `note`, `status`, `list`. Nové a zmenené:

| Príkaz | Čo robí |
|---|---|
| `migrate` | migrácia schémy; spúšťa sa aj automaticky, vždy so zálohou |
| `run-all [--profile P] [--minutes M]` | celý beh v poradí fáz podľa modelov (kapitola 4) |
| `discover [--minutes M]` | objavovanie (R-DISC) |
| `sources --check` | overenie katalógu zdrojov |
| `select --hypothesis ID --people on` (alebo `off`) | zapne alebo vypne hľadanie ľudí pre hypotézu |
| `orgs --hypothesis ID` | organizácie k hypotéze |
| `people --hypothesis ID` | osoby, kontakty, väzby, priorita |
| `drafts --hypothesis ID` | návrhy oslovení |
| `gui` | GUI na 127.0.0.1:8501 |
| `eval --task T --profile P` (alebo `all`) `[--limit N]` | eval |
| `doctor [--wait S] [--bench]` | diagnostika a benchmark rolí |
| `forget --person ID` (alebo `--email X`, `--phone X`) | výmaz a potlačenie |
| `purge` | retencia |
| `privacy reapply` | znovu použije potlačenie, napr. po obnove zo zálohy |
| `export --what W --format F [--include-personal]` | W: hypotheses, people, outreach, interviews; F: csv, xlsx; do `exports/` |
| `export --person ID` | všetky údaje o osobe (žiadosť o prístup) |

`run_nightly.bat` sa upraví: spustí llama-swap namiesto llama-servera, `doctor --wait`, potom
`run-all --profile balanced` a nakoniec ukončí llama-swap aj procesy llama-server.

---

## 15. Konfigurácia (doplnky `config.yaml`)

Všetko má predvolené hodnoty v `config.py`. Existujúce sekcie ostávajú.

```yaml
models:                        # ak sekcia chýba, všetky roly = llm (správanie v0.1)
  base_url: http://127.0.0.1:8080/v1      # llama-swap alebo router mód llama-servera
  roles:
    fast:     {model: fast,     timeout_s: 600,  max_tokens: 2048}
    accurate: {model: accurate, timeout_s: 1800, max_tokens: 4096}
    embed:    {model: embed,    timeout_s: 120}
  profile: balanced
  self_consistency_temperature: 0.7
  tasks:                                   # profil balanced
    screen:           {role: fast,     thinking: false, k: 1}
    extract:          {role: fast,     thinking: false, k: 1}
    number_extract:   {role: fast,     thinking: false, k: 1}
    entail:           {role: accurate, thinking: false, k: 1}
    entail_gate:      {role: accurate, thinking: true,  k: 3}
    cluster_assign:   {role: accurate, thinking: false, k: 1}
    critique:         {role: accurate, thinking: true,  k: 1}
    market_structure: {role: accurate, thinking: true,  k: 1}
    deep_dive:        {role: fast,     thinking: false, k: 1}
    org_mentions:     {role: fast,     thinking: false, k: 1}
    person_mentions:  {role: fast,     thinking: false, k: 1}
    segment_filter:   {role: accurate, thinking: false, k: 1}
    contact_assoc:    {role: fast,     thinking: false, k: 1}
    role_map:         {role: accurate, thinking: true,  k: 3}
    role_fit:         {role: accurate, thinking: false, k: 1}
    draft:            {role: accurate, thinking: false, k: 1}
    draft_check:      {role: accurate, thinking: true,  k: 1}
  eval_profiles:                           # model a uvažovanie pre testovanú úlohu
    A: {model: fast,       thinking: false}
    B: {model: fast-think, thinking: true}   # samostatná položka v llama-swap (iný --reasoning-budget)
    C: {model: accurate,   thinking: false}
    D: {model: accurate,   thinking: true}

discovery:
  exploration_share: 0.3
  min_arm_share: 0.05
  sources_file: sources.yaml
  novelty_threshold: 0.80
  candidate_promotion: {min_claims: 2, min_domains: 2}
  pain_patterns:
    - nedostatok pracovníkov
    - meškanie dodávok
    - oneskorenie projektov
    - ručné prepisovanie údajov
    - evidencia v exceli
    - duplicitné zadávanie údajov
    - chybovosť
    - reklamácie
    - prestoje
    - administratívna záťaž
    - nová povinnosť od
    - pokuta za nesplnenie
    - nedostatočná kapacita
    - chýba prehľad o
    - rastúce náklady na
    - kontrola zistila nedostatky
    - dlhé čakacie lehoty
    - neefektívny proces
    - manual process bottleneck          # anglické zdroje EÚ
    - compliance burden
  sectors:                               # ľudské názvy pre dopyty; kódy SK NACE iba pre registre
    - priemyselná výroba
    - výroba strojov a zariadení
    - výroba kovových konštrukcií
    - automobilový priemysel
    - potravinárstvo
    - stavebníctvo
    - doprava a logistika
    - energetika
    - vodárenstvo a odpady
    - veľkoobchod
    - maloobchod
    - IT služby
    - účtovníctvo a poradenstvo
    - samosprávy obcí
    - štátna správa
    - nemocnice
    - sociálne služby
    - školy
    - poľnohospodárstvo

people:
  auto_select_top: 3
  min_strength: stredná
  max_orgs_per_hypothesis: 30
  max_people_per_hypothesis: 15
  max_people_per_org: 2
  max_experts_per_hypothesis: 3
  max_contact_pages_per_org: 5
  registry_delay_s: 1.0
  registry_cache_days: 30
  check_mx: false
  blocked_domains: [linkedin.com, facebook.com, instagram.com, x.com, twitter.com, tiktok.com]
  priority_weights: {role_fit: 0.35, link: 0.25, channel: 0.20, org_fit: 0.20}

outreach:
  sender:
    name: "{MENO_PRIEZVISKO}"
    email: "{EMAIL_ODOSIELATELA}"
    phone: ""                            # voliteľné
    affiliation: "{AFILIACIA}"           # napr. "študent inžinierskeho štúdia" – iba ak je to pravda
    privacy_notice_url: "{URL_INFORMACIE_O_SPRACUVANI}"
  language: sk
  daily_cap: 10
  follow_up_business_days: [5, 7]
  max_follow_ups: 1
  holidays: []                           # dátumy YYYY-MM-DD, ktoré sa nepočítajú ako pracovné dni
  email_subject_max_chars: 70
  email_max_words: 150
  linkedin_note_max_chars: 200           # bezplatný účet; Premium 300
  phone_opener_max_words: 80
  forbidden_phrases: [ponúkame, naše riešenie, náš produkt, demo, zľava, akcia, bezplatná konzultácia, cenová ponuka]
  exclude_orgs: []                       # IČO alebo domény, ktoré sa nikdy neoslovujú

privacy:
  lia_completed: false                   # true až po vyplnení docs/LIA_template.md
  retention_days_uncontacted: 90
  retention_days_after_interaction: 365
  retention_days_declined: 30
  retention_days_exports: 30
  pseudonymize_reports: true

gui:
  host: 127.0.0.1
  port: 8501
```

---

## 16. Míľniky a akceptačné kritériá

Pre každý míľnik platí:

- `python -m pytest -q` prechádza offline;
- README je aktualizované;
- `docs/DECISIONS.md` obsahuje rozhodnutia a odchýlky;
- v repozitári nie sú osobné údaje ani tajomstvá.

Na reálnom stroji `doctor` spúšťa používateľ.

Poradie: M1 → M2 → M3 → M4 → M5 → M6 → M7. M8 (objavovanie) od M4–M7 nezávisí a môže ísť skôr
alebo súbežne. AK = akceptačné kritérium.

### M1 – Základ: migrácie, roly modelov, provenance, benchmark

Rozsah:

- `migrations.py` (v1 → v2 so zálohou) a `busy_timeout`;
- `models.py`: roly, routing, profily, `run_task`, orezanie → `undecided`, zápis do `model_judgments`;
- `doctor --bench` a tabuľka `bench`;
- maskovanie logov;
- `llama-swap.example.yaml`, `start_llm_swap.bat` a úprava `run_nightly.bat` (príloha A);
- skontrolovať `.gitignore`: `exports/` už je, soľ a zálohy ležia v ignorovanom `data/`.

AK:

1. DB z v0.1 (fixture vytvorená starou schémou s dátami) sa zmigruje, dáta ostanú, vznikne záloha
   a `user_version = 2`. Druhé spustenie nič nezmení.
2. Bez sekcie `models` idú všetky úlohy na `llm` a testy v0.1 prechádzajú bez úprav.
3. V profile `balanced` ide `extract` na `fast` a `entail` na `accurate` (FakeLLM zaznamenáva pole `model`).
4. `finish_reason = "length"` vedie k `undecided` a riadku v `review_queue`; beh pokračuje.
5. Každé volanie modelu má riadok v `model_judgments` s `prompt_version`.
6. Log neobsahuje e-mail ani telefón z testovacej správy.

### M2 – GUI základ a kontrola

Rozsah:

- `radar/gui/` so stránkami Prehľad, Problémy, Dôkazy, Kontrola a Súkromie (zatiaľ iba stav);
- `python -m radar gui` a `.streamlit/config.toml`;
- tabuľky `labels` a `review_queue`.

AK:

1. Príkaz `gui` obsahuje `--server.address 127.0.0.1` a `--browser.gatherUsageStats false`;
   `config.toml` má rovnaké hodnoty.
2. Každá stránka sa načíta bez výnimky nad dočasnou DB (`streamlit.testing.v1.AppTest`).
3. Označenie v Kontrole vytvorí riadok v `labels` a uzavrie položku fronty.
4. Text zdroja so značkou `<script>` sa zobrazí escapovaný.

### M3 – Presnosť: kaskáda, self-consistency, eval

Rozsah:

- routing vo všetkých fázach v0.1; `entail` posudzuje iný model ako `extract`;
- self-consistency;
- `evals.py`, príkaz `eval`, Wilsonov interval, report, syntetické prípady.

AK:

1. Hlasovanie 3:0 a 2:1 dá verdikt (2:1 s príznakom). Pri 1:1:1 alebo `undecided` ide položka do kontroly.
2. Eval na fixture s 20 labelmi a FakeLLM s plánovanými odpoveďami spočíta presnosť, úplnosť, F1 a
   interval rovnako ako ručný výpočet v teste.
3. Pri menej ako 50 labeloch report píše „nedostatok dát“.
4. Pôvodné testy v0.1 prechádzajú.

### M4 – Organizácie

Rozsah:

- `registers.py` (RPO, RÚZ, cache, tempo, uvedenie licencie) a `orgs.py`;
- príkazy `select` a `orgs`;
- stránka Organizácie;
- `exclude_orgs`.

AK:

1. Z fixture odpovedí RPO (uložený JSON, bez siete) sa uloží názov, IČO, právna forma a hlavná
   činnosť. Adresa štatutára sa neuloží ani nezaloguje.
2. Organizácia menovaná v dôkaze sa uloží iba vtedy, keď je názov doslova v citáte alebo okne.
3. Web je overený iba vtedy, keď stránka obsahuje IČO alebo presný názov.
4. Organizácia v `exclude_orgs` sa neobjaví.

### M5 – Ľudia, kontakty, súkromie

Rozsah:

- `contacts.py` a `people.py`;
- `privacy.py`: brána LIA, potlačenie, `forget`, `purge`, pseudonymizácia;
- fetcher: blokované domény aj po presmerovaní, `fetch_html`;
- stránky Ľudia a kontakty a Súkromie.

AK (HTML fixtures):

1. Z `mailto:` aj z textu sa e-mail uloží s úryvkom, v ktorom doslova je.
2. Stránka so vzorom meno.priezvisko@ pre 3 osoby a štvrtou osobou bez e-mailu: štvrtej sa e-mail nevytvorí.
3. Pri `jan.novak(at)firma.sk` a Cloudflare ochrane vznikne `hidden_contact`, nikdy dekódovaný e-mail.
4. Telefón v rôznych zápisoch sa uloží v E.164.
5. Pri dvoch osobách v jednej tabuľke sa kontakty priradia podľa riadku. Kontakt mimo bloku osoby ostane generický.
6. Fetcher odmietne linkedin.com aj presmerovanie naň.
7. `people` bez `lia_completed` skončí so zrozumiteľnou chybou.
8. `forget --email` zmaže osobu a kontakty a pridá hash. Nový beh `people` nad rovnakou stránkou
   osobu znovu neuloží.
9. `purge` s posunutým časom zmaže neoslovených po 90 dňoch, `declined` po 30 dňoch a staré exporty.
10. Limity: najviac 15 osôb na hypotézu a 2 na organizáciu; priorita má uložené zložky.
11. Statický test: v `radar/` nie je `smtplib` ani klient sociálnych sietí.
12. Adresa z RPO `statutoryBodies` sa nikde neuloží.

### M6 – Návrhy oslovení

Rozsah:

- `outreach.py` a príkaz `drafts`;
- stránka Oslovenia: Kopírovať, `mailto:`, označenie odoslaného, follow-up, denné počítadlo.

AK:

1. E-mail má ≤ 150 slov a predmet ≤ 70 znakov; poznámka pre LinkedIn ≤ 200 znakov vrátane odkazu.
2. Návrh so zakázanou frázou, s číslom mimo faktov alebo bez pätičky dostane `needs_edit`.
3. Bez `gender_hint` je oslovenie „Dobrý deň,“.
4. Schválenie s placeholderom v profile odosielateľa je zablokované.
5. 11. schválenie v jeden deň je zablokované.
6. `follow_up_due` pripadne na 5. pracovný deň po `sent` (test cez víkend).
7. `objected` spustí `forget`.
8. Organizácia s prebiehajúcim súvisiacim obstarávaním nedostane návrh, iba upozornenie.
9. `mailto:` je správne URL-encoded (diakritika, nové riadky).

### M7 – Brána 0

Rozsah:

- `crm.py`;
- stránky Rozhovory a Brána 0, rozhodnutia;
- citáty z rozhovorov ako dôkazy;
- exporty;
- nové časti správy: Brána 0, nové problémy, čakajúce kontroly;
- README.

AK:

1. Citát z rozhovoru, ktorý nie je v poznámkach, je zamietnutý.
2. Pri fixture s 10 rozhovormi, 3 „spomenul sám“ a potvrdeným platiteľom je verdikt `pass`. Bez
   platiteľa je `chýba platiteľ`; pri 7 rozhovoroch `nedostatok dát`.
3. Experti sa do metrík nepočítajú.
4. Správa ani export bez `--include-personal` neobsahujú mená ani kontakty.

### M8 – Objavovanie nových problémov

Rozsah:

- `sources.yaml` a `sources --check`;
- `discover.py`: matica, regulačný radar, signály dopytu, bandit;
- `screen`;
- `novelty.py`: embeddingy, stav `candidate`;
- pôvod hypotéz a filtre v GUI.

AK:

1. Pri rozpočte 10 dokumentov a `exploration_share 0.3` sú aspoň 3 dokumenty z objavovania.
2. Bandit s pevným seedom dáva opakovateľné rozdelenie a každá kategória dostane aspoň 5 %.
3. S fake embeddingmi:
   - podobné tvrdenie sa zaradí k existujúcej hypotéze;
   - odlišné vytvorí `candidate` s `origin = discovery`;
   - povýšenie na `new` príde až po 2 tvrdeniach z 2 domén.
4. Dokument s výsledkom `screen` = nie sa neextrahuje.
5. Rotácia matice pokračuje tam, kde minulý beh skončil.

---

## 17. Otvorené otázky

Polož ich používateľovi naraz. Dovtedy platia predvolené hodnoty.

1. **Profil odosielateľa** (meno, e-mail, afiliácia, telefón). Predvolene placeholdery, schvaľovanie je zablokované.
2. **Kde bude zverejnená informácia o spracúvaní** (čl. 14)? Predvolene text v pätičke a ponuka
   poslať plné znenie na požiadanie.
3. **Ktoré hypotézy zapnúť pre hľadanie ľudí** na začiatok? Predvolene automaticky top 3 so silou aspoň stredná.
4. **Iba Slovensko, alebo aj Česko?** Predvolene iba SK.
5. **Presný model:** Qwen3.6-27B, alebo Gemma 4 31B? Predvolene Qwen3.6-27B, rozhodne eval.
6. **Prístup k datahubu Slovensko.Digital?** Predvolene nie.
7. **Rozsah objavovania:** všetky odvetvia, alebo užší výber? Predvolene všetky sekcie SK NACE.
8. **Organizácie, ktoré nikdy neoslovovať** (`exclude_orgs`)? Predvolene prázdne.
9. **Denný limit a follow-up:** vyhovuje limit 10 a jeden follow-up po 5–7 pracovných dňoch?

---

## 18. Hardvér a výkon

Odhady pre RTX 5070 Laptop (8 GB VRAM) a 32 GB RAM. Skutočné hodnoty zmeraj cez `doctor --bench`.

| | fast (MoE 35B-A3B) | accurate (hustý ~27B, časť v RAM) |
|---|---|---|
| Súbor Q4_K_M | ~21 GB | ~17 GB |
| Generovanie | ~20–40 tok/s | ~2–5 tok/s |
| Posúdenie (vstup ~2 000 tokenov, výstup ~150) | ~10 s | ~1–2 min |
| To isté s uvažovaním (~1 000 tokenov navyše) | ~1 min | ~4–8 min |
| Za 8 hodín | tisíce posúdení | ~250–450 bez uvažovania, ~60–120 s uvažovaním |

**Prečo kaskáda:** extrakcia v0.1 (do 40 dokumentov × až 8 častí) by na `accurate` trvala rádovo
10 a viac hodín. Spomalenie 10–30× preto platíme iba tam, kde rozhoduje o kvalite (posudzovanie,
väzby, návrhy) a kde sa naň spoliehajú kroky smerom k ľuďom. Na celý beh v profile `accurate` je
vhodný víkend.

- Naraz beží iba jeden veľký model; embedding model (~0,6 GB) môže bežať na CPU popri ňom.
- Pre `accurate` stačí kontext 16 384 tokenov, posúdenia majú vstup 2–4 tisíc tokenov.
- Disk: oba modely a embedding model zaberú ~40 GB, s alternatívami do ~60 GB.
- V noci: napájanie, režim výkonu, vypnutý spánok. Beh je obnoviteľný.

Nové závislosti: `streamlit` (≥ 1.40), `phonenumbers`, `openpyxl`, `numpy`.

---

## 19. Prílohy

### A. llama-swap (príklad; syntax over podľa README svojej verzie)

llama-swap je proxy s OpenAI API: podľa poľa `model` v požiadavke spustí príslušný `llama-server`
a ostatné podľa skupín vypne. `${PORT}` doplní llama-swap.

```yaml
# llama-swap.yaml
healthCheckTimeout: 600          # veľké modely sa z disku načítavajú dlho
logLevel: info
startPort: 10001

macros:
  server: "C:/llama.cpp/llama-server.exe --host 127.0.0.1 --port ${PORT} --jinja --flash-attn on"

models:
  fast:
    cmd: >
      ${server} -m C:/models/Qwen3.6-35B-A3B-Q4_K_M.gguf
      -ngl 99 --n-cpu-moe 30 -c 32768
      --cache-type-k q8_0 --cache-type-v q8_0 --reasoning-budget 0
    ttl: 0
  fast-think:                    # iba pre eval profil B
    cmd: >
      ${server} -m C:/models/Qwen3.6-35B-A3B-Q4_K_M.gguf
      -ngl 99 --n-cpu-moe 30 -c 32768
      --cache-type-k q8_0 --cache-type-v q8_0 --reasoning-budget 1024
    ttl: 0
  accurate:                      # -ngl nájdi cez llama-bench, nech ostane ~1 GB voľnej VRAM
    cmd: >
      ${server} -m C:/models/Qwen3.6-27B-Q4_K_M.gguf
      -ngl 20 -c 16384
      --cache-type-k q8_0 --cache-type-v q8_0 --reasoning-budget 2048
    ttl: 0
  embed:                         # pooling podľa modelu (bge-m3: cls); over
    cmd: >
      C:/llama.cpp/llama-server.exe --host 127.0.0.1 --port ${PORT}
      -m C:/models/bge-m3-Q8_0.gguf --embeddings --pooling cls -ngl 0 -c 8192
    ttl: 0

groups:
  llm:
    swap: true                   # naraz beží iba jeden veľký model
    exclusive: false             # nevypína skupinu embedding
    members: [fast, fast-think, accurate]
  embedding:
    swap: false
    exclusive: false
    persistent: true             # embedding na CPU môže bežať popri veľkom modeli
    members: [embed]
```

```bat
@echo off
REM start_llm_swap.bat – OpenAI API na 127.0.0.1:8080, modely sa menia podľa poľa "model"
C:\llama-swap\llama-swap.exe --config llama-swap.yaml --listen 127.0.0.1:8080
```

**Alternatíva: router mód llama-servera** (bez ďalšieho programu):

```
llama-server --models-preset models.ini --models-max 1 --host 127.0.0.1 --port 8080
```

Názov sekcie v INI je názov modelu, kľúče zodpovedajú prepínačom (napr. `model`, `ctx-size`, `ngl`).
Formát INI sa medzi verziami mení a niektoré návody uvádzajú prepínač `--models`. Či INI podporuje
`n-cpu-moe` a `reasoning-budget`, over cez `llama-server --help`. Skupiny (embedding popri LLM) tu
nie sú, preto je predvolená voľba llama-swap.

### B. `sources.yaml` (príklad; všetky URL over cez `sources --check`)

```yaml
- category: audit
  name: NKÚ SR – výsledky kontrol
  url: https://www.nku.gov.sk
  method: search
  queries: ['site:nku.gov.sk kontrola zistenia nedostatky']
  cadence_days: 14
  language: sk
  verified: false
- category: spending_review
  name: Útvar hodnoty za peniaze (MF SR) – revízie výdavkov
  url: https://www.mfsr.sk
  method: search
  queries: ['site:mfsr.sk revízia výdavkov opatrenia']
  cadence_days: 30
  language: sk
  verified: false
- category: fiscal
  name: Rada pre rozpočtovú zodpovednosť
  url: https://www.rrz.sk
  method: search
  cadence_days: 30
  language: sk
  verified: false
- category: ombudsman
  name: Verejný ochranca práv – správy o činnosti
  url: https://www.vop.gov.sk
  method: search
  cadence_days: 60
  language: sk
  verified: false
- category: legislation
  name: Slov-Lex – legislatívny proces a pripomienky
  url: https://www.slov-lex.sk
  method: search
  cadence_days: 7
  language: sk
  verified: false
- category: eu_obligations
  name: EUR-Lex – nové nariadenia a smernice
  url: https://eur-lex.europa.eu
  method: search
  cadence_days: 14
  language: en
  verified: false
- category: procurement
  name: ÚVO – vestník a prípravné trhové konzultácie
  url: https://www.uvo.gov.sk
  method: search
  queries: ['site:uvo.gov.sk "prípravné trhové konzultácie"']
  cadence_days: 7
  language: sk
  verified: false
- category: procurement
  name: Centrálny register zmlúv
  url: https://www.crz.gov.sk
  method: search
  cadence_days: 14
  language: sk
  verified: false
- category: annual_report
  name: RÚZ – výročné správy (časť o rizikách)
  url: https://www.registeruz.sk/cruz-public
  method: api
  cadence_days: 30
  language: sk
  verified: false
- category: association
  name: Podnikateľská aliancia Slovenska – prieskumy podnikateľského prostredia
  url: https://alianciapas.sk
  method: search
  cadence_days: 30
  language: sk
  verified: false
- category: association
  name: Slovenská obchodná a priemyselná komora
  url: https://www.sopk.sk
  method: search
  cadence_days: 30
  language: sk
  verified: false
```

### C. Príklady oslovení (cieľový štýl; `{…}` dopĺňa systém z faktov a configu)

**E-mail, varianta A** (odkaz na verejný výrok):

```
Predmet: Vaša skúsenosť s {krátky názov problému} – 20 minút?

Dobrý deň, pán {Priezvisko},

v {názov zdroja} ({mesiac rok}) ste uviedli, že {parafráza faktu F1}. Volám sa {MENO_PRIEZVISKO},
som {AFILIACIA} a skúmam, ako {segment} rieši {problém}.

Nejde o ponuku ani predaj. Chcem pochopiť, ako to u vás prebieha v praxi: kedy sa to stalo naposledy
a ako ste to riešili. Mali by ste v najbližších dvoch týždňoch 20 minút na telefonát alebo online hovor?

Ak to pre vás nie je téma, stačí odpísať „nie“ a viac sa neozvem.

S pozdravom
{MENO_PRIEZVISKO}
{EMAIL_ODOSIELATELA}

--
Váš pracovný kontakt som našiel na {URL_ZDROJA}. Použijem ho iba na dohodnutie tohto rozhovoru
(oprávnený záujem), uchovám ho najviac 12 mesiacov a na požiadanie ho hneď vymažem.
Viac: {URL_INFORMACIE_O_SPRACUVANI}
```

**E-mail, varianta B** (podľa funkcie):

```
Predmet: {Funkcia} a {proces} – krátka otázka

Dobrý deň,

volám sa {MENO_PRIEZVISKO}, som {AFILIACIA} a robím sériu rozhovorov s ľuďmi, ktorí
v {typ organizácie} zodpovedajú za {proces}. Zaujíma ma, ako to u vás prebieha v praxi.
Nejde o ponuku ani predaj.

Mali by ste v najbližších dvoch týždňoch 20 minút na telefonát? Ak je na to vhodnejší niekto iný
z vášho tímu, budem vďačný za nasmerovanie.

Ak to pre vás nie je téma, stačí odpísať „nie“.

S pozdravom
{MENO_PRIEZVISKO}
{EMAIL_ODOSIELATELA}

-- (pätička ako vo variante A)
```

**LinkedIn** (≤ 200 znakov vrátane odkazu):

```
Dobrý deň, skúmam, ako {segment} rieši {problém}. Mali by ste 20 min na rozhovor o vašej skúsenosti? Nič nepredávam. Info o údajoch: {KRATKY_ODKAZ}
```

**Telefón:**

```
Dobrý deň, tu {meno}. Volám s krátkou otázkou: skúmam, ako {segment} rieši {problém}, a vaše meno
som našiel na {zdroj}. Nič nepredávam. Máte teraz dve minúty, alebo sa vám hodí iný čas
na 20-minútový rozhovor?
Prvá otázka: Kedy ste naposledy riešili {problém}? Ako to prebiehalo?
Ak „pošlite mi e-mail“: Rád. Je správna adresa {e-mail zo zdroja}? (do e-mailu potom pätička)
```

**Follow-up** (jeden, po 5–7 pracovných dňoch):

```
Predmet: Re: {pôvodný predmet}

Dobrý deň, pripomínam sa s otázkou nižšie. Ak to pre vás nie je téma, pochopím a viac sa neozvem.
S pozdravom, {MENO_PRIEZVISKO}
```

### D. Otázky na rozhovor (podľa knihy The Mom Test, Rob Fitzpatrick)

- Kedy sa vám to stalo naposledy? Čo presne sa stalo?
- Ako to riešite dnes? Kto sa tým zaoberá?
- Koľko vás to stojí (čas, peniaze, chyby)? Ako to viete?
- Čo ste už skúšali? Prečo to nevyšlo?
- Kto by o riešení rozhodoval a z akého rozpočtu?
- S kým ďalším by som sa o tom mal porozprávať?

Nepýtať sa: „Kúpili by ste…?“, „Páčil by sa vám nástroj, ktorý…?“, „Koľko by ste zaplatili?“.
Odpovede na hypotetické otázky nie sú dôkaz; dôkaz je minulé správanie a záväzok (čas, predstavenie,
dáta, peniaze).
