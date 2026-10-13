# Improvements log

Nápady na budúce vylepšenia, ktoré vznikli počas implementácie, ale do aktuálnej úlohy nepatria
(alebo neboli zadané). Prevažne časovo náročnejšie veci. Najnovšie dole.

Formát: **dátum – oblasť – nápad.** Prečo · odhad náročnosti.

---

- ~~**2026-10-11 – dôkazy – rozpoznať typ zdroja (dodávateľ / médiá / úrad / výskum).**~~ **Hotové 12. 10. 2026** (pravidlá + model raz na doménu; dodávatelia sa nepočítajú do sily). Agent pri teste
  hypotéz občas berie text z blogu dodávateľa softvéru ako podporu problému. Klasifikátor zdroja
  (doména + model) by vedel váhovať dôkazy a v GUI označiť „marketing dodávateľa“. · 1–2 dni.
- ~~**2026-10-11 – hypotézy – hlbšie zlučovanie príliš úzkych hypotéz.**~~ **Čiastočne hotové 12. 10. 2026** (témy nad hypotézami bez zlučovania; ostáva: test hypotéz a Brána 0 na úrovni témy, napr. vybrať celú tému na rozhovory). Jeden beh vytvoril 18 hypotéz
  o e-faktúrach; consolidate zlučuje iba dvojice nad prahom podobnosti. Hierarchia (téma → podproblémy)
  alebo zhlukovanie celého zoznamu naraz by zredukovalo šum. · 2–3 dni.
- **2026-10-11 – prevádzka – naplánovať nočný beh.** *12. 10. 2026: skript pripravený (`register_weekly_task.ps1 -Daily -Time 23:00 -Wake`, beh ako úloha Správcu akcií); registráciu spúšťaš ty. Ostáva poradie akcií podľa času (napr. 6:00 zber ľudí).* Úloha v Plánovači (scripts/register_weekly_task.ps1)
  nie je zaregistrovaná; poradie akcií by sa dalo spúšťať aj podľa času (napr. 22:00 test hypotéz,
  6:00 zber ľudí). · 0,5 dňa.
- ~~**2026-10-11 – agent – vlastný „scout“ prompt pre slovenské úradné zdroje.**~~ **Hotové 12. 10. 2026** (portály z katalógu Brány 1 v prompte prieskumu; výťažnosť ešte treba zmerať na behoch). Sedenia prieskumu majú
  nízku výťažnosť (1–3 citáty zo 6 sedení); zoznam overených portálov (NKÚ, ÚVO, slov-lex, výročné
  správy) ako štartovacie URL by agentovi ušetril vyhľadávanie. · 1 deň.
- ~~**2026-10-12 – kontakty – kontakty z PDF výročných správ a úradných zverejnení.**~~ **Hotové 12. 10. 2026** (vyhľadanie `filetype:pdf` na webe organizácie, okná textu okolo mena). Krok `contacts` číta iba
  HTML stránky; mnohé obce majú kontakty zamestnancov v PDF (organizačný poriadok). Extrakcia blokov z PDF
  (pypdf + rovnaké pravidlo „meno pred kontaktom“) by doplnila časť zo 24 ľudí bez kontaktu. · 1 deň.
- **2026-10-12 – Brána 1 – posúdenie GDPR priamo v GUI pre zablokovaný dataset.** Dataset s osobnými údajmi
  sa dnes neuloží vôbec; tlačidlo „posúdenie GDPR hotové, ponechať iba agregáty“ by uložilo agregovanú
  verziu (napr. GROUP BY bez mien). · 1–2 dni.
- ~~**2026-10-12 – Brána 1 – ďalšie formáty a archívy.**~~ **Hotové 12. 10. 2026** (ZIP, XML; API so stránkovaním). Podporované sú CSV/XLSX/JSON/Parquet; chýba ZIP (veľa
  portálov balí CSV), XML (ITMS, vestník ÚVO) a API so stránkovaním (RÚZ, ITMS2014+). · 2–3 dni.
- ~~**2026-10-12 – Brána 1 – lepšie časové stĺpce.**~~ **Hotové 12. 10. 2026** (dlhý tvar `dsN_long`; ostáva: exporty DATAcube s viacriadkovou hlavičkou nad tabuľkou – CSV sa načíta s nesprávnymi názvami stĺpcov, treba nájsť riadok hlavičky). Obdobia ako „2021-Q1“, „01/2022“ alebo rok v názve stĺpca
  (široké tabuľky ŠÚ SR) sa dnes neprevedú na časový rad; normalizácia „wide → long“ by sprístupnila DATAcube. · 2 dni.
- ~~**2026-10-12 – Brána 2 – viac modelov a sezónnosť.**~~ **Čiastočne hotové 12. 10. 2026** (sezóna podľa obdobia, ses, holt_damped, seasonal_trend, auto; ostáva model naprieč organizáciami – pooled). Spätný test má lineárny trend a AR(1) proti naivnému odhadu;
  automatická detekcia periódy (mesačné dáta), exponenciálne vyrovnávanie a model naprieč organizáciami
  (pooled) by dali férovejšie porovnanie. · 2–3 dni.
- **2026-10-12 – Brána 2 – sledovanie expertov.** Spätná väzba expertov je dnes jedno pole pri experimente;
  prepojenie s formulárom Rozhovory (expert, dátum, citát) by dalo históriu a metriku „nové poznatky“. · 1 deň.
- **2026-10-12 – modely – automatický eval po zmene modelu v GUI.** Po výbere nového modelu pre rolu by sa
  mohol spustiť benchmark a eval na označených príkladoch a ukázať rozdiel oproti predchádzajúcemu. · 1–2 dni.
- **2026-10-12 – vyhľadávanie – spoľahlivejší vyhľadávač.** *Čiastočne hotové 12. 10. 2026: tempo, zopakovanie, dopyt bez úvodzoviek, pamäť dopytov; pre Bránu 1 národný katalóg cez SPARQL (bez vyhľadávača); ostáva lokálny index domén organizácií.* SearXNG má pri automatických dopytoch funkčný
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
- ~~**2026-10-12 – modely – menej výmen modelov v run-all.**~~ **Čiastočne hotové 12. 10. 2026** (extrakcia po agentoch celej dávky v teste aj v Bráne 1; poradie krokov run-all ostáva kvôli prednosti Brány 1; ďalší krok: zmerať čas výmeny z logu Ollama `load_duration` a podľa toho rozhodnúť). S rolou agent (35B) sa v rýchlej fáze strieda 9B a 35B;
  zoskupiť kroky agenta (scout, gate1-sources, validate-search) za sebou by ušetrilo ~1 min na výmenu. · 0,5 dňa.
- **2026-10-12 – prevádzka – nočný beh odolný voči spánku a výpadkom.** *Čiastočne hotové 12. 10. 2026: „Pokračovať v poradí“ a „Spustiť znova“ v Správcovi akcií; skript s prebudením a kontrolou nabíjačky je pripravený (zaregistruješ ty).* Teraz sa počítač neuspí pre nečinnosť a limit sa
  neráta počas spánku; po zatvorení veka však beh stojí. Plánovač úloh s „Wake the computer to run this task“ a
  kontrolou nabíjačky, plus obnovenie prerušenej akcie z poradia po štarte, by noc využili celú. · 0,5–1 deň.
- **2026-10-12 – agenti – prompt a nástroje pre modely, ktoré iba čítajú.** 9B aj MoE 35B agent číta, ale nástroj
  na uloženie takmer nevolá; dnes to dorovnáva extrakcia z prečítaných stránok. Skúsiť: nástroj read_page vracia
  rovno „kandidátske citáty“ na potvrdenie, alebo vynútiť record po každom read_page (tool_choice). · 1 deň.
- ~~**2026-10-12 – Brána 2 – hodnoty filtrov z dát.**~~ **Hotové 12. 10. 2026** (schéma pre model uvádza všetky hodnoty textových stĺpcov s ≤ 30 hodnotami; výber v GUI zo zoznamu ostáva). Model pri predregistrácii vidí iba 3 ukážkové riadky, takže
  hodnotu filtra (presný názov ukazovateľa alebo kraja) často nepozná. Zoznam odlišných hodnôt nízkokardinalitných
  stĺpcov (napr. ≤ 50) v schéme a výber filtra v GUI zo zoznamu by znížili počet chybných návrhov. · 0,5 dňa.
- **2026-10-12 – Brána 1 – relevantnejšie kandidáty z národného katalógu.** Hľadanie podľa kmeňov slov z názvu poľa
  je zašumené (napr. „Rozpočet projektu“ → „Zoznam projektov rozvoja IT“). Lokálny index názvov a popisov ~22 500
  datasetov s embeddingmi (raz týždenne zo SPARQL) by dal poradie podľa významu poľa aj témy hypotézy. · 1 deň.
- **2026-10-12 – modely – otestovať presnosť ostatných modelov pre úlohy presnej roly (rozhodnutie: zatiaľ ostáva 27B).**
  Mapa rolí na qwen3.6:27b trvá ~194 s na vzorku × 3 vzorky ≈ 10 min na hypotézu (akcia #19: 53 hypotéz → ~9 h iba
  na mapy rolí); filter segmentu ~50 s, posúdenie citátu ~20 s. Kandidáti: MoE qwen3.6:35b-a3b (~8× rýchlejšie
  generovanie), 9B, prípadne menší počet vzoriek pri mape rolí (1 namiesto 3). Postup: pre každú úlohu presnej roly
  (entail, cluster_assign, role_map, role_fit, segment_filter, draft_check, gate1_*) zostaviť sadu s gold labelmi
  z Kontroly (≥ 50 na úlohu; pri role_map a segment_filter ručné posúdenie výstupov), spustiť `eval` pre všetky
  modely a porovnať presnosť, podiel neplatného JSON a čas na položku. Zhoda s 27B nie je presnosť – referenciou sú
  iba ľudské labely. Model meniť po úlohách, nie celú rolu naraz (napr. role_map na 35B, entail ostáva 27B), a iba
  ak rozdiel presnosti nie je štatisticky významný na úkor kvality. · 1 deň + zber labelov.
- **2026-10-13 – GUI – odhadované časy v dashboarde (požiadavka používateľa).** V Prehľade a Správcovi akcií chýba,
  kedy čo skončí. Doplniť: (1) pri bežiacej akcii odhad konca (začiatok + limit, presnejšie podľa priemerného trvania
  rovnakej akcie z histórie `jobs.stats_json` – úlohy často skončia skôr), (2) pri každej úlohe v poradí odhadovaný
  začiatok a koniec (kumulatívne podľa limitov a histórie), (3) pri autopilotovi koniec kola a celého behu a ktoré akcie
  ešte prídu na rad, (4) pri akciách v Správcovi akcií typické trvanie (medián z histórie) a pri kartách s prácou
  odhad, koľko z nej sa stihne (napr. test hypotéz ~3 min na hypotézu, posúdenie citátu ~2 s na 9B), (5) časy
  v miestnom čase namiesto UTC. · 0,5–1 deň.
- **2026-10-13 – Brána 1 – lepší výber čísla na reprodukciu.** Pri H11 boli jedinými cieľmi „10 mil. €“ (metodika), „40 %“
  (vlastnícky podiel) a „2025 decembra“ (dátum) – žiadne sa z dát zmlúv nedá vypočítať, preto reprodukcia „nevyšla“ a hypotéza
  nedostala stav pripravená napriek pokrytiu všetkých polí. Vyraďovať dátumy a roky, uprednostniť čísla o objeme, počte a sume
  z tvrdení o rovnakej doméne ako dataset; ak vhodné číslo neexistuje, označiť „reprodukcia nemožná (bez cieľa)“ namiesto
  „nevyšla“ a pripravenosť počítať bez tejto zložky. · 0,5 dňa.
- **2026-10-13 – objavovanie – rozpočet pre nové zdroje a kvalita hypotéz z inšpekcií.** Autopilot dáva objavovaniu
  15 dokumentov na úlohu, pri 15 kategóriách ~1 dokument na kategóriu (noc 13. 10.: 3 dokumenty na kategóriu) – na posúdenie
  zdrojov málo; agent prieskumu pritom za 17–22 min prečíta ~18 stránok a uloží 3–4 citáty. Zvážiť: viac dokumentov z katalógu
  (napr. 40) a menej sedení agenta. Správy inšpekcií (SIŽP) dávajú hypotézy o jednotlivých incidentoch („Spoločnosť X vypúšťa
  odpadové vody…“, „Analýza vzoriek zistila…“) – extrakcia by mala žiadať problém typu organizácie, nie jeden prípad. · 0,5 dňa.
- **2026-10-13 – Brána 1 → 2 – presnosť posúdenia tvojich dát a prechod bez skutočných dát.** Sudca (27B) triedi polia z popisu alebo schémy; nemá gold labely, takže presnosť nepoznáme. Zbierať tvoje opravy posúdenia (pole, áno/čiastočne/nie) ako labely pre eval. Hypotéza, ktorá prešla iba so schémou, by mala v Bráne 2 dostať príznak „bez dát – spätný test čaká“ a autopilot by ju nemal opakovane posielať do predregistrácie, kým nepribudne dataset. Zvážiť aj šablónu schémy (CSV hlavička z polí Brány 1 na stiahnutie), aby sa vstup ľahšie vypĺňal. · 0,5 dňa.
- **2026-10-13 – Brána 2 – experiment, ktorý testuje samotnú hypotézu.** Predregistrácia je vždy predpoveď časového radu (napr. mesačná suma zmlúv organizácie) proti naivnému odhadu. Pri H11 („neprehľadnosť financií a korupčné riziká“) to overuje predvídateľnosť výdavkov, nie problém. Doplniť typy experimentov podľa hypotézy: detekcia anomálií (zmluvy tesne pod limitom obstarávania, dodatky zvyšujúce sumu, koncentrácia dodávateľov) s ručným posúdením vzorky a metrikou presnosti; model navrhne typ, kód počíta. Okno CRZ (9 mesiacov) na sezónnosť nestačí – pre ročné porovnanie stiahnuť aspoň 24 mesiacov (~2× dlhšie sťahovanie, ~1 GB). · 1–2 dni.
- **2026-10-13 – Brána 2 – štítky z nálezov kontrol (NKÚ, ÚHP, ÚVO).** Experimenty typu poradie a porovnanie zatiaľ používajú náhradné štítky z dát (napr. dodatok s navýšením). Silnejšie štítky: organizácie a zmluvy so zisteniami NKÚ, negatívne hodnotenia ÚHP, rozhodnutia ÚVO – extrakcia z dokumentov (citát + IČO cez RPO), nová operácia `external_label` (spojenie podľa IČO a obdobia). Pozor na málo prípadov a oneskorenie nálezov. · 2 dni.
- **2026-10-13 – Brána 2 – merať kvalitu návrhov 27B.** Zapisovať, koľko návrhov prešlo skúšobným behom na prvý pokus a po oprave, koľko ich človek uzamkol bez úprav; podľa toho upraviť prompt alebo pridať príklady. · 0,5 dňa.
