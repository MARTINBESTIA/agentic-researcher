# Problem Radar – sprievodca pre nového používateľa

Stav k 21. 10. 2026. Pre človeka, ktorý chce z aplikácie dostať **reálne problémy s odkazmi na zdroje, popis domény,
navrhnutý produkt, podklady z dát a postup, koho a s čím osloviť**, aby overil, či je problém naozaj relevantný.

## 1. Čo aplikácia robí – jednou vetou

Problem Radar na tvojom počítači hľadá vo verejných slovenských zdrojoch podnikateľské problémy, každé tvrdenie dokladá
doslovným citátom s odkazom, k problému pripraví popis domény, návrh riešenia, dátové podklady a zoznam ľudí a organizácií
na overovacie rozhovory – a nič za teba neodosiela.

**Čo nerobí:** neoveruje trh za teba. Citát z webu dokazuje, že niekto o probléme píše, nie že ho má konkrétna firma alebo
že za riešenie zaplatí. To zistíš až rozhovorom (tzv. Brána 0 – customer discovery: najprv over problém, potom stavaj).

## 2. Čo dostaneš ku každému problému a ako dôveryhodné to je

Všetko je na jednej strane: **Problémy → vyber hypotézu → záložka „Prehľad príležitosti“** (alebo súbor
`reports/prehlady_problemov/H<číslo>.md`). Tabuľka všetkých problémov je v `reports/prehlady_problemov/INDEX.md`.

| Čo chceš | Čo aplikácia dáva | Ako tomu veriť |
|---|---|---|
| **Odkazy na reálne problémy** | overené citáty (doslovný úryvok, odkaz, dátum stiahnutia) rozdelené podľa pôvodu: *nezávislý dôkaz problému* / *dodávatelia riešení* / *protiargumenty* / *iba kontext* | že citát je naozaj v zdroji, overuje kód. **„Nezávislý dôkaz 0“** znamená, že problém zatiaľ tvrdia hlavne tí, čo predávajú riešenie – marketing nie je dôkaz |
| **Popis domény** – čo problém reálne znamená | prehľad domény na ~5 minút: ako proces prebieha, 5 pojmov, kto s problémom pracuje / spravuje dáta / rozhoduje o rozpočte, rozhodnutie, dnešný postup, existujúce riešenia, úskalia významu dát, neznáme | všeobecný pre odvetvie, **nie fakt o konkrétnej firme**; čo zdroje nehovoria, je medzi neznámymi; stav „needs_review“ = hodnotiteľ mal výhrady |
| **Navrhnutý produkt** | *hypotéza riešenia*: výstup, ktorý pomôže (napr. poradie čo skontrolovať skôr, upozornenie, predpoveď), rozhodnutie, ktoré podporí, obchodná metrika a čo by nápad vyvrátilo; malý dodateľný výstup pre zákazníka / držiteľa dát / experta; pri konkrétnej organizácii až 3 možnosti riešenia a malá skúška; konkurencia, kritika („prečo to nemusí fungovať“) a hrubý odhad trhu | je to **návrh na overenie**, nie overený produkt. Úspory dodávateľov sa neprenášajú; percentá a sľuby bez merania kód zakazuje |
| **Podklady z dát** | tabuľka „čo je podložené a čo nie“: problém v zdrojoch, zákazník (rozhovory), dáta (Brána 1 – nájdené verejné datasety, pripravenosť), experimenty (Brána 2 – spätný test proti jednoduchému porovnaniu), ochota platiť | čísla počíta Python, nie model; tvrdenie o prínose dovolí až experiment, ktorý prekonal jednoduché porovnanie na reálnych dátach – syntetické dáta nestačia |
| **Koho a s čím osloviť** | poradie kontaktov (zákazník / držiteľ dát / expert) s dôvodom, prečo práve oni, čo od nich žiadať, kanálom a stavom; hlavná otázka a otázky „na posledný konkrétny prípad“; hotové návrhy e-mailov na tvoju kontrolu | kontakty sú iba pracovné a zverejnené samotnou organizáciou, so zdrojom; e-maily sa nehádajú; organizácia mimo segmentu je „preveriť rolu“, neoverené obstarávanie zastaví schválenie |

Na konci každého prehľadu je **„Čo chýba a ďalší krok“** – presná akcia v aplikácii, ktorá chýbajúcu časť doplní.

## 3. Ako začať (prvý týždeň)

1. Otvor `reports/prehlady_problemov/INDEX.md` alebo GUI **Problémy**. Vyber 2–3 problémy, ktoré majú **nezávislý dôkaz > 0**,
   popis domény aj kontakty.
2. Prečítaj v prehľade časť **3 (doména)** – pojmy a proces, aby si v rozhovore hovoril jazykom odvetvia.
3. Pozri časť **4 (produkt)** a **kritiku** – predstav si, čo by muselo byť pravda, aby to malo zmysel.
4. V časti **6** vezmi prvé 3–5 kontakty. Cieľ prvej správy: **overiť, či sa problém u nich stal**, nie predávať.
   Návrh e-mailu je v **Osloveniach** – skontroluj ho, schváľ, skopíruj a pošli **sám** (aplikácia nič neodosiela).
   Pred prvým odoslaním vyplň profil odosielateľa v `config.yaml` (inak v texte ostanú `{MENO_PRIEZVISKO}` a pod.).
5. Pre organizáciu, ktorá odpovie, si vytvor **kartu organizácie** (Brána 0 → Doména a prínos → Karta organizácie):
   konkrétny pracovný úkon, čo by sa zmenilo, minimálny vstup a malá skúška pre ňu.
6. Každú odpoveď **zapíš** (Oslovenia → výsledok, alebo pri karte): potvrdil / vyvrátil / nezistené, posledný prípad,
   nástroj, dôvod nezáujmu. Karta tej organizácie sa označí na obnovu; iné organizácie sa nemenia. „Bez odpovede“ nie je
   vyvrátenie.
7. Keď máš 8–12 rozhovorov, pozri **Brána 0 → Metriky a rozhodnutie** a zapíš rozhodnutie (pokračovať / pivot / stop).

## 4. Aktuálny stav tvojich dát (21. 10. 2026)

- 820 hypotéz, z toho 54 vybraných na Bránu 0; 2 028 overených citátov zo 4 071 dokumentov.
- **Najúplnejšie problémy** (nezávislý dôkaz, doména, produkt, kontakty, návrhy oslovení): H11 Neprehľadnosť financií
  v štátnych inštitúciách, H2 Ceny liekov, H14 Hodnotenie nákladovej efektívnosti liekov, H3 Malé realitné kancelárie.
  H4 Účtovné kancelárie má všetko okrem **nezávislého dôkazu** (21 z 23 citátov je od dodávateľov softvéru).
- Bez popisu domény, ale so silnými zdrojmi: H1 Eurofondy – kde sa projekty zasekávajú (13 nezávislých citátov),
  H64 Výber postupu verejného obstarávania (9).
- **Zákazník zatiaľ nepotvrdil nič:** 0 odoslaných oslovení, 0 rozhovorov. Tri experimenty Brány 2 sú staršieho typu
  a treba ich spustiť znova (spätný test) – zatiaľ nič nedokazuje prínos riešenia.

## 5. Ako čítať značky

- **zo zdroja / uviedol kontakt / pozorované v dátach / hypotéza / návrh / nevieme** – stav poznania pri každom výroku karty.
  Hypotéza má vždy dôvod a spôsob, ako ju vyvrátiť.
- **Tri pripravenosti karty:** *na rozhovor* (vieme koho a s akou otázkou), *na návrh skúšky* (úkon, zásah, vstup,
  metrika; zvlášť súhlas a dáta), *na tvrdenie o prínose* (iba po reálnom porovnaní). Sú nezávislé – rozhovor môžeš
  začať aj bez skúšky.
- **needs_review / na kontrolu** – hodnotiteľ (iný model) našiel výhradu; prečítaj ju v rozbaľovacej sekcii kontrol.
- **preveriť rolu** – organizácia nesedí do segmentu ako zákazník (napr. ministerstvo pri účtovných kanceláriách);
  môže byť držiteľ dát alebo expert.
- **obstarávanie neoverené** – našla sa iba stránka obstarávania; schválenie e-mailu čaká, kým to overíš.

## 6. Všetky funkcie aplikácie (prehľad)

**Hľadanie problémov**
- Objavovanie z verejných zdrojov (úrady, kontrolné správy, médiá, regulácia) podľa matice odvetvie × vzor bolesti;
  agent prieskumu na webe; zber k témam a RSS; regulačný radar (nové povinnosti).
- Extrakcia citátov → **overenie kódom** (citát a čísla doslova v zdroji) → posúdenie modelom → zaradenie k hypotéze;
  nový problém je kandidát, kým nemá 2 overené citáty z 2 domén.
- Test hypotéz (dôkazy za, proti a existujúce riešenia), kritika a hrubý odhad trhu, témy (zhluky podobných hypotéz),
  typ vydavateľa (úrad, médiá, výskum, združenie, dodávateľ).

**Brána 0 – overenie u ľudí**
- Organizácie k problému (z dôkazov, obstarávania, registrov RPO/RÚZ, webu overeného cez IČO), ľudia a ich zverejnené
  pracovné kontakty so zdrojom; plán kontaktov (zákazník / držiteľ dát / expert).
- **Koho osloviť:** odporúčanie s dôvodmi, zhoda so segmentom, stav potreby, používané riešenie, obstarávanie, stav
  komunikácie a fronta prvého oslovenia (jedna osoba na organizáciu, odstup 21 dní).
- **Prehľad domény** (ESCO povolania, ohraničený webový výskum), tvrdenia so stavom a otázky s tým, čo blokujú.
- **Návrh prínosu** pre typ adresáta a **karta organizácie** (pracovný prípad, prekážka, kto môže konať, dnešné riešenie,
  možnosti, malá skúška, otázky, ponuka) s cieleným výskumom medzier a sústredeným režimom pre 3 organizácie.
- **Návrhy oslovení** podľa účelu (rozhovor, žiadosť o dáta, posúdenie expertom, pilot) s kontrolami (dĺžka, sľuby, čísla,
  rod, pätička GDPR), stav pripravenosti, follow-up, odkaz `mailto:` – odosielaš ručne.
- **Výsledky oslovení a rozhovory** (The Mom Test), učenie z odpovedí po organizáciách, metriky a rozhodnutie Brány 0.

**Brána 1 – dáta**
- Polia potrebné na riešenie → zdroje (Národný katalóg otvorených dát, CRZ, NKÚ, …) s licenciou → stiahnutie do DuckDB
  s kontrolou obsahu → väzba poľa na stĺpec → zreprodukovanie známeho čísla → pripravenosť; žiadosti podľa infozákona;
  vloženie vlastných dát.

**Brána 2 – experimenty a demo**
- Problémová karta (problém, kto, rozhodnutie, výstup, metrika) a podhypotézy; experimenty (poradie, predpoveď, anomália,
  prah, porovnanie) s predregistráciou, spätným testom bez úniku budúcich dát a silným jednoduchým porovnaním; štyri osi
  výsledku a verdikt; demo pre organizáciu.

**Event logy a procesy**
- Žiadosť o event log iba keď odpovie na otázku karty; import CSV/Parquet do oddeleného priestoru organizácie s kontrolou
  kvality; procesný report (varianty, odstupy, trvania, opakovania) bez tvrdení o príčine či úspore.

**Automatizácia a modely**
- Správca akcií (každá akcia ako úloha na pozadí, poradie, zrušenie), autopilot s prioritami, lievik „hypotéza po
  hypotéze“, nočný beh `run-all`.
- Lokálne modely cez Ollama (rýchly 9B, presný 27B, agent 35B); voliteľne hodnotiteľ v Azure OpenAI s vlastným rozpočtom
  a iba pre úlohy nad verejnými údajmi.

**Výstupy**
- **Prehľad príležitosti** (jedna strana na problém) a tabuľka problémov; dosiér hypotézy (ZIP so všetkým, predvolene
  pseudonymizovaný); správa `reports/latest.md`; export kariet a CRM tabuliek.

**Kvalita a súkromie**
- Kontrolná fronta pre rozhodnutia človeka, eval a referenčné vzorky, benchmark modelov, `doctor`.
- GUI a služby iba na `127.0.0.1`, žiadna telemetria; nič sa neodosiela; LIA (oprávnený záujem), zoznam potlačenia,
  `forget`, pseudonymizácia v exportoch, logy bez e-mailov a telefónov; LinkedIn sa nesťahuje.

## 7. Obmedzenia, ktoré treba poznať

- Výstupy modelov sú návrhy – lokálny 27B občas napíše ako fakt to, čo zdroj nehovorí (preto hodnotiteľ a stav „na
  kontrolu“), alebo použije češtinu či angličtinu.
- Jedna karta organizácie na lokálnom modeli trvá 15–18 minút; prehľad domény a návrhy tiež desiatky minút.
- Vyhľadávanie (SearXNG v Dockeri) musí bežať; inak cielený výskum ostane „otvorený“.
- Verejné zdroje o mnohých problémoch píšu hlavne dodávatelia riešení – pozri „nezávislý dôkaz“.
- ESCO (európsky číselník povolaní) treba stiahnuť ručne (formulár na esco.ec.europa.eu) a naimportovať.

## 8. Spustenie

```
docker compose up -d                 # vyhľadávač SearXNG
python -m radar gui                  # GUI na http://127.0.0.1:8501
python -m radar problem-brief --all  # prehľady problémov do reports/prehlady_problemov/
python -m radar doctor               # kontrola modelov a služieb
```

Podrobnosti: `README.md` (inštalácia, Brány 0–2, bezpečnosť), `docs/organization_opportunities.md` (karty organizácií),
`docs/DECISIONS.md` (prečo je čo tak).
