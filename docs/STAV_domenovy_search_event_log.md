# Stav implementácie: doménové vyhľadávanie a event log (dodatok 16. 10. 2026)

Zadanie: `researcher_2026-10-16_domenovy_search_event_log.md` (nadväzuje na audit 15. 10. 2026, M23–M27).
Podrobnosti rozhodnutí sú v `docs/DECISIONS.md`, akceptačné testy v `docs/ACCEPTANCE.md` (riadky M28–M32).

## Rozdelenie na míľniky

| Míľnik | Obsah (body zadania) | Stav |
|---|---|---|
| M28 | 1, 4.1, 4.3, 5: stavy doménového výskumu, priority domén, tvrdenia so stavom a rozsahom, otázky s blokovanou akciou a históriou odpovedí, pripravenosť oslovenia nezávislá od karty, jedna hlavná otázka v prvom oslovení; schéma v14 | **hotové** |
| M29 | 3, 4.2: lokálny ESCO import, fulltext, kandidáti povolaní, ohraničený doménový výskum (limity, jeden pokus na verziu vstupu), partial / needs_research s dôvodom | **hotové** (bez konektorov uvedených nižšie) |
| M30 | 6: žiadosť o event log (účel, definícia prípadu, minimálna schéma, malý prvý krok, prínos, podmienky skúšky), alternatívy bez event logu | **hotové** |
| M31 | 7.1–7.2: import CSV/Parquet do priestoru organizácie, idempotentný, mapovanie, povinné kontroly, report kvality, dostupnosť metrík | **hotové** (bez XES) |
| M32 | 7.3, 8: ProcessReport (počty, frekvencie, varianty, nadväznosti, odstupy, trvanie, opakovanie, čas aktivity, odchýlka od termínu), voliteľný PM4Py adaptér, GUI a akcie | **hotové** |

Akceptačné scenáre dodatku (časť 9): 1–16 majú testy (pozri `docs/ACCEPTANCE.md`, M28–M32).

## Ešte nie je implementované

- **Ďalšie konektory zdrojov (3.1):** O*NET (americký kontext – iba doplnok), NSP (endpoint a podmienky prístupu treba
  overiť, nevymýšľať), MetaIS (agendy verejnej správy) a online ESCO API. Teraz: ESCO iba z ručne stiahnutého balíka
  CSV (`esco-import`) a web cez SearXNG. Bod 10.6 zadania: konektory doplniť podľa prvých troch domén.
- **Sémantické vyhľadávanie (3.2):** zámerne vypnuté (`semantic_search_enabled: false`), kým sa nezmerajú chyby fulltextu.
- **XES import (7.1):** voliteľný ďalší formát; teraz CSV a Parquet.
- **Pracovný čas (7.3):** nepočíta sa – vyžaduje dohodnutý kalendár a pravidlá organizácie (v reporte vždy „nie“).
- **Zložitejšie objektové väzby (6.2):** objednávka s viacerými položkami, čiastkové dodávky a faktúry – import ich
  nezlučuje a report upozorní na viac systémov či premenovania, ale samostatné mapovanie úrovní prípadu (object-centric)
  nie je.
- **Vyhodnotenie prínosu doménových podkladov (10, posledný odsek):** porovnanie ~20 kontaktov s podkladmi a bez nich
  (správnosť dôvodu výberu, opora tvrdení, konkrétnosť výstupu, primeranosť požiadavky, čas ručných opráv) nemá
  nástroj – dá sa urobiť ručne z Oslovení a výsledkov (metriky po organizáciách sú v M27).
- **Posúdenie procesného reportu na pozadí:** hodnotiteľ formulácií sa spúšťa tlačidlom v GUI (blokuje stránku počas
  volania modelu) alebo `process-report --review`; nie je samostatnou akciou v Správcovi akcií.
- **Retencia event logov:** dáta organizácie sa mažú iba ručne (`delete_import`, tlačidlo v GUI); automatická
  retencia ako pri osobných údajoch zatiaľ nie je.
- **PM4Py:** adaptér je pripravený a overuje verziu, ale knižnica nie je v `requirements.txt` (nová závislosť by
  potrebovala odôvodnenie) a reálne volanie `discover_dfg` nebolo overené na nainštalovanej verzii – iba na falošnom module.
