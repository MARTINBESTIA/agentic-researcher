# Plán: „Dosiér hypotézy“ – export všetkého k jednej hypotéze

Stav: implementované 14. 10. 2026 (rozhodnutia a merania v `docs/DECISIONS.md`). Pokyn používateľa: exportovať úplne
všetko relevantné k danej hypotéze – dáta, citácie, ľudí s kontaktnými údajmi …

## Čo vznikne

- `radar/dossier.py` – zber a zápis; jediné SQL v `radar/db/` (doplnia sa čítacie metódy).
- CLI: `python -m radar dossier --hypothesis 582 [--personal] [--data full|sample|none] [--no-related]`
- GUI: Problémy → detail hypotézy → „Exportovať všetko k hypotéze“: voľby, odhad veľkosti, tlačidlo,
  po dokončení stiahnutie (do ~200 MB) alebo cesta k súboru.
- Výstup: `exports/H582_<skratka>_<dátum>_<pseudonym|osobne>.zip` (exports/ je mimo gitu, purge po 30 dňoch).

## Obsah ZIP

| Priečinok / súbor | Obsah |
|---|---|
| `README.md` | čitateľný súhrn: hypotéza, segment, téma, stav testu a sila, Brány 0–2 (stav, rozhodnutia), kľúčové citáty, experimenty so závermi, počty ľudí a organizácií, zoznam súborov, licencie, upozornenie na osobné údaje |
| `prehlad.xlsx` | všetky tabuľky okrem dát ako hárky (na prácu v Exceli) |
| `dosier.json` | všetko okrem dát a textov dokumentov, strojovo čitateľné |
| `manifest.json` | čas, commit kódu, režim (pseudonym/osobne), súbory so SHA-256 a počtom riadkov |
| `hypoteza/` | záznam hypotézy (vrátane `validation_json`, `gate1_json`), rodič a podhypotézy, téma, poznámky, kritiky, odhad trhu |
| `dokazy/tvrdenia.csv` | všetky tvrdenia: citát, problém, dotknutá strana, typ, čísla, overenie kódom (skóre, poznámka), entail/gate verdikt, duplikáty, URL, názov, čas stiahnutia, hash, typ vydavateľa |
| `dokazy/dokumenty.csv` + `dokazy/texty/D<id>.txt` | zdrojové dokumenty a ich uložený text |
| `proveniencia/posudky_modelov.csv` | volania modelov k tvrdeniam, dokumentom, hypotéze, organizáciám a experimentom (úloha, model, verzia promptu, verdikt, výstup, tokeny, čas); k osobám a osloveniam iba v režime „osobne“ |
| `brana0/organizacie.csv` | organizácie (IČO, forma, sektor, web a jeho overenie, vzťah k hypotéze, fit, obstarávanie, vylúčené) + údaje z registrov s atribúciou RPO |
| `brana0/ludia.csv` | ľudia: väzba, priorita a jej zložky, verdikt, dôvod, dôkaz; meno a zdroj (URL + úryvok) iba v režime „osobne“, inak pseudonym |
| `brana0/kontakty.csv` | všeobecné kontakty organizácií vždy; osobné kontakty (e-mail, telefón, profil vrátane LinkedIn, „otvor ručne“) so zdrojom a úryvkom iba v režime „osobne“ |
| `brana0/mapa_roli.json`, `oslovenia.csv`, `rozhovory.csv`, `rozhodnutia.csv` | mapa rolí, návrhy oslovení (stav, texty), rozhovory, verdikty Brány 0 |
| `brana1/` | polia, zdroje, datasety (metadáta, profil, licencia), pokrytie organizácií, reprodukcie, žiadosti podľa infozákona, tvoje vstupy, rozhodnutia |
| `brana1/data/` | tabuľky datasetov: `full` = Parquet (ZSTD) celé + CSV pri ≤ 100 tis. riadkoch, `sample` = 1 000 riadkov CSV, `none` = nič; `SCHEMA.md` so stĺpcami a počtami |
| `brana2/` | karty (všetky verzie), experimenty (plán, hash predregistrácie, výsledok, príznaky, záver, spätná väzba expertov), `postup_E<id>.md` (gate2_trace), demá |

**Súvisiace hypotézy:** Brána 0 ostáva pri rodičovi (H582 nemá ľudí ani organizácie, H11 má 18 väzieb,
28 organizácií). Predvolene sa pridajú údaje rodiča a podhypotéz so stĺpcom `hypotéza`; `--no-related` ich vynechá.

## Súkromie (CLAUDE.md: „exporty osoby predvolene pseudonymizujú“)

- Predvolene pseudonymizovaný: osoby ako `P<id> (funkcia, typ organizácie)`, texty oslovení a poznámky
  rozhovorov cez `pseudonymize_text`, bez osobných kontaktov a zdrojových úryvkov s menom.
- Voľba „S menami a kontaktmi“ (`--personal`): plné údaje; README nesie upozornenie (oprávnený záujem,
  nepreposielať, zmazať po použití); do poznámok hypotézy sa zapíše záznam o exporte s počtom osôb
  (zodpovednosť podľa čl. 5 ods. 2 GDPR).
- Kontrola v teste: ZIP v režime pseudonym neobsahuje žiadne meno osoby ani osobný e-mail/telefón (prehľadanie všetkých súborov).
- Logy maskujú e-maily a telefóny ako doteraz; obsah exportu sa do logu nepíše.

## Kroky

1. `radar/db/`: chýbajúce čítacie metódy (tvrdenia s dokumentmi pre zoznam hypotéz, posudky modelov podľa typu a ID,
   ľudia zozbieraní pre hypotézu, kontakty organizácie, všetky verzie kariet, vstupy Brány 1).
2. `radar/dossier.py`: `collect()` → sekcie s riadkami; `estimate()` → odhad veľkosti dát; `write()` → ZIP + manifest
   (Parquet cez DuckDB `COPY … TO … (FORMAT parquet, COMPRESSION zstd)` z pripojenia iba na čítanie).
3. CLI `dossier`, GUI v detaile hypotézy na stránke Problémy.
4. Testy (offline, fakes): pseudonym bez osobných údajov; režim osobne s kontaktmi a záznamom do poznámok;
   podhypotéza obsahuje ľudí rodiča; `full`/`sample`/`none`; SHA-256 v manifeste sedí; tvrdenie má URL, čas, hash a citát.
5. Overenie na reálnych H582 (CRZ 2,1 mil. riadkov – čas a veľkosť) a H4 (ľudia, oslovenia).
6. README (príkaz), mapa kódu v CLAUDE.md, DECISIONS, ACCEPTANCE; commit a push.

## Na rozhodnutie (predvolené, ak nepovieš inak)

1. Osobné údaje: predvolene pseudonym, mená a kontakty po zaškrtnutí.
2. Dáta: predvolene celé (Parquet); pri H11/H581/H582 rádovo 100–200 MB (zmeriam).
3. Rodič a podhypotézy: predvolene zahrnuté.
