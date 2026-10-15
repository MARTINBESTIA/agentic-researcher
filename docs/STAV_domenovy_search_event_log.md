# Stav implementácie: doménové vyhľadávanie a event log (dodatok 16. 10. 2026)

Zadanie: `researcher_2026-10-16_domenovy_search_event_log.md` (nadväzuje na audit 15. 10. 2026, M23–M27).
Tento súbor sa aktualizuje po každom míľniku. Ak práca skončí skôr, sekcia „Ešte nie je implementované“ je
presný zoznam toho, čo chýba.

## Rozdelenie na míľniky

| Míľnik | Obsah (body zadania) | Stav |
|---|---|---|
| M28 | 1, 4.1, 4.3, 5: stavy doménového výskumu, priority domén, tvrdenia so stavom a rozsahom, otázky s blokovanou akciou a históriou odpovedí, pripravenosť oslovenia nezávislá od karty, jedna hlavná otázka v prvom oslovení; schéma v14 | **hotové** (scenáre 1–5 okrem zdrojov z M29) |
| M29 | 3, 4.2: lokálny ESCO import, fulltext, kandidáti povolaní, ohraničený doménový výskum (limity, jeden pokus na verziu vstupu), partial / needs_research s dôvodom | **hotové** (konektory O*NET, NSP, MetaIS a online ESCO API zatiaľ nie – pozri nižšie) |
| M30 | 6: žiadosť o event log (účel, definícia prípadu, minimálna schéma, malý prvý krok, prínos, podmienky skúšky), alternatívy bez event logu | **hotové** |
| M31 | 7.1–7.2: import CSV/Parquet do priestoru organizácie, idempotentný, mapovanie, povinné kontroly, report kvality, dostupnosť metrík | rozpracované |
| M32 | 7.3, 8: ProcessReport (počty, frekvencie, varianty, nadväznosti, odstupy, trvanie, opakovanie, čas aktivity, odchýlka od termínu), voliteľný PM4Py adaptér, GUI a akcie | čaká |

## Ešte nie je implementované

Všetko z tabuľky vyššie okrem dokončených míľnikov (stav „hotové“).
