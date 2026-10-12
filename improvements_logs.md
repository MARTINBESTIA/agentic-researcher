# Improvements log

Nápady na budúce vylepšenia, ktoré vznikli počas implementácie, ale do aktuálnej úlohy nepatria
(alebo neboli zadané). Prevažne časovo náročnejšie veci. Najnovšie dole.

Formát: **dátum – oblasť – nápad.** Prečo · odhad náročnosti.

---

- ~~**2026-10-11 – dôkazy – rozpoznať typ zdroja (dodávateľ / médiá / úrad / výskum).**~~ **Hotové 12. 10. 2026** (pravidlá + model raz na doménu; dodávatelia sa nepočítajú do sily). Agent pri teste
  hypotéz občas berie text z blogu dodávateľa softvéru ako podporu problému. Klasifikátor zdroja
  (doména + model) by vedel váhovať dôkazy a v GUI označiť „marketing dodávateľa“. · 1–2 dni.
- **2026-10-11 – hypotézy – hlbšie zlučovanie príliš úzkych hypotéz.** Jeden beh vytvoril 18 hypotéz
  o e-faktúrach; consolidate zlučuje iba dvojice nad prahom podobnosti. Hierarchia (téma → podproblémy)
  alebo zhlukovanie celého zoznamu naraz by zredukovalo šum. · 2–3 dni.
- **2026-10-11 – prevádzka – naplánovať nočný beh.** Úloha v Plánovači (scripts/register_weekly_task.ps1)
  nie je zaregistrovaná; poradie akcií by sa dalo spúšťať aj podľa času (napr. 22:00 test hypotéz,
  6:00 zber ľudí). · 0,5 dňa.
- **2026-10-11 – agent – vlastný „scout“ prompt pre slovenské úradné zdroje.** Sedenia prieskumu majú
  nízku výťažnosť (1–3 citáty zo 6 sedení); zoznam overených portálov (NKÚ, ÚVO, slov-lex, výročné
  správy) ako štartovacie URL by agentovi ušetril vyhľadávanie. · 1 deň.
- **2026-10-12 – kontakty – kontakty z PDF výročných správ a úradných zverejnení.** Krok `contacts` číta iba
  HTML stránky; mnohé obce majú kontakty zamestnancov v PDF (organizačný poriadok). Extrakcia blokov z PDF
  (pypdf + rovnaké pravidlo „meno pred kontaktom“) by doplnila časť zo 24 ľudí bez kontaktu. · 1 deň.
- **2026-10-12 – Brána 1 – posúdenie GDPR priamo v GUI pre zablokovaný dataset.** Dataset s osobnými údajmi
  sa dnes neuloží vôbec; tlačidlo „posúdenie GDPR hotové, ponechať iba agregáty“ by uložilo agregovanú
  verziu (napr. GROUP BY bez mien). · 1–2 dni.
- ~~**2026-10-12 – Brána 1 – ďalšie formáty a archívy.**~~ **Hotové 12. 10. 2026** (ZIP, XML; API so stránkovaním). Podporované sú CSV/XLSX/JSON/Parquet; chýba ZIP (veľa
  portálov balí CSV), XML (ITMS, vestník ÚVO) a API so stránkovaním (RÚZ, ITMS2014+). · 2–3 dni.
- **2026-10-12 – Brána 1 – lepšie časové stĺpce.** Obdobia ako „2021-Q1“, „01/2022“ alebo rok v názve stĺpca
  (široké tabuľky ŠÚ SR) sa dnes neprevedú na časový rad; normalizácia „wide → long“ by sprístupnila DATAcube. · 2 dni.
- **2026-10-12 – Brána 2 – viac modelov a sezónnosť.** Spätný test má lineárny trend a AR(1) proti naivnému odhadu;
  automatická detekcia periódy (mesačné dáta), exponenciálne vyrovnávanie a model naprieč organizáciami
  (pooled) by dali férovejšie porovnanie. · 2–3 dni.
- **2026-10-12 – Brána 2 – sledovanie expertov.** Spätná väzba expertov je dnes jedno pole pri experimente;
  prepojenie s formulárom Rozhovory (expert, dátum, citát) by dalo históriu a metriku „nové poznatky“. · 1 deň.
- **2026-10-12 – modely – automatický eval po zmene modelu v GUI.** Po výbere nového modelu pre rolu by sa
  mohol spustiť benchmark a eval na označených príkladoch a ukázať rozdiel oproti predchádzajúcemu. · 1–2 dni.
- **2026-10-12 – vyhľadávanie – spoľahlivejší vyhľadávač.** *Čiastočne hotové 12. 10. 2026: tempo, zopakovanie, dopyt bez úvodzoviek, pamäť dopytov; ostáva lokálny index domén a iný zdroj výsledkov.* SearXNG má pri automatických dopytoch funkčný
  najmä Bing a Yahoo (DuckDuckGo CAPTCHA, Google CSE limit, Seznam timeout; Mojeek a Startpage sú v novej
  verzii SearXNG vypnuté pre proof-of-work CAPTCHA). Kontakty k osobe a agenti tým strácajú väčšinu
  dopytov. Možnosti: tempo dopytov a rotácia vyhľadávačov v SearxClient, lokálny index vybraných domén
  (sitemap + crawl webov organizácií z DB), prípadne API s bezplatným limitom (pozor: odosiela mená osôb
  tretej strane – vyžaduje posúdenie v LIA). · 1–3 dni.
- ~~**2026-10-12 – Brána 1 – stránkovanie API.**~~ **Hotové 12. 10. 2026** (kurzor/offset/page z popisu OpenAPI). Endpoint API sa sťahuje iba prvou stranou (`limit=1000`); ITMS2014+
  stránkuje cez `minId`. Generické stránkovanie (limit/offset, minId, odkaz next) s limitom veľkosti by dalo
  úplný dataset na reprodukciu a spätný test. · 1 deň.
- **2026-10-12 – modely – MoE 35B ako presná rola?** qwen3.6:35b-a3b generuje 32,8 tok/s oproti 4,1 tok/s hustého
  27B. Ak eval (gold labely z Kontroly, ≥ 50 na úlohu) ukáže porovnateľnú presnosť pri entail/cluster_assign,
  nočný beh by stihol rádovo viac posúdení. Treba najprv nazbierať labely. · 0,5 dňa + labely.
- **2026-10-12 – modely – menej výmen modelov v run-all.** S rolou agent (35B) sa v rýchlej fáze strieda 9B a 35B;
  zoskupiť kroky agenta (scout, gate1-sources, validate-search) za sebou by ušetrilo ~1 min na výmenu. · 0,5 dňa.
- **2026-10-12 – prevádzka – nočný beh odolný voči spánku a výpadkom.** Teraz sa počítač neuspí pre nečinnosť a limit sa
  neráta počas spánku; po zatvorení veka však beh stojí. Plánovač úloh s „Wake the computer to run this task“ a
  kontrolou nabíjačky, plus obnovenie prerušenej akcie z poradia po štarte, by noc využili celú. · 0,5–1 deň.
- **2026-10-12 – agenti – prompt a nástroje pre modely, ktoré iba čítajú.** 9B aj MoE 35B agent číta, ale nástroj
  na uloženie takmer nevolá; dnes to dorovnáva extrakcia z prečítaných stránok. Skúsiť: nástroj read_page vracia
  rovno „kandidátske citáty“ na potvrdenie, alebo vynútiť record po každom read_page (tool_choice). · 1 deň.
