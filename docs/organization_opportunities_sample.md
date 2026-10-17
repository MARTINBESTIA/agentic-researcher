# Ukážka: tri organizácie – všeobecná ponuka vs. karta organizácie (pseudonymizované)

Pôvod: zmrazená kópia lokálnej databázy zo 16. 10. 2026 (reálne uložené verejné podklady: register, citáty z webu,
zistenie NKÚ, referencia dodávateľa), spracovaná 17. 10. 2026 príkazom
`python -m radar org-cards --run --target 4:4 --target 100:4 --target 33:11` a porovnaná cez `--compare`.
Karty zostavil a skontroloval model **gpt-6-luna** (Azure; v tejto kópii bola presná rola nasmerovaná do cloudu –
v hlavnej konfigurácii ide `org_card` na lokálny 27B). Kontroly kódom sú deterministické. **Žiadna z organizácií nebola
oslovená a nie sú tu žiadne odpovede ani výsledky nasadenia** – všetko sú návrhy na kontrolu človekom. Názvy organizácií, produktov a odkazy sú
nahradené; organizácia A = účtovná kancelária so zhodou segmentu bez potvrdenej bolesti, B = kancelária uvedená
v referencii dodávateľa softvéru (používa riešenie), C = štátna organizácia so zistením kontroly k projektu.
Cielený výskum medzier nebežal (SearXNG nebol spustený) – medzery sú preto „otvorené“.

Vygenerované 2026-10-17T19:14 UTC. Starý formát je rekonštrukcia `build_package` pred v15 (prvý všeobecný návrh hypotézy pre všetkých adresátov roly). Nový je aktuálna karta organizácie. Výsledky modelov sú návrhy na kontrolu človekom, nie overenie u zákazníka. Názvy organizácií sú pseudonymizované.

## Organizácia A – H4 (zákazník (používateľ alebo vlastník procesu); SK NACE 6920, sektor private)

### Pred v15 (všeobecná ponuka)

- potreba: nevieme – prvým krokom je otázka na skúsenosť
- výstup: –
- minimálny vstup: –
- hlavná otázka: Spomeňte si na posledný konkrétny mesiac: ktorý doklad alebo klient si vyžiadal najviac ručného prepisovania a koľko času to približne zabralo?
- poznámka: žiadny použiteľný všeobecný návrh (1 v stave na úpravu) – rovnaký generický text pre všetkých

### v15 (karta organizácie)

- verzia 3, stav na kontrolu, model gpt-6-luna, poskytovateľ azure
- komu: Organizácia A – zákazník (používateľ alebo vlastník procesu); konať môže: Osoba, ktorá môže schváliť zmenu pracovného postupu alebo prístup k dokladom, nie je známa.
- úkon: Jeden doklad, napríklad faktúra alebo bloček; konkrétny typ vhodný na overenie nie je známy. · Prijatie dokladu od klienta môže spustiť jeho spracovanie v kancelárii; postup Organizácia A nie je potvrdený.
- zmena: Možná zmena sa obmedzuje na spôsob spracovania vybraného typu dokladu; najprv treba potvrdiť súčasný postup.
- minimálny vstup: Prejsť jeden nedávny doklad a, ak je to možné, ukázať anonymizovaný vstup aj zaevidovaný výsledok.
- overenie: Aktívny čas spracovania a počet opráv alebo nesprávnych polí na doklade. · Ten istý doklad porovnať so súčasným postupom kancelárie; automatizovaný výsledok vždy skontroluje človek. · stop: Zastaviť, ak chýba súhlas, nemožno bezpečne anonymizovať vstup alebo výsledok obsahuje neopravenú kritickú chybu.
- ponuka: Krátke vyhodnotenie jedného schváleného prípadu: kroky, čas, opravy a vhodnosť obmedzenej skúšky.
- hlavná otázka: Pri poslednom doklade, ktorý ste spracovali, čo sa dialo od prijatia po zaevidovanie a bolo treba údaje prepisovať ručne?
- pripravenosť: rozhovor áno; skúška áno; tvrdenie o prínose nie
- ďalší krok: dohodnúť prvý krok: Prejsť jeden nedávny doklad a, ak je to možné, ukázať anonymizovaný vstup aj zaevidovaný výsledok.

| položka | stav |
|---|---|
| prečo tento adresát | jasná hypotéza |
| konkrétny úkon | jasná hypotéza |
| dnešný postup | jasná hypotéza |
| prekážka | chýba |
| právomoc zasiahnuť | jasná hypotéza |
| existujúce riešenie / medzera | chýba |
| kandidátny zásah | jasná hypotéza |
| minimálny vstup | jasná hypotéza |
| metrika | jasná hypotéza |
| neistota | jasné s podkladom |

Kontroly: 0 výhrad kódu, 1 zoslabených výrokov; hodnotiteľ opora ok, použiteľnosť problems.

| medzera | zmení | zdroj | stav |
|---|---|---|---|
| Čím sa zaoberá Organizácia A v oblasti „prepis uctovnych dokladov“ (agenda, služby, kompetencie)? | adresát | verejný dokument | otvorené |
| Ktorý útvar alebo funkcia v Organizácia A zodpovedá za „prepis uctovnych dokladov“? | adresát | verejný dokument | otvorené |
| Aký systém alebo nástroj používa Organizácia A pri „prepis uctovnych dokladov“ (referencie dodávateľov, vlastné zverejnenia)? | výber riešenia | verejný dokument | otvorené |
| Ako prebehol v Organizácia A posledný konkrétny prípad „prepis uctovnych dokladov“ (kroky, kto, nástroje)? | návrh skúšky | kontakt (rozhovor) | otvorené |
| Nastal v poslednom prípade v Organizácia A jav, ktorý predpokladáme (ručná práca, čakanie, opravy)? | návrh skúšky | kontakt (rozhovor) | otvorené |
| Má Organizácia A podklady pre skúšku: Jeden anonymizovaný doklad a jeho zaevidovaný výsledok; rozsah je návrh do potvrdenia človekom.? | návrh skúšky | vzorka dokladov alebo dát | otvorené |

## Organizácia B – H4 (zákazník (používateľ alebo vlastník procesu); SK NACE -, sektor neuvedený)

### Pred v15 (všeobecná ponuka)

- potreba: nevieme – prvým krokom je otázka na skúsenosť
- výstup: –
- minimálny vstup: –
- hlavná otázka: Spomeňte si na posledný konkrétny mesiac: ktorý doklad alebo klient si vyžiadal najviac ručného prepisovania a koľko času to približne zabralo?
- poznámka: žiadny použiteľný všeobecný návrh (1 v stave na úpravu) – rovnaký generický text pre všetkých

### v15 (karta organizácie)

- verzia 3, stav na kontrolu, model gpt-6-luna, poskytovateľ azure
- komu: Organizácia B – zákazník (používateľ alebo vlastník procesu); konať môže: Nie je známe, kto v Organizácia B môže meniť tento pracovný postup alebo schvaľovať skúšku.
- úkon: Možnou jednotkou je jeden doklad; rozsah práce v Organizácia B nie je potvrdený. · Referencia opisuje prepisovanie dokladov pred používaním [produkt dodávateľa]; dnešný spúšťač tejto práce v Organizácia B nepoznáme.
- zmena: Po potvrdení postupu možno skúsiť zaznamenať čas práce na doklade a jeho opravy bez zmeny účtovných pravidiel.
- minimálny vstup: Prejsť s účtovníkom jeden nedávny, anonymizovaný prípad a zistiť, či prepisovanie skutočne nastalo.
- overenie: Čas spracovania na doklad a počet opráv zistených pri kontrole. · Porovnať zaznamenaný postup s dnešným spracovaním rovnakého typu dokladu. · stop: Zastaviť, ak Organizácia B nepotvrdí problém, neposkytne schválenú vzorku alebo skúška naruší bežné spracovanie.
- ponuka: Krátky rozbor jedného schváleného prípadu: kroky, čas, opravy a návrh bezpečnej skúšky.
- hlavná otázka: Stal sa v Organizácia B nedávno prípad, keď účtovník ručne prepisoval údaje z dokladu? Ak áno, čo sa vtedy dialo?
- pripravenosť: rozhovor nie; skúška áno; tvrdenie o prínose nie
- ďalší krok: doplň: overený kanál (kontakt alebo overený web organizácie)

| položka | stav |
|---|---|
| prečo tento adresát | jasné s podkladom |
| konkrétny úkon | jasné s podkladom |
| dnešný postup | jasné s podkladom |
| prekážka | jasná hypotéza |
| právomoc zasiahnuť | jasná hypotéza |
| existujúce riešenie / medzera | jasné s podkladom |
| kandidátny zásah | jasná hypotéza |
| minimálny vstup | jasná hypotéza |
| metrika | jasná hypotéza |
| neistota | jasné s podkladom |

Kontroly: 4 výhrad kódu, 2 zoslabených výrokov; hodnotiteľ opora problems, použiteľnosť problems.

| medzera | zmení | zdroj | stav |
|---|---|---|---|
| Ktorý útvar alebo funkcia v Organizácia B zodpovedá za „prepis dokladov“? | adresát | verejný dokument | otvorené |
| Zistiť: Nie je známe, či Organizácia B dnes stále používa [produkt dodávateľa], ani či má nevyriešený problém s prepisovaním dokladov? | adresát | verejný dokument | otvorené |
| Zistiť: Aktuálny stav nasadenia ani používané funkcie [produkt dodávateľa] v Organizácia B nie sú známe; Nie je potvrdené, či Organizácia B má dnes medzeru v spracovaní dokladov? | výber riešenia | verejný dokument | otvorené |
| Zistiť: Nie je známe, ako Organizácia B rieši nečitateľné, neúplné alebo neštandardné doklady; Zistiť, ako doklad dorazí k účtovníkovi; konkrétny postup Organizácia B nepoznáme? | návrh skúšky | verejný dokument | otvorené |
| Nastal v poslednom prípade v Organizácia B jav, ktorý predpokladáme (ručná práca, čakanie, opravy)? | návrh skúšky | kontakt (rozhovor) | otvorené |
| Má Organizácia B podklady pre skúšku: Jeden alebo niekoľko anonymizovaných dokladov, vybraných a schválených Organizácia B.? | návrh skúšky | vzorka dokladov alebo dát | otvorené |

## Organizácia C – H11 (zákazník (používateľ alebo vlastník procesu); SK NACE -, sektor neuvedený)

### Pred v15 (všeobecná ponuka)

- potreba: nevieme – prvým krokom je otázka na skúsenosť
- výstup: –
- minimálny vstup: –
- hlavná otázka: –
- poznámka: žiadny použiteľný všeobecný návrh (0 v stave na úpravu) – rovnaký generický text pre všetkých

### v15 (karta organizácie)

- verzia 2, stav na kontrolu, model gpt-6-luna, poskytovateľ azure
- komu: Organizácia C – zákazník (používateľ alebo vlastník procesu); konať môže: Konkrétny vlastník procesu ani jeho právomoci nie sú známe.
- úkon: Jeden projekt [názov] [názov] [názov] s financovanými aktivitami a zmluvnými záväzkami. · Historická kontrola opísala projekt, pri ktorom neboli splnené zmluvné záväzky.
- zmena: Navrhnúť jednoduché sledovanie záväzkov, termínov, platieb a eskalácií pre jeden projekt.
- minimálny vstup: Zdieľať bezpečne anonymizované podklady k jednému historickému projektu na overenie sledovania záväzkov.
- overenie: Podiel záväzkov s doloženým vlastníkom, termínom a stavom; počet nejasností oproti pôvodným záznamom. · Porovnať návrh s pôvodnými záznamami a postupom použitým pri danom projekte. · stop: Zastaviť, ak chýba súhlas vlastníka údajov, bezpečný spôsob zdieľania alebo vhodný projekt.
- ponuka: Krátka analýza jedného anonymizovaného projektu a návrh kontrolného prehľadu záväzkov.
- hlavná otázka: Pri poslednom projekte s financovanými aktivitami nastal prípad nesplneného záväzku, nepodanej žiadosti o platbu alebo oneskoreného informovania poskytovateľa?
- pripravenosť: rozhovor áno; skúška áno; tvrdenie o prínose nie
- ďalší krok: dohodnúť prvý krok: Zdieľať bezpečne anonymizované podklady k jednému historickému projektu na overenie sledovania záväzkov.

| položka | stav |
|---|---|
| prečo tento adresát | jasné s podkladom |
| konkrétny úkon | jasné s podkladom |
| dnešný postup | jasné s podkladom |
| prekážka | jasné s podkladom |
| právomoc zasiahnuť | jasná hypotéza |
| existujúce riešenie / medzera | chýba |
| kandidátny zásah | jasná hypotéza |
| minimálny vstup | jasná hypotéza |
| metrika | jasná hypotéza |
| neistota | jasné s podkladom |

Kontroly: 0 výhrad kódu, 0 zoslabených výrokov; hodnotiteľ opora ok, použiteľnosť problems.

| medzera | zmení | zdroj | stav |
|---|---|---|---|
| Ktorý útvar alebo funkcia v Organizácia C zodpovedá za „projektove zavazky a platby“? | adresát | verejný dokument | otvorené |
| Zistiť: Nevieme, či sa podobné ťažkosti opakujú, kto dnes sleduje záväzky ani aké nástroje organizácia používa? | adresát | verejný dokument | otvorené |
| Aký systém alebo nástroj používa Organizácia C pri „projektove zavazky a platby“ (referencie dodávateľov, vlastné zverejnenia)? | výber riešenia | verejný dokument | otvorené |
| Zistiť: Zodpovedný útvar, projektový pracovník a schvaľovateľ nie sú zo zdrojov známi; Zmluvné termíny, plánované aktivity, doklady o výdavkoch a komunikácia s poskytovateľom; ich dnešná dostupnosť nie je známa? | návrh skúšky | verejný dokument | otvorené |
| Má Organizácia C podklady pre skúšku: Jeden dokončený alebo problémový projekt: anonymizované záväzky, termíny, stav aktivít, platobné podklady a relevantná k? | návrh skúšky | event log | otvorené |

