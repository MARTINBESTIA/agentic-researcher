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
