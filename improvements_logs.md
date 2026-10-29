# Improvements log

Nápady na budúce vylepšenia, ktoré vznikli počas implementácie, ale do aktuálnej úlohy nepatria
(alebo neboli zadané). Prevažne časovo náročnejšie veci. Najnovšie dole.

Formát: **dátum – oblasť – nápad.** Prečo · odhad náročnosti.

---

- ~~**2026-10-11 – dôkazy – rozpoznať typ zdroja (dodávateľ / médiá / úrad / výskum).**~~ **Hotové 12. 10. 2026** (pravidlá + model raz na doménu; dodávatelia sa nepočítajú do sily). Agent pri teste
  hypotéz občas berie text z blogu dodávateľa softvéru ako podporu problému. Klasifikátor zdroja
  (doména + model) by vedel váhovať dôkazy a v GUI označiť „marketing dodávateľa“. · 1–2 dni.
- ~~**2026-10-11 – hypotézy – hlbšie zlučovanie príliš úzkych hypotéz.**~~ **Hotové 14. 10. 2026** (Problémy → Témy: súhrn Brány 0 za tému – vybrané hypotézy, ľudia, oslovenia, rozhovory; „Vybrať celú tému na Bránu 0“, prieskum dát pre tému). **Čiastočne hotové 12. 10. 2026** (témy nad hypotézami bez zlučovania; ostáva: test hypotéz a Brána 0 na úrovni témy, napr. vybrať celú tému na rozhovory). Jeden beh vytvoril 18 hypotéz
  o e-faktúrach; consolidate zlučuje iba dvojice nad prahom podobnosti. Hierarchia (téma → podproblémy)
  alebo zhlukovanie celého zoznamu naraz by zredukovalo šum. · 2–3 dni.
- ~~**2026-10-11 – prevádzka – naplánovať nočný beh.**~~ **Hotové 14. 10. 2026** (Plánovač je zaregistrovaný; poradie podľa času: `workflows.autopilot_hours`, napr. {people: [6, 22]}). *12. 10. 2026: skript pripravený (`register_weekly_task.ps1 -Daily -Time 23:00 -Wake`, beh ako úloha Správcu akcií); registráciu spúšťaš ty. Ostáva poradie akcií podľa času (napr. 6:00 zber ľudí).* Úloha v Plánovači (scripts/register_weekly_task.ps1)
  nie je zaregistrovaná; poradie akcií by sa dalo spúšťať aj podľa času (napr. 22:00 test hypotéz,
  6:00 zber ľudí). · 0,5 dňa.
- ~~**2026-10-11 – agent – vlastný „scout“ prompt pre slovenské úradné zdroje.**~~ **Hotové 12. 10. 2026** (portály z katalógu Brány 1 v prompte prieskumu; výťažnosť ešte treba zmerať na behoch). Sedenia prieskumu majú
  nízku výťažnosť (1–3 citáty zo 6 sedení); zoznam overených portálov (NKÚ, ÚVO, slov-lex, výročné
  správy) ako štartovacie URL by agentovi ušetril vyhľadávanie. · 1 deň.
- ~~**2026-10-12 – kontakty – kontakty z PDF výročných správ a úradných zverejnení.**~~ **Hotové 12. 10. 2026** (vyhľadanie `filetype:pdf` na webe organizácie, okná textu okolo mena). Krok `contacts` číta iba
  HTML stránky; mnohé obce majú kontakty zamestnancov v PDF (organizačný poriadok). Extrakcia blokov z PDF
  (pypdf + rovnaké pravidlo „meno pred kontaktom“) by doplnila časť zo 24 ľudí bez kontaktu. · 1 deň.
- ~~**2026-10-12 – Brána 1 – posúdenie GDPR priamo v GUI pre zablokovaný dataset.**~~ **Hotové 14. 10. 2026** (Brána 1 → Datasety → Posúdenie GDPR: tvoj účel a právny základ, dataset sa stiahne znova bez stĺpcov s osobnými údajmi; rozhodnutie v poznámkach. Agregácia ostáva na tebe – kód iba odstráni stĺpce). *Neurobené 13. 10. 2026: rozhodnutie o spracovaní osobných údajov (agregácia, právny základ) je tvoje – zatiaľ sa takéto datasety neukladajú vôbec.* Dataset s osobnými údajmi
  sa dnes neuloží vôbec; tlačidlo „posúdenie GDPR hotové, ponechať iba agregáty“ by uložilo agregovanú
  verziu (napr. GROUP BY bez mien). · 1–2 dni.
- ~~**2026-10-12 – Brána 1 – ďalšie formáty a archívy.**~~ **Hotové 12. 10. 2026** (ZIP, XML; API so stránkovaním). Podporované sú CSV/XLSX/JSON/Parquet; chýba ZIP (veľa
  portálov balí CSV), XML (ITMS, vestník ÚVO) a API so stránkovaním (RÚZ, ITMS2014+). · 2–3 dni.
- ~~**2026-10-12 – Brána 1 – lepšie časové stĺpce.**~~ **Hotové 12. 10. 2026** (dlhý tvar `dsN_long`; ostáva: exporty DATAcube s viacriadkovou hlavičkou nad tabuľkou – CSV sa načíta s nesprávnymi názvami stĺpcov, treba nájsť riadok hlavičky). Obdobia ako „2021-Q1“, „01/2022“ alebo rok v názve stĺpca
  (široké tabuľky ŠÚ SR) sa dnes neprevedú na časový rad; normalizácia „wide → long“ by sprístupnila DATAcube. · 2 dni.
- ~~**2026-10-12 – Brána 2 – viac modelov a sezónnosť.**~~ **Hotové 13. 10. 2026** (ETS zo statsmodels, spoločný gradient boosting naprieč organizáciami, 4 naivné odhady × 5 metrík). Spätný test má lineárny trend a AR(1) proti naivnému odhadu;
  automatická detekcia periódy (mesačné dáta), exponenciálne vyrovnávanie a model naprieč organizáciami
  (pooled) by dali férovejšie porovnanie. · 2–3 dni.
- ~~**2026-10-12 – Brána 2 – sledovanie expertov.**~~ **Hotové 13. 10. 2026** (história záznamov pri experimente s odkazom na rozhovor – pseudonym, bez mien; metrika „nové poznatky X z Y“). Spätná väzba expertov je dnes jedno pole pri experimente;
  prepojenie s formulárom Rozhovory (expert, dátum, citát) by dalo históriu a metriku „nové poznatky“. · 1 deň.
- ~~**2026-10-12 – modely – automatický eval po zmene modelu v GUI.**~~ **Hotové 14. 10. 2026** (akcia „Eval presnosti modelov“, ponuka po zmene modelu, zber labelov jedným klikom na stránke Eval; reálny eval čaká na ≥ 50 gold labelov na úlohu – zatiaľ 0). *Čiastočne hotové 13. 10. 2026: po uložení modelov GUI ponúkne meranie rýchlosti a schopností; eval presnosti čaká na gold labely (Kontrola).* Po výbere nového modelu pre rolu by sa
  mohol spustiť benchmark a eval na označených príkladoch a ukázať rozdiel oproti predchádzajúcemu. · 1–2 dni.
- ~~**2026-10-12 – vyhľadávanie – spoľahlivejší vyhľadávač.**~~ **Hotové 14. 10. 2026** (blokujúci vyhľadávač sa 10 min vynechá; lokálny index webov organizácií zo sitemap – robots.txt aj vnorené, cache 30 dní – pri kontaktoch k osobe bez vyhľadávača). *Čiastočne hotové 12. 10. 2026: tempo, zopakovanie, dopyt bez úvodzoviek, pamäť dopytov; pre Bránu 1 národný katalóg cez SPARQL (bez vyhľadávača); ostáva lokálny index domén organizácií.* SearXNG má pri automatických dopytoch funkčný
  najmä Bing a Yahoo (DuckDuckGo CAPTCHA, Google CSE limit, Seznam timeout; Mojeek a Startpage sú v novej
  verzii SearXNG vypnuté pre proof-of-work CAPTCHA). Kontakty k osobe a agenti tým strácajú väčšinu
  dopytov. Možnosti: tempo dopytov a rotácia vyhľadávačov v SearxClient, lokálny index vybraných domén
  (sitemap + crawl webov organizácií z DB), prípadne API s bezplatným limitom (pozor: odosiela mená osôb
  tretej strane – vyžaduje posúdenie v LIA). · 1–3 dni.
- ~~**2026-10-12 – Brána 1 – stránkovanie API.**~~ **Hotové 12. 10. 2026** (kurzor/offset/page z popisu OpenAPI). Endpoint API sa sťahuje iba prvou stranou (`limit=1000`); ITMS2014+
  stránkuje cez `minId`. Generické stránkovanie (limit/offset, minId, odkaz next) s limitom veľkosti by dalo
  úplný dataset na reprodukciu a spätný test. · 1 deň.
- **2026-10-12 – modely – MoE 35B ako presná rola?** **Pripravené 14. 10. 2026, rozhodnutie čaká na labely** (akcia Eval; bez ≥ 50 gold labelov na úlohu sa presnosť porovnať nedá – zatiaľ 0 labelov). qwen3.6:35b-a3b generuje 32,8 tok/s oproti 4,1 tok/s hustého
  27B. Ak eval (gold labely z Kontroly, ≥ 50 na úlohu) ukáže porovnateľnú presnosť pri entail/cluster_assign,
  nočný beh by stihol rádovo viac posúdení. Treba najprv nazbierať labely. · 0,5 dňa + labely.
- ~~**2026-10-12 – modely – menej výmen modelov v run-all.**~~ **Meranie hotové 14. 10. 2026** (čas načítania modelu pri každej výmene – Ollama load_duration – v Správcovi akcií). **Čiastočne hotové 12. 10. 2026** (extrakcia po agentoch celej dávky v teste aj v Bráne 1; poradie krokov run-all ostáva kvôli prednosti Brány 1; ďalší krok: zmerať čas výmeny z logu Ollama `load_duration` a podľa toho rozhodnúť). S rolou agent (35B) sa v rýchlej fáze strieda 9B a 35B;
  zoskupiť kroky agenta (scout, gate1-sources, validate-search) za sebou by ušetrilo ~1 min na výmenu. · 0,5 dňa.
- ~~**2026-10-12 – prevádzka – nočný beh odolný voči spánku a výpadkom.**~~ **Hotové 14. 10. 2026** (`radar job … --resume` v run_weekly.ps1: zastavené poradie a prerušené úlohy z posledných 12 h pokračujú pred novým kolom. Zatvorené veko notebooku je nastavenie Windows – zmeníš ho ty: Možnosti napájania → Pri zatvorení veka → Nerobiť nič, pri napájaní). *Čiastočne hotové 12. 10. 2026: „Pokračovať v poradí“ a „Spustiť znova“ v Správcovi akcií; skript s prebudením a kontrolou nabíjačky je pripravený (zaregistruješ ty).* Teraz sa počítač neuspí pre nečinnosť a limit sa
  neráta počas spánku; po zatvorení veka však beh stojí. Plánovač úloh s „Wake the computer to run this task“ a
  kontrolou nabíjačky, plus obnovenie prerušenej akcie z poradia po štarte, by noc využili celú. · 0,5–1 deň.
- ~~**2026-10-12 – agenti – prompt a nástroje pre modely, ktoré iba čítajú.**~~ **Hotové 14. 10. 2026** (read_page ponúkne kandidátske citáty – vety s opisom problému; efekt treba zmerať na behoch: podiel sedení s record_evidence). 9B aj MoE 35B agent číta, ale nástroj
  na uloženie takmer nevolá; dnes to dorovnáva extrakcia z prečítaných stránok. Skúsiť: nástroj read_page vracia
  rovno „kandidátske citáty“ na potvrdenie, alebo vynútiť record po každom read_page (tool_choice). · 1 deň.
- ~~**2026-10-12 – Brána 2 – hodnoty filtrov z dát.**~~ **Hotové 12. 10. 2026** (schéma pre model uvádza všetky hodnoty textových stĺpcov s ≤ 30 hodnotami; výber v GUI zo zoznamu ostáva). Model pri predregistrácii vidí iba 3 ukážkové riadky, takže
  hodnotu filtra (presný názov ukazovateľa alebo kraja) často nepozná. Zoznam odlišných hodnôt nízkokardinalitných
  stĺpcov (napr. ≤ 50) v schéme a výber filtra v GUI zo zoznamu by znížili počet chybných návrhov. · 0,5 dňa.
- ~~**2026-10-12 – Brána 1 – relevantnejšie kandidáty z národného katalógu.**~~ **Hotové 14. 10. 2026** (lokálny index katalógu: 22 579 datasetov, vektor na sériu – 7 949; Brána 1 hľadá kandidátov aj podľa významu; akcia „Aktualizovať index národného katalógu“, autopilot raz za týždeň). **Čiastočne hotové 13. 10. 2026** (8 kandidátov z katalógu sa zoradí embeddingmi podľa poľa a hypotézy, agent dostane 2 najbližšie; ostáva lokálny index celého katalógu). Hľadanie podľa kmeňov slov z názvu poľa
  je zašumené (napr. „Rozpočet projektu“ → „Zoznam projektov rozvoja IT“). Lokálny index názvov a popisov ~22 500
  datasetov s embeddingmi (raz týždenne zo SPARQL) by dal poradie podľa významu poľa aj témy hypotézy. · 1 deň.
- **2026-10-12 – modely – otestovať presnosť ostatných modelov pre úlohy presnej roly (rozhodnutie: zatiaľ ostáva 27B).** **Pripravené 14. 10. 2026, rozhodnutie čaká na labely** (pozri Eval; zber labelov jedným klikom).
  Mapa rolí na qwen3.6:27b trvá ~194 s na vzorku × 3 vzorky ≈ 10 min na hypotézu (akcia #19: 53 hypotéz → ~9 h iba
  na mapy rolí); filter segmentu ~50 s, posúdenie citátu ~20 s. Kandidáti: MoE qwen3.6:35b-a3b (~8× rýchlejšie
  generovanie), 9B, prípadne menší počet vzoriek pri mape rolí (1 namiesto 3). Postup: pre každú úlohu presnej roly
  (entail, cluster_assign, role_map, role_fit, segment_filter, draft_check, gate1_*) zostaviť sadu s gold labelmi
  z Kontroly (≥ 50 na úlohu; pri role_map a segment_filter ručné posúdenie výstupov), spustiť `eval` pre všetky
  modely a porovnať presnosť, podiel neplatného JSON a čas na položku. Zhoda s 27B nie je presnosť – referenciou sú
  iba ľudské labely. Model meniť po úlohách, nie celú rolu naraz (napr. role_map na 35B, entail ostáva 27B), a iba
  ak rozdiel presnosti nie je štatisticky významný na úkor kvality. · 1 deň + zber labelov.
- ~~**2026-10-13 – GUI – odhadované časy v dashboarde (požiadavka používateľa).**~~ **Hotové 13. 10. 2026** (`radar/eta.py`: odhad konca bežiacej úlohy z histórie a limitu, začiatok a koniec úloh v poradí, „všetko hotové ~HH:MM“, typické trvanie na kartách akcií, časy v miestnom čase). V Prehľade a Správcovi akcií chýba,
  kedy čo skončí. Doplniť: (1) pri bežiacej akcii odhad konca (začiatok + limit, presnejšie podľa priemerného trvania
  rovnakej akcie z histórie `jobs.stats_json` – úlohy často skončia skôr), (2) pri každej úlohe v poradí odhadovaný
  začiatok a koniec (kumulatívne podľa limitov a histórie), (3) pri autopilotovi koniec kola a celého behu a ktoré akcie
  ešte prídu na rad, (4) pri akciách v Správcovi akcií typické trvanie (medián z histórie) a pri kartách s prácou
  odhad, koľko z nej sa stihne (napr. test hypotéz ~3 min na hypotézu, posúdenie citátu ~2 s na 9B), (5) časy
  v miestnom čase namiesto UTC. · 0,5–1 deň.
- ~~**2026-10-13 – Brána 1 – lepší výber čísla na reprodukciu.**~~ **Hotové 13. 10. 2026** (bez rokov a dátumov, prednosť objem a počet; verdikt „bez cieľa“ a pripravenosť bez tejto zložky). Pri H11 boli jedinými cieľmi „10 mil. €“ (metodika), „40 %“
  (vlastnícky podiel) a „2025 decembra“ (dátum) – žiadne sa z dát zmlúv nedá vypočítať, preto reprodukcia „nevyšla“ a hypotéza
  nedostala stav pripravená napriek pokrytiu všetkých polí. Vyraďovať dátumy a roky, uprednostniť čísla o objeme, počte a sume
  z tvrdení o rovnakej doméne ako dataset; ak vhodné číslo neexistuje, označiť „reprodukcia nemožná (bez cieľa)“ namiesto
  „nevyšla“ a pripravenosť počítať bez tejto zložky. · 0,5 dňa.
- ~~**2026-10-13 – objavovanie – rozpočet pre nové zdroje a kvalita hypotéz z inšpekcií.**~~ **Hotové 13. 10. 2026** (40 dokumentov a 3 sedenia na úlohu; extrakcia: jednotlivý incident iba ak text hovorí o opakovanom alebo rozšírenom probléme). Autopilot dáva objavovaniu
  15 dokumentov na úlohu, pri 15 kategóriách ~1 dokument na kategóriu (noc 13. 10.: 3 dokumenty na kategóriu) – na posúdenie
  zdrojov málo; agent prieskumu pritom za 17–22 min prečíta ~18 stránok a uloží 3–4 citáty. Zvážiť: viac dokumentov z katalógu
  (napr. 40) a menej sedení agenta. Správy inšpekcií (SIŽP) dávajú hypotézy o jednotlivých incidentoch („Spoločnosť X vypúšťa
  odpadové vody…“, „Analýza vzoriek zistila…“) – extrakcia by mala žiadať problém typu organizácie, nie jeden prípad. · 0,5 dňa.
- ~~**2026-10-13 – Brána 1 → 2 – presnosť posúdenia tvojich dát a prechod bez skutočných dát.**~~ **Hotové 13. 10. 2026** (opravy posúdenia polí sa ukladajú ako gold labely `gate1_user_eval`; šablóna CSV z polí; Brána 2 bez datasetu upozorní a experimenty sa nenavrhujú). Sudca (27B) triedi polia z popisu alebo schémy; nemá gold labely, takže presnosť nepoznáme. Zbierať tvoje opravy posúdenia (pole, áno/čiastočne/nie) ako labely pre eval. Hypotéza, ktorá prešla iba so schémou, by mala v Bráne 2 dostať príznak „bez dát – spätný test čaká“ a autopilot by ju nemal opakovane posielať do predregistrácie, kým nepribudne dataset. Zvážiť aj šablónu schémy (CSV hlavička z polí Brány 1 na stiahnutie), aby sa vstup ľahšie vypĺňal. · 0,5 dňa.
- ~~**2026-10-13 – Brána 2 – experiment, ktorý testuje samotnú hypotézu.**~~ **Hotové 13. 10. 2026** (problémová karta, 5 typov úloh, operácie podľa hypotézy, CRZ 36 mesiacov). Predregistrácia je vždy predpoveď časového radu (napr. mesačná suma zmlúv organizácie) proti naivnému odhadu. Pri H11 („neprehľadnosť financií a korupčné riziká“) to overuje predvídateľnosť výdavkov, nie problém. Doplniť typy experimentov podľa hypotézy: detekcia anomálií (zmluvy tesne pod limitom obstarávania, dodatky zvyšujúce sumu, koncentrácia dodávateľov) s ručným posúdením vzorky a metrikou presnosti; model navrhne typ, kód počíta. Okno CRZ (9 mesiacov) na sezónnosť nestačí – pre ročné porovnanie stiahnuť aspoň 24 mesiacov (~2× dlhšie sťahovanie, ~1 GB). · 1–2 dni.
- ~~**2026-10-13 – Brána 2 – štítky z nálezov kontrol (NKÚ, ÚHP, ÚVO).**~~ **Doplnené 14. 10. 2026:** ÚHP – zoznam hodnotených projektov (opendata.mfsr.sk) so „Stanoviskom ÚHP“ a „Navrhovanou úsporou“ je v dátach H11/H581/H582 a plánovač dostal jeho popis (značka „chýbajúce dáta“ pri H581 bola chybná – zrušená). ÚVO „Správne delikty“ je v národnom katalógu, ale iba ako HTML na uvo.gov.sk a robots.txt ÚVO sťahovanie zakazuje – automaticky nie; stiahni ich ručne a vlož v Bráne 1 → Tvoje dáta. **Čiastočne hotové 13. 10. 2026** (NKÚ: otvorený dataset kontrolných zistení spojený so zoznamom subjektov – 34 tis. zistení, 99,7 % s IČO; operácia `lookup` pripojí ľubovoľný dataset hypotézy podľa IČO a časového okna; „Pridať dataset z odkazu“ v Bráne 1. Ostáva: ÚVO „Správne delikty“ je iba HTML, ÚHP hodnotenia nemajú dataset). Experimenty typu poradie a porovnanie zatiaľ používajú náhradné štítky z dát (napr. dodatok s navýšením). Silnejšie štítky: organizácie a zmluvy so zisteniami NKÚ, negatívne hodnotenia ÚHP, rozhodnutia ÚVO – extrakcia z dokumentov (citát + IČO cez RPO), nová operácia `external_label` (spojenie podľa IČO a obdobia). Pozor na málo prípadov a oneskorenie nálezov. · 2 dni.
- ~~**2026-10-13 – Brána 2 – merať kvalitu návrhov 27B.**~~ **Hotové 13. 10. 2026** (pri návrhu: pokusy, bez chýb na prvý pokus, po oprave; pri uzamknutí: zmenil človek plán?; súhrn v Bráne 2; voliteľné automatické uzamknutie bezchybných návrhov). Zapisovať, koľko návrhov prešlo skúšobným behom na prvý pokus a po oprave, koľko ich človek uzamkol bez úprav; podľa toho upraviť prompt alebo pridať príklady. · 0,5 dňa.
- ~~**2026-10-14 – vyhľadávanie – zablokované vyhľadávače v SearXNG.**~~ **Hotové 14. 10. 2026** (vynechanie blokujúceho vyhľadávača na 10 min, počty prázdnych hľadaní v súhrne úlohy, agent dostane radu namiesto prázdneho výsledku. Ďalšie vyhľadávače nepridané – Brave, Google a Qwant blokujú hneď, Mojeek a Startpage majú CAPTCHA). Počas úlohy #75 hlásil SearXNG DuckDuckGo „CAPTCHA“, Google CSE „too many requests“ a Bing výpadky; 5 hľadaní agenta zdrojov vrátilo nulu výsledkov a agent čítal nesúvisiace stránky (Stack Overflow, e-shop). Možnosti: pridať ďalšie vyhľadávače (Brave, Qwant, Startpage ak prejde PoW), pauzu a opakovanie pri prázdnej odpovedi s nedostupnými vyhľadávačmi, merať podiel prázdnych hľadaní v súhrne úlohy a pri vysokom podiele agenta zdrojov odložiť. · 0,5–1 deň.
- **2026-10-14 – Brána 0 – návrh žiadosti o vzorku dát pre držiteľov dát.** Držitelia dát (kontaktný bod datasetu,
  vydavateľ zdroja Brány 1) dnes nedostanú návrh oslovenia – rozhovor podľa Mom Test sa k nim nehodí. Variant návrhu
  „schéma + 20 anonymizovaných riadkov výmenou za konkrétny výstup“ (fakty: pole, zdroj, čo s dátami urobím; kontroly
  kódom ako pri oslovení; pri úrade odkaz na infozákon) by z „Koho osloviť“ spravil jeden klik. · 1 deň.
- **2026-10-14 – kontakty – zmerať výťažnosť hľadania podľa hypotézy.** Po prvých behoch `contact-search`: koľko dopytov
  dá overenú organizáciu, koľko z nich osobu s e-mailom, podľa typu cieľa (zákazník / držiteľ dát / expert) a koľko
  zákazníkov vyradí filter segmentu. Podľa toho upraviť prompt `contact_plan` (napr. viac dopytov na kontaktné stránky
  úradov, menej všeobecných) a `contact_queries_per_run`. · 0,5 dňa + behy.
- **2026-10-14 – dosiér – plán kontaktov a „Koho osloviť“ v ZIP.** Dosiér hypotézy zatiaľ neobsahuje ciele plánu
  (`contact_targets`) ani odporúčania; pseudonymizovaná verzia by mala typ, funkciu, organizáciu, odporúčanie a čo žiadať
  (bez mien a kontaktov). · 0,5 dňa.
- **2026-10-15 – lievik – voliteľné kroky kritika a návrhy oslovení.** Lievik ide test → kontakty → Brána 1 → Brána 2;
  kritika (protiargumenty, otázky na rozhovory) a návrhy oslovení pre „oslov najprv“ by sa dali pridať ako kroky
  (`funnel_steps`) hneď po kontaktoch, aby bola hypotéza pripravená aj na rozhovory Brány 0. · 0,5 dňa.
- **2026-10-15 – lievik – menej výmen modelov.** Pri jednej hypotéze sa modely striedajú ~6–8× (agent, sudca, presný).
  Zoskupiť kroky s rovnakou rolou (napr. agent testu ďalšej hypotézy hneď po agentovi zdrojov Brány 1) by ušetrilo
  ~5 min na hypotézu; zmerať najprv čas výmen v Správcovi akcií. · 1 deň.
- **2026-10-15 – Brána 1 – opakovať import z pôvodného súboru po novej verzii parsera.** Pôvodné súbory sa už uchovávajú
  podľa SHA-256 (`data/datasets/raw`); príkaz `gate1-reimport` by pri zmene `PARSER_VERSION` znova naimportoval
  datasety bez sťahovania a väzby by sa overili na novej verzii (`refresh_stale`). · 0,5 dňa.
- **2026-10-15 – Brána 1 – referenčná vzorka väzieb a importov.** Potvrdenia a zamietnutia stĺpcov (`field_binding`) a
  kontroly importu (`dataset_import`) sú labely; po ~30 väzbách a ~20 importoch zmerať presnosť návrhov modelu a
  pravidiel kontroly (eval s Wilsonovým intervalom, holdout). · 0,5 dňa.
- **2026-10-15 – Brána 1 – pojmová poistka zo slovníka domény.** Pojmy (patent, cena, odpoveď …) sú zatiaľ pevný
  zoznam v `bindings.CONCEPTS`; zamietnuté väzby s dôvodom by mohli navrhovať nové pojmy (človek schváli). · 1 deň.
- **2026-10-15 – Brána 2 – dátum zverejnenia NKÚ a zoznam kontrol bez zistení.** Štítky NKÚ sú od M21 bez
  `label_population` neplatné. Doplniť adaptér zoznamu kontrolných akcií (subjekty vrátane kontrol bez zistení) a dátum
  zverejnenia správy (stránka výsledku kontroly) – potom sa dá E6/H582 navrhnúť znova s platným štítkom. · 1–2 dni.
- **2026-10-15 – Brána 2 – workflow experiment (nie predikcia).** Audit: nie každá hypotéza potrebuje model; pre
  automatizáciu práce typ úlohy „workflow“ (čas, chybovosť, výsledok práce pred a po) s meraním u používateľa. · 2 dni.
- **2026-10-15 – Brána 2 – časové bloky aj pre predpoveď.** Bootstrap predpovede je po entitách; pri jednej entite by
  mal prevzorkovať bloky období. · 0,5 dňa.
- **2026-10-15 – kontakty – účel z textu stránky, nie iba z funkcie.** Účel kontaktu sa zatiaľ určuje z názvu funkcie
  pravidlami; dôkaz zodpovednosti by mohol byť aj veta zo stránky („zodpovedá za fakturáciu“) – kód by ju našiel
  v okolí mena a doslovne uložil. Najprv zmerať na referenčnej vzorke `contact_role`. · 1 deň.
- **2026-10-15 – autopilot – ladenie podielu zberu a počtu priorít.** 25 % a 3 priority sú pracovné nastavenie z auditu;
  po 2 týždňoch porovnať postup k zákazníkovi (Prehľad) a počet nových hypotéz a upraviť. · 0,5 dňa.
- **2026-10-17 – karty – živý cielený výskum medzier.** Vyhodnotenie troch kariet bežalo bez SearXNG (Docker nebol
  spustený); verejné medzery ostali otvorené. Po spustení SearXNG pustiť `org-cards --run --target …` a zmerať, koľko
  medzier sa reálne zodpovie (nájdené / verejne nezistiteľné) a či rozsah „organizácia“ pri úryvkoch sedí. · 0,5 dňa.
- **2026-10-17 – karty – názov pracovného prípadu s diakritikou.** `process_key` je slug bez diakritiky („prepis
  uctovnych dokladov“) a tak sa zobrazuje aj v otázkach medzier; uložiť zvlášť čitateľný názov z výstupu modelu. · 1 h.
- **2026-10-17 – karty – porovnanie kariet 27B a gpt-6-luna.** Karty vo vyhodnotení písal gpt-6-luna (kópia
  s presnou rolou v cloude); v hlavnej konfigurácii ide `org_card` na lokálny 27B – zmerať kvalitu (hodnotiaca tabuľka,
  výhrady kódu a hodnotiteľa) a čas na tých istých troch organizáciách. · 0,5 dňa.
- **2026-10-17 – karty – overenie obstarávania z konkrétneho záznamu ÚVO.** `assess_procurement` je heuristika nad
  titulkom a útržkom; s povoleným čítaním vestníka by sa dal overiť predmet a lehota zo stránky záznamu. · 1 deň.
- **2026-10-17 – karty – pseudonymizácia v porovnaní a exporte.** Kmeňové nahrádzanie názvov radšej nahradí viac;
  pri reálnom zdieľaní ešte prezrieť názvy produktov (referencie dodávateľov môžu prezradiť organizáciu). · 0,5 dňa.
- **2026-10-21 – sila dôkazov podľa relevantnosti.** `strength.evidence_strength` (test hypotéz, poradie) ráta aj citát z cudzieho programu (eurofondy pri účtovných kanceláriách) a dodávateľa podľa staršieho typu vydavateľa; Prehľad príležitosti už rozlišuje nezávislý dôkaz (H4: 0 z 23). Zjednotiť silu s posúdením relevantnosti a prepočítať verdikty testu – najprv porovnať, koľko hypotéz by zmenilo stav. · 1 deň.
- ~~**2026-10-26 – TED – kalibrácia prahu podobnosti.**~~ **Hotové 26. 10. 2026** (podobnosť nestačila – kandidátov posudzuje rýchly model; zákazka = postup; DECISIONS). Ostáva: po prvom plnom behu prezrieť posúdenia 10 hypotéz a zmerať presnosť (správne / nesprávne). `procurement.match_threshold` 0,5 je predbežný; po prvom behu akcie `procurement` (beží sama, CPU voľné) prezrieť zhody 10 hypotéz (správne / nesprávne) a nastaviť prah, príp. pridať kontrolu kľúčových slov predmetu. · 0,5 dňa.
- **2026-10-26 – TED – kupujúci ako organizácie hypotézy.** Opakovaní kupujúci podobných zákaziek sú kandidáti na zákazníka; tlačidlo „pridať kupujúceho k hypotéze“ (organizácia podľa IČO, bez automatického zberu ľudí). · 0,5 dňa.
- **2026-10-26 – obstarávanie pod limitmi EÚ.** TED nemá podlimitné zákazky; ÚVO vestník zakazuje sťahovanie (robots.txt), EKS overiť API alebo otvorené dáta, CRZ (`crz.py`) využiť aj ako signál uzavretých zmlúv k hypotéze. · 1–2 dni.
- ~~**2026-10-26 – audit (komerčné objavovanie) – M36: zdroj → obchodný výsledok (zatiaľ iba správa).**~~ **Hotové 28. 10. 2026** (`commercial_yield.py`). Objavovanie
  (Thompson sampling v `discover.py`) odmeňuje dokument s overeným tvrdením – výskumnú výťažnosť, nie obchodnú. Pre
  každú kategóriu a stratégiu (scout, matica dopytov, katalóg, TED, ručne) dva lieviky vedľa seba: výskumný
  (dokumenty → overené tvrdenia) a obchodný (hypotézy → prešli testom → Brána 0 → oslovení → odpovede → rozhovory →
  potvrdenie problému → stupne ochoty platiť z M35). Reťaz hypotéza ← tvrdenia ← dokumenty ← kategória sa dá spojiť
  z existujúcich tabuliek (bez novej schémy). GUI Zdroje + príkaz `commercial-yield`. **Bandit nemeniť**, kým nie je
  aspoň ~30 odoslaných oslovení v ≥ 3 kategóriách (predajné výsledky sú oneskorené, riedke a závisia od toho, koho
  oslovíš); pravidlo zapísať do DECISIONS. · 1 deň.
- ~~**2026-10-26 – audit – M38: ekonomika u konkrétneho zákazníka.**~~ **Hotové 28. 10. 2026** (`economics.py`; model sa nepoužíva). Tabuľka `buyer_economics` (organizácia × hypotéza,
  väzba na kartu organizácie): objem prípadov, minúty na prípad, cena práce, dosiahnuteľná úspora, náklad na zavedenie
  a kontrolu; každý faktor s pôvodom `observed` / `customer_reported` / `inferred` / `unknown` a zdrojom (citát,
  rozhovor, výsledok oslovenia). Rozpätie ročnej hodnoty, čistý prínos a návratnosť počíta Python (model iba
  štruktúru, ako `market.py`); neznámy faktor = otázka do rozhovoru. Úspora z webu dodávateľa sa nepoužije.
  Pravidlo: veľký trh nič neposúva – „pripravené na ponuku“ až s aspoň jedným nákladovým údajom `observed` alebo
  `customer_reported`. Doplniť aj otázky, či zákazník úsporu vie reálne využiť (nie iba „ušetrený čas“). · 2 dni.
- ~~**2026-10-26 – audit – M39: konkurencia ako dôvod zamietnutia.**~~ **Hotové 28. 10. 2026** (`alternatives.py`). Kritik dnes iba ukladá odkazy z vyhľadávania
  (`critic.py`). Pre hypotézy na Bráne 0 posúdenie alternatív – aj nesoftvérových (človek, Excel, externá služba,
  modul ERP, zrušenie procesu): pokrýva proces (áno / čiastočne / nie, s citátom), ako sa kupuje, bariéry zavedenia,
  ktorá časť ostáva nevyriešená (s dôkazom). Zdroje: odkazy kritika, „existujúce riešenia“ z kariet organizácií,
  výskumné úlohy. Nový stav `no_defensible_gap`: kód ho **navrhne**, keď každá alternatíva pokrýva proces a chýba
  dôkaz nevyriešenej časti; **potvrdzuje človek** (súhlas používateľa 26. 10. 2026); lievik a autopilot potom
  hypotézu vynechajú. · 2 dni.
- ~~**2026-10-26 – audit – M40: benchmark v reálnom svete.**~~ **Kód hotový 28. 10. 2026** (`benchmark.py`; ostávajú oslovenia). Kohorta 10 príležitostí zo systému (celý reťazec
  v Prehľade príležitosti) a kontrolná kohorta 10 problémov vybraných ručne z čerstvých oznámení o obstarávaní (TED,
  M37) bez agentov. Ciele: ≥ 20 oslovení a ≥ 5 rozhovorov (odosiela používateľ ručne; 26. 10. „neviem či stihnem 20,
  ale nejaké písať budem“), stavy z M35. Kód: označenie kohort a porovnávacia správa (obchodné stavy, čas na
  prípravu). Rozhodovacie pravidlo do DECISIONS: ak systém neprekoná ručnú kohortu v obchodných stavoch, ďalší
  míľnik o „lepšom uvažovaní“ (modely, agenti, Brána 2) sa nerobí. · 0,5 dňa kódu + oslovenia.
- **2026-10-26 – audit – zdroje zo súkromného sektora (overiť realizovateľnosť).** Katalóg zdrojov je silný
  vo verejnom sektore (audity, inšpekcie, zväzy) – problémy, ktoré sa dajú ľahko doložiť, nie nutne tie, za ktoré
  firmy platia. Inzeráty: iba oficiálne dáta alebo povolené API (ÚPSVaR / sluzbyzamestnanosti.gov.sk – overené sú iba
  agregáty voľných miest po okresoch, API s textami inzerátov nepotvrdené; EURES API overiť); recenzné portály
  (G2, Capterra) sťahovanie zakazujú – nie. Odborné diskusie iba s povolením. Najprv 0,5-dňová kontrola podmienok
  použitia, potom rozhodnutie. · 0,5 dňa + podľa výsledku.
- **2026-10-26 – audit – nezávislosť pozorovaní (preberané správy).** `cluster.py` už vyradí takmer rovnaký citát
  z iného dokumentu, ale nerozpozná prerozprávanie toho istého primárneho zdroja (tri články o jednej správe NKÚ =
  tri domény v `strength.py`). Zoskupiť dôkazy podľa primárneho zdroja (odkaz na tú istú správu, rovnaké číslo +
  inštitúcia + rok) a rátať nezávislé pozorovania, nie domény; nadväzuje na „sila dôkazov podľa relevantnosti“
  (21. 10.). · 1–2 dni.
- **2026-10-26 – audit – zmerať presnosť komponentov na reálnych behoch.** Pred ďalším uvažovaním modelov overiť,
  ako presne fungujú existujúce kroky: ručne označiť ~50 prípadov na úlohu (Kontrola → gold labely; zatiaľ 0),
  presnosť kariet organizácií a kontaktov (správna organizácia, správna rola, kontakt platný) na vzorke 20. · 1 deň
  + označovanie.

## 2026-10-29 – požiadavky používateľa

- **2026-10-29 – GUI – história mojej práce v aplikácii a pripomenutie nevyužitých funkcií.** Lokálne zapisovať, čo
  používateľ v aplikácii reálne robí (navštívené stránky a karty, tlačidlá, formuláre, príkazy CLI – iba názvy akcií
  a čas, žiadne údaje kontaktov). Porovnať so zoznamom funkcií podľa míľnikov (M35–M43 …) a ukázať „čo je nové a ešte si
  to neskúsil“ (napr. v Prehľade) – a aby asistent v Claude Code vedel z histórie upozorniť na zabudnutú funkciu.
  Iba lokálne v DB, bez telemetrie (Streamlit `gatherUsageStats` ostáva vypnuté), s možnosťou vymazania. · 1 deň.
- **2026-10-29 – Správca akcií – tlačidlo „Spustiť kroky pre existujúce hypotézy na X hodín“.** Ako „Spustiť všetko
  hneď“ (zvolený čas), ale bez hľadania nových hypotéz: autopilot bez akcií objavovania (scout, topics, catalog)
  a lievik bez kroku „find“; beží test, kontakty, Brány 1 a 2, karty, kritika, alternatívy, návrhy, pošta a porovnanie
  s TED pre existujúce hypotézy. Rozhodnúť: akcia `evidence` (zaradenie tvrdení) z nezaradených tvrdení zakladá aj
  nových kandidátov – v tomto režime iba zaraďovať k existujúcim. · 0,5 dňa.

## 2026-10-29 – externá recenzia (iné AI) po M35–M43

Hodnotenie: väčšina medzier z auditu 26. 10. je riešená (M35–M43); zostávajú správnosť, spoľahlivosť a reálny
obchodný výsledok. Nasledujúce body sú overené v kóde 29. 10. 2026, ak nie je uvedené inak.

- **P0 – obstarávanie: výzva bez lehoty nie je otvorená.** `procurement.open_days_without_deadline` (60) berie výzvu
  bez lehoty v dátach ako otvorenú 60 dní od zverejnenia – overené. Taký stav má byť „neoverené“, nie otvorené ani
  potvrdené, kým sa neoverí živý stav zákazky. Regresný test. · 0,5 dňa.
- **P0 – ochota platiť: posledný platný stav, nie iba najvyšší.** `commercial._top` berie najvyšší kladný stupeň –
  overené; neskoršie „nezaplatia“ alebo odmietnutie ponuky staršiu ochotu nezruší, firma ďalej vyzerá ako aktívny
  kupujúci. Viesť zvlášť historické maximum, posledný účinný stav (neskorší negatívny prebije), rozpory a stiahnuté
  dôkazy; GUI, Prehľad, M36 a M40 počítať z posledného stavu. · 1 deň.
- **P0 – pošta: párovanie s prahom istoty.** Bez vlákna sa odoslaná správa páruje podľa adresy a najpodobnejšieho
  predmetu bez minimálnej podobnosti (`mail._pick`) a odpoveď bez vlákna padá na posledné odoslané oslovenie na adresu –
  overené. Pridať prah (napr. token_set_ratio ≥ 80) a nejasné prípady dať na ručné potvrdenie. · 0,5 dňa.
- **P0 – ekonomika: dve úrovne pripravenosti.** `pripravené_na_ponuku` stačí jeden nákladový údaj zákazníka, aj keď
  ostatné faktory sú odhad (zámer M38). Rozlíšiť „pripravené na rozhovor o pilote“ a „pripravené na ekonomické tvrdenie
  alebo cenu“ (napr. objem, čas aj cena práce pozorované alebo od zákazníka). · 0,5 dňa.
- **P0 – alternatívy: „bez medzery“ vyžaduje dôkaz od zákazníka.** Návrh `no_defensible_gap` dnes stačí, že nájdené
  alternatívy podľa citátov pokrývajú proces a medzera nie je doložená; inzerovaná funkcia nedokazuje, že zákazník je
  dobre obslúžený. Vyžadovať aspoň jeden dôkaz zo strany zákazníka (výsledok oslovenia, rozhovor, recenzia používateľa).
  · 0,5 dňa.
- **P0 – sila dôkazov skôr ako ďalšie poradie.** Nadväzuje na „sila dôkazov podľa relevantnosti“ (21. 10.)
  a „nezávislosť pozorovaní“ (26. 10.): jednotka dôkazu = nezávislé pôvodné pozorovanie relevantné k cieľovému procesu,
  nie overený citát z inej domény (tri články o jednej správe NKÚ = 1). Ovplyvňuje, ktoré hypotézy idú do lievika.
- **P0 – míľnik podľa výsledku, nie funkcií.** Ďalší míľnik: 3 reálne organizácie s doloženým dnešným postupom,
  aspoň 1 overený ekonomický odhad (M38) a 1 vážny rozhovor o platenom pilote. Námietky a zlyhania potom určia, čo
  softvér naozaj potrebuje.
- **P1 – benchmark M40 iba smerový.** Verdikt po 20 osloveniach a 5 rozhovoroch je diagnostika, nie dôkaz prevahy;
  kohorty sa líšia výberom problémov. Párovať príležitosti z rovnakého zdroja a odvetvia a ukazovať mieru konverzie
  s intervalom (Wilson, už je v `evals.py`), nie iba počty. · 0,5 dňa.
- **P1 – referenčné sady a CI.** Ručne označené sady z reálnych behov: relevantnosť zákaziek TED, správna organizácia,
  rola kontaktu (správny rozhodovateľ), relevantnosť tvrdení, odlíšenie nenaplnenej potreby od inzerovanej funkcie,
  párovanie pošty na reálnych vláknach; presnosť a úplnosť pred väčšími modelmi (nadväzuje na „zmerať presnosť
  komponentov“ 26. 10.). GitHub Actions s offline pytest – v repozitári žiadny workflow nie je (overené). · 1 deň
  + označovanie.
- **P2 – nová stratégia objavovania: povolania a úlohy (Cedefop + ESCO).** Import skóre nedostatku pracovníkov
  (Cedefop), rozklad povolania na úlohy s podielom času (ESCO je už lokálne), technická automatizovateľnosť na úrovni
  úlohy, prepojenie s firmami s tlakom na nábor (inzeráty iba z povolených zdrojov) → hypotéza o úspore a nákupe, napr.
  „technik údržby → hľadanie náhradných dielov a dokumentácia zákaziek → ušetrené hodiny → konkrétne oddelenia údržby
  na rozhovor“. Nepredpovedať zánik povolaní. Samostatná stratégia do kariet organizácií a Brány 0; zmierni zaujatosť
  zdrojov k verejnému sektoru. · 2–3 dni.
- **Zatiaľ nerobiť** (odporúčanie recenzie, súhlasí s pravidlom M40): ďalšiu multiagentovú vrstvu, vážené „skóre
  úspechu startupu“, zložitejšie ML experimenty. Úzke hrdlo sú rozhovory so zákazníkmi a merané výsledky.

## 2026-10-29 – modernejšia AI (recenzia, druhá časť)

- **Selektívny silný cloudový model pre ťažké úsudky a výskum.** Lokálne Qwen ostávajú na hromadný zber a extrakciu;
  silný model iba pre zložité, nejednoznačné prípady (rozporné zdroje, karta organizácie, alternatívy, výskum medzier).
  Nemeniť globálne, kým označená sada neukáže zlepšenie. Voľba modelu nižšie.
- **Rozhodnutie 29. 10. 2026: Claude Sonnet 5.5 (pred GPT-6.1 Sol).** Obe stoja 2 $ / 10 $ za milión vstupných /
  výstupných tokenov a majú okno ~1 M tokenov. Sonnet 5.5 vedie v nezávislých agentových a výskumných meraniach
  (Vals AI: Vals Index 70,9 vs 63,9 %, Legal Research Bench 48,1 vs 38,5 %, Finance Agent v2.1 67,3 vs 61,8 %;
  Artificial Analysis Intelligence Index 56 vs 52; Terminal-Bench 4.0 64 vs 55 % podľa Artificial Analysis), je
  rýchlejší (86 vs 29 tok/s v jednom meraní) a má publikované viacjazyčné výsledky (Global-MMLU 92,1 %). GPT-6.1 Sol
  vedie vo faktickej pamäti bez zdrojov (SimpleQA Verified 73,9 vs 46,5 %), na SRE Bench a DeepSWE a má lacnejšie
  čítanie z cache (0,10 vs 0,20 $). Pre tento projekt rozhoduje práca nad stiahnutými zdrojmi s overením citátov
  kódom, agent s nástrojmi a slovenčina – tam vedie Sonnet; faktická pamäť modelu je menej dôležitá. Výhrady: väčšina
  čísel je z agregátorov a správ výrobcov (nezhody medzi zdrojmi); rozhodnutie navrhol asistent bežiaci na modeli
  Claude – preto ho potvrdí až porovnanie A/B/C/D na vlastných prípadoch (Sol ako druhý kandidát). Integrácia: Sonnet
  cez Anthropic API alebo Microsoft Foundry (Azure) – v `cloud.py` druhý protokol (Messages API cez `httpx`, bez SDK),
  rozpočet, `cloud.allowed_tasks` a zákaz osobných údajov v cloude platia ďalej. API kľúče a nastavenia vyžiadať až
  pri implementácii. Zdroje: gradually.ai (Vals AI), llm-stats.com, fullstack.com, datacamp.com. · 1–2 dni.
- **`cloud.py`: Responses API pre volanie nástrojov.** Nástroje modelov GPT-6.x idú cez Responses API; dnešný klient
  používa Chat Completions – pri GPT stačí na štruktúrovaný JSON, nie na agenta s nástrojmi. · 0,5 dňa.
- **Hybridné hľadanie v zozbieraných dokumentoch + reranker.** Dnes sémantika iba páruje tvrdenia, hypotézy a zákazky.
  Lokálne SQLite FTS5 (BM25) nad všetkými dokumentmi + existujúce Qwen embeddingy + reranker pasáží; vyhodnotiť na ručne
  označených dopytoch. Potom porovnať SearXNG s platenou search API – zmerať pokrytie slovenských úradných webov, nie
  predpokladať. · 2 dni.
- **Viacmodálne dokumenty (tabuľky, skeny).** `fetch.py` berie text PDF cez pypdf. Docling (lokálne: rozloženie,
  štruktúra tabuliek, poradie čítania, OCR) iba pri dôležitých tabuľkách, slabej extrakcii alebo skenoch; pri
  kľúčových stranách model s videním nad obrázkom strany. Hodnoty vždy overiť voči originálu (OCR sa mýli). Nová
  závislosť → odôvodnenie v DECISIONS. · 1–2 dni.
- **Adaptívna hĺbka výskumu.** Úsilie podľa očakávanej hodnoty a neistoty: jednoduchá extrakcia → lokálny rýchly
  model; nezhoda lokálnych modelov → silnejší hodnotiteľ; chýba kľúčový fakt → cielené dohľadanie; rozpor zdrojov →
  nezávislé posúdenie s pôvodom; sľubná príležitosť s nevyriešenou rozhodujúcou otázkou → silný výskumný agent; silný
  protidôkaz alebo žiadny platiteľ → stop a šetriť rozpočet. · 1–2 dni.
- **Výskumný agent s plánom.** Namiesto opakovaného širokého hľadania najprv najdôležitejšia neznáma (napr. „majú
  firmy plánovanie už dobre pokryté v ERP/MES?“), cielené hľadanie nasadení a sťažností, revízia plánu podľa dôkazov;
  stavať na cielenom výskume medzier v kartách organizácií. Merať počet nových nezávisle overených obchodne relevantných
  faktov na beh, nie dĺžku správy. · 2 dni.
- **Porovnanie A/B/C/D na 30–50 reálnych prípadoch pred plošnou zmenou.** A dnešný systém, B silný model pri zložitých
  rozhodnutiach, C lepšie hľadanie s dnešnými modelmi, D oboje. Merať úplnosť platných zdrojov, podiel nepodložených
  tvrdení, nájdených platiteľov, vyriešené obchodne dôležité neznáme, čas a cenu. Ak B > A, pomáha uvažovanie; ak C > A,
  úzkym hrdlom je hľadanie; ak D neprinesie viac, neplatiť za oboje. · 1 deň + príprava prípadov.
- **Nižšia priorita:** agent ovládajúci prehliadač iba na povolených weboch s JavaScriptom, kde API ani stiahnutie
  nestačí (stredná); MCP integrácie (nízka až stredná); znalostný graf organizácia – zákazka – dodávateľ – problém –
  produkt (stredná); doladenie lokálnych modelov až po stovkách označených chýb (nízka); multiagentové frameworky –
  orchestrácia už je v kóde (nízka).
