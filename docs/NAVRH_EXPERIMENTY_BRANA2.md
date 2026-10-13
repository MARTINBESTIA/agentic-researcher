# Návrh: experimenty Brány 2 naviazané na problém (príklad H11)

Stav: návrh na rozhodnutie (13. 10. 2026). Čísla v časti 1 sú overené dotazmi na dáta H11 v tento deň.

## 0. Zhrnutie

- Súčasný experiment (predpoveď mesačnej sumy zmlúv organizácie) **nesúvisí s problémom H11** a ani lepší model
  by to nezmenil: mesačnú sumu určuje pár veľkých zmlúv (Bratislava, september: 1 zmluva = 85 % sumy).
- Úzke miesto nie je algoritmus, ale (1) chýba **rozhodnutie**, ktoré má výsledok podporiť, (2) chýbajú
  **označené prípady** (čo je „problémová“ zmluva), (3) rady majú 8 mesiacov. Pokročilé modely na 8 bodoch iba preučia.
- Lokálne modely sa hodia na **formulovanie problému, výber typu experimentu z ponuky, triedenie textov zmlúv
  a písanie záveru z vypočítaných faktov**. Modelovanie a čísla má robiť Python (scikit-learn, statsmodels, LightGBM
  na CPU), nie model.
- Navrhujem pipeline s problémovou kartou na začiatku, knižnicou typov experimentov, výsledkovou tabuľkou
  (všetky naivné odhady × všetky metriky, jedna hlavná predregistrovaná) a záverom „čo to znamená pre problém“.

## 1. Doménové minimum (pre informatika)

- **CRZ** (Centrálny register zmlúv): každá povinne zverejňovaná zmluva štátu a samospráv; účinnosť nastane až po
  zverejnení. Dáta: objednávateľ (IČO), dodávateľ (IČO), predmet (voľný text), dátumy, suma, odkaz na ÚVO.
- **Dodatok**: zmena zmluvy (cena, termín, rozsah). Dodatok zvyšujúci cenu krátko po podpise je klasický
  varovný signál – súťaž sa vyhrala nízkou cenou, cena sa potom „dorovnala“.
- **Varovné signály (red flags)** pri obstarávaní: jediný uchádzač, zmluvy tesne pod limitom zákazky (aby sa
  nemusela vypísať súťaž), delenie zákazky na viac menších, nový dodávateľ bez histórie, neskoré zverejnenie,
  koncentrácia zákaziek u jedného dodávateľa. V literatúre sa skladajú do indexu rizika (napr. Fazekas a kol. 2016,
  *Corruption Risk Index*). Signál nie je dôkaz korupcie – je to **poradie, čo skontrolovať skôr**.
- **Kto má problém a rozhoduje**: hlavný kontrolór obce či mesta, interný audit ministerstva alebo kraja, NKÚ,
  Útvar hodnoty za peniaze (ÚHP), watchdog organizácie a novinári. Spoločné rozhodnutie: *ktoré zmluvy a obstarávania
  preveriť s obmedzenou kapacitou*.

## 2. Diagnóza súčasného stavu (overené 13. 10. 2026)

| Problém | Fakt z dát | Dôsledok |
|---|---|---|
| Hypotéza nie je jeden problém | H11 „Neprehľadnosť financií a korupčné riziká v štátnych inštitúciách“ (stav `new`) má 20 dôkazov o rôznych veciach: neoveriteľné náklady IT projektov (ÚHP), konflikt záujmov pri štátnom IT obstarávaní, neprehľadné dopady kategorizácie liekov, všeobecné vety o korupcii, aj protidôkaz (zlepšenie v CPI). | Model nemá z čoho odvodiť, aké rozhodnutie má experiment podporiť → zvolil generickú predpoveď sumy. |
| Cieľ nemá vzťah k problému | Bratislava, september 2026: 166 zmlúv, najväčšia (modernizácia električkovej trate, 119 mil. €) = 85 % sumy mesiaca. 215 006 z 514 122 zmlúv (42 %) má nulovú sumu. | Suma za mesiac je súčet niekoľkých veľkých, ojedinelých zmlúv. Jej predpoveď nehovorí nič o riziku ani o prehľadnosti. |
| Krátke rady, jedna organizácia | Okno CRZ 15. 1. – 11. 10. 2026 → 8 úplných mesiacov, 3 kolá testu. E1 +1,6 % (interval −4,3 až 2,9), E2 −1,2 %. | Žiadny model sa z 8 bodov nič nenaučí; výsledok je šum. V okne je pritom 26 304 rôznych objednávateľov – dáta na *spoločný* model sú. |
| Jedna metrika, jeden naivný odhad | MAPE pri nulových a ojedinelých hodnotách skáče (E2 kolo 3: obe chyby ~69 %). | Nevidno, či výsledok drží aj pri inej metrike alebo silnejšom naivnom odhade. |
| Dodatky nie sú prepojené | Typ 2 má 30 370 záznamov (65 % z nich má „dodatok“ v predmete alebo čísle), no „dodatok“ je aj v 43 376 záznamoch typu 1; `suma_spolu` > `suma_zmluva` iba v 271 prípadoch. Odkaz na ÚVO má 16 266 zmlúv (3 %). | Najsilnejší dostupný signál (navýšenie ceny dodatkom) treba najprv vyskladať z textu. |
| Zverejnenie | Medián oneskorenia zverejnenia 3 dni, 90. percentil 30 dní, 99. percentil 127 dní (zmluvy podpísané od 15. 1.). | Merateľný signál netransparentnosti priamo z CRZ. |

## 3. Čo dokáže lokálny model (merané v tomto projekte)

Hardvér: RTX 5070 Laptop 8 GB VRAM, 32 GB RAM. Naraz jeden veľký model.

| Úloha v pipeline | Vhodné? | Model | Dôkaz / odhad |
|---|---|---|---|
| Problémová karta z hypotézy a citátov (kto, aké rozhodnutie, aký výstup pomôže) | áno, s kontrolou | 27B (presnosť) + sudca, potom človek | Generovanie ~4,5 tok/s → karta (~600 tokenov) ~2–3 min. Na desiatky hypotéz za noc stačí. |
| Výber typu experimentu z ponuky a parametrov (JSON schéma) | áno | 27B | Predregistrácia 50–80 s. **Bez mantinelov chybuje**: pri 9-mesačnom okne zvolil roky (0 období) – kód to musel opraviť. |
| Triedenie predmetu zmluvy (kategória, „je dodatok“, „číslo pôvodnej zmluvy“) | áno, v dávkach | 9B + regulárne výrazy najprv | ~54 tok/s; odhad ~40 predmetov na volanie ≈ 8 s → 100 tis. zmlúv ≈ 6 h (noc). Presnosť overiť na vzorke 100 ručne. |
| Posúdenie, či citát podporuje tvrdenie | áno | 27B (sudca) | 9B sa zhodol s 27B iba v 67 %, v 28 prípadoch bol benevolentnejší → nie ako sudca. |
| Návrh nových príznakov ako SQL výrazy (iba SELECT, kontrola kódom) | čiastočne | 27B / 35B MoE | Bezpečné vďaka `check_sql`; prínos meria kód (ablácia). |
| Písanie a spúšťanie vlastného kódu na modelovanie | **nie** | – | Pomalé (27B: skript 1 000 tokenov ≈ 4 min na pokus), chyby typu únik budúcich dát sa automaticky ťažko odhalia, a spúšťanie kódu od modelu porušuje invariant „agent nemá nástroj, ktorý niečo mení“. |
| Výpočty, štatistika, rozhodnutie o úspechu | **nie** | – | Invariant 2: čísla počíta Python. |
| Záver „čo to znamená pre problém“ iba z faktov | áno, s kontrolou čísel | 27B + kód (čísla mimo faktov → text sa zahodí) + sudca | Rovnaký mechanizmus ako dnešné demo. |

**Záver:** lokálny model zvládne *plánovanie v mantineloch, triedenie textu a vysvetľovanie*. Pokročilé modelovanie
zvládne *notebook (CPU)* – gradient boosting na 500 tis. riadkoch a desiatkach príznakov trvá sekundy až minúty.
Na hlboké učenie nie je dôvod: dát je málo, GPU obsadzuje jazykový model.

## 4. Navrhovaná pipeline

```
hypotéza + citáty
   │ 1. Problémová karta (27B → sudca → človek potvrdí)
   ▼
problém, kto rozhoduje, rozhodnutie, výstup, obchodná metrika
   │ 2. Výber typu experimentu z knižnice (27B navrhne, kód overí uskutočniteľnosť)
   ▼
typ + parametre + zdôvodnenie väzby na kartu
   │ 3. Dáta (Brána 1 + doplnky: dlhšia história, RÚZ/RPO, štítky)      ← kód
   │ 4. Predspracovanie s protokolom (čistenie, prepojenie dodatkov,     ← kód + 9B na text
   │    kategórie predmetu, okná, ochrana pred únikom budúcich dát)
   │ 5. Príznaky (k dátumu rozhodnutia, nie neskôr)                      ← kód (+ návrhy SQL od 27B)
   │ 6. Predregistrácia: hlavná metrika, hlavný naivný odhad, prah,      ← 27B návrh, človek uzamkne
   │    rozdelenie podľa času
   │ 7. Modely + výsledková tabuľka (všetky naivné odhady × metriky)     ← Python
   │ 8. Záver pre problém (z faktov a karty)                            ← 27B, kontrola čísel kódom, sudca
   ▼
postup krok po kroku (už existuje) + rozhodnutie Brány 2 (človek)
```

### 4.1 Problémová karta (nový krok, pred predregistráciou)

Polia (každé tvrdenie s odkazom na citát `C<id>`, sudca overí podporu ako pri `entail`):

- **Problém jednou vetou** (nie názov hypotézy), **kto ho má** (rola, typ organizácie), **aké rozhodnutie robí**,
  **ako to robí dnes** (bez nástroja: napr. náhodný výber, najväčšie zmluvy, podnet), **cena chyby**.
- **Výstup, ktorý by pomohol**: poradie (čo skontrolovať), upozornenie (neobvyklá zmluva), predpoveď (plánovanie),
  porovnanie (cena oproti podobným).
- **Obchodná metrika**: napr. „koľko z 50 skontrolovaných zmlúv má problém“ oproti dnešnému postupu.
- **Čo by hypotézu vyvrátilo**.

Široká hypotéza (H11) dostane návrh **rozdelenia** na užšie podhypotézy – rozhodne človek.

### 4.2 Knižnica typov experimentov (kód, nie voľný kód od modelu)

| Typ | Otázka | Cieľ (štítok) | Hlavná metrika | Silné naivné odhady |
|---|---|---|---|---|
| A. Poradie rizika | Ktoré zmluvy preveriť skôr? | budúca udalosť: dodatok s navýšením ceny do 12 mesiacov; neskôr nálezy NKÚ/ÚVO | presnosť v prvých k (precision@k), AUC-PR | náhodný výber, „najväčšie najprv“, počet varovných signálov (pravidlo) |
| B. Neobvyklé výdavky | Je tento mesiac / táto zmluva mimo bežného správania organizácie? | odchýlka od intervalu predpovede spoločného modelu | pokrytie intervalu, podiel upozornení potvrdených človekom | sezónny naivný, priemer podobných organizácií |
| C. Delenie a hromadenie pod limitom | Hromadí objednávateľ zmluvy tesne pod limitom? | nadbytok zmlúv tesne pod limitom oproti očakávaniu (test nespojitosti hustoty) | veľkosť nadbytku s intervalom | rozdelenie u ostatných objednávateľov |
| D. Porovnanie ceny | Je cena zmluvy (IT projektu) mimo bežného rozpätia podobných? | kvantil ceny v kategórii predmetu | podiel odľahlých, zhoda s hodnotením ÚHP | medián kategórie |
| E. Predpoveď na plánovanie | Koľko organizácia minie (rozpočet)? | suma bez ojedinelých veľkých zmlúv | MASE (+ MAE, RMSE, sMAPE) | posledná hodnota, sezónny naivný, drift, priemer |

Model vyberá typ a parametre podľa karty; kód overí uskutočniteľnosť (stĺpce, dĺžka histórie, počet štítkov ≥ prah,
napr. 50 pozitívnych prípadov pre typ A). Typ E (dnešný experiment) je iba jedna z možností a pri H11 nie je hlavný.

### 4.3 Predspracovanie s protokolom

Každý krok zapíše do postupu „čo, prečo, koľko riadkov“ (rozšírenie dnešného postupu krok po kroku):
zlúčenie verzií zmluvy (už je), oprava IČO (už je), **prepojenie dodatkov** (regulárne výrazy na „Dodatok č. X
k zmluve č. Y“ + rovnaký objednávateľ a dodávateľ; nejasné texty 9B), nulové a extrémne sumy (príznak, nie zmazanie),
**kategória predmetu** (9B v dávkach + zhluky embeddingov; presnosť na vzorke), časové okná a **ochrana pred
únikom**: príznak zmluvy sa smie počítať iba z údajov známych k dátumu jej podpisu.

### 4.4 Modely (Python, CPU)

- **Spoločný model cez organizácie** namiesto modelu na jednu organizáciu: 26 tis. objednávateľov × mesiace alebo
  500 tis. zmlúv dá dosť dát aj pri 8–36 mesiacoch.
- Typ A: logistická regresia (vysvetliteľná), gradient boosting (LightGBM), pravidlový index ako silný naivný odhad;
  kalibrácia pravdepodobností.
- Typ B a E: exponenciálne vyrovnávanie a ARIMA (statsmodels) pri ≥ 24 mesiacoch, spoločný boosting s oneskorenými
  hodnotami; **intervaly predpovede** (kvantilová regresia alebo konformné intervaly), lebo upozornenie potrebuje
  „mimo rozpätia“, nie bodovú predpoveď.
- Typ C: test nespojitosti hustoty pri limite (štýl McCrary) s bootstrap intervalom.
- Nové závislosti: `scikit-learn`, `statsmodels`, `lightgbm` – bežné, bez cloudu, na CPU; zapísať do DECISIONS.

### 4.5 Výsledková tabuľka: všetky naivné odhady × všetky metriky

- Spočítať **všetky** naivné odhady daného typu a **všetky** metriky (predpoveď: MAE, RMSE, MASE, sMAPE, MAPE;
  poradie: precision@k pre viac k, AUC-PR, Brierovo skóre) a zobraziť ako maticu.
- Predregistrácia **musí** určiť jednu hlavnú dvojicu (metrika × naivný odhad) a prah; ostatné sú *sekundárne*.
  Dôvod: pri 5 naivných odhadoch × 5 metrikách sa vždy nájde kombinácia, kde model „vyhrá“ (záhrada rozvetvených
  ciest). Pravidlo úspechu vyhodnocuje kód iba na hlavnej dvojici; sekundárne ukazujú robustnosť.
- Rozdelenie podľa času (učenie na starších, test na novších), interval cez bootstrap po organizáciách alebo zmluvách.

### 4.6 Záver pre problém (nový krok)

Šablóna (model píše iba z faktov a karty, čísla kontroluje kód, tvrdenia sudca):

1. **Problém a rozhodnutie** (z karty).
2. **Čo sme otestovali** jednou vetou (typ, dáta, obdobie).
3. **Výsledok v reči rozhodnutia**: napr. „kontrolór, ktorý preverí 50 zmlúv podľa skóre, nájde {X} zmlúv
   s navýšením dodatkom, pri výbere najväčších {Y}, náhodne {Z}“.
4. **Robustnosť** (sekundárne metriky, interval).
5. **Obmedzenia** (dĺžka dát, štítok je náhradný ukazovateľ, nie korupcia).
6. **Čo overiť v rozhovoroch Brány 0** (napr. „ako dnes vyberáte zmluvy na kontrolu?“).

## 5. Konkrétne pre H11

**Navrhované rozdelenie** (rozhodne človek):

1. *Kontrola zmlúv s obmedzenou kapacitou* (kontrolóri obcí a krajov, interný audit) → typy A a C. **Hlavný kandidát.**
2. *Neoveriteľné náklady IT projektov* (ÚHP, rezorty) → typ D; štítky z hodnotení ÚHP (málo prípadov).
3. *Konflikt záujmov pri obstarávaní* → potrebuje údaje o osobách (RPVS: koneční užívatelia výhod) = osobné údaje;
   **odporúčam vynechať** alebo iba po posúdení oprávneného záujmu (LIA) a bez ukladania mien.

**Prvý experiment (typ A):** štítok „dodatok s navýšením ceny do 12 mesiacov od podpisu“; príznaky k dátumu podpisu:
hodnota a vzdialenosť od limitu zákazky (limity overiť na uvo.gov.sk – menia sa novelami), chýbajúci odkaz na ÚVO
pri vyššej hodnote, oneskorenie zverejnenia, nový dodávateľ pre objednávateľa, podiel dodávateľa na zmluvách
objednávateľa, viac zmlúv s rovnakým dodávateľom do 30 dní, kategória predmetu, vek a veľkosť dodávateľa (RPO, RÚZ).
Potrebuje históriu aspoň 24–36 mesiacov (štítok sa „dozrieva“ 12 mesiacov).

**Súčasné E1–E5** nechať ako technický test; do rozhovorov Brány 0 ich nepoužiť.

## 6. Dáta a súkromie

| Zdroj | Na čo | Náročnosť | Súkromie |
|---|---|---|---|
| CRZ 36 mesiacov | štítky (dodatky), príznaky, dlhšie rady | ~1 100 denných dávok × 10 s ≈ 3–4 h; ~280 MB ZIP, ~0,35 GB DuckDB (podľa 266 dní = 70 MB / 91 MB) | bez mien dodávateľov (už dnes) |
| RPO, RÚZ (organizácie) | vek, veľkosť, obrat dodávateľa | klienti s cache už sú | iba organizácie |
| NKÚ, ÚHP (správy) | štítky pre typy A a D | extrakcia citátov ako dnes | bez osôb |
| ÚVO | jediný uchádzač, postup | robots.txt zakazuje sťahovanie → iba ak sú v katalógu otvorených dát (overiť) | – |
| RPVS | konflikt záujmov | – | osobné údaje → odporúčam vynechať |

## 7. Plán implementácie

| Fáza | Obsah | Odhad |
|---|---|---|
| 1 | Výsledková tabuľka (všetky naivné odhady × metriky, hlavná dvojica v predregistrácii), problémová karta, záver pre problém | 1–1,5 dňa |
| 2 | CRZ 36 mesiacov, prepojenie dodatkov, kategórie predmetu (9B), protokol predspracovania | 2 dni |
| 3 | Knižnica typov A, B, C (+ E ako dnes), spoločné modely, intervaly; nové závislosti | 3–4 dni |
| 4 | Štítky z NKÚ a ÚHP, typ D | 2 dni |

## 8. Rozhodnutia pre teba

1. Ktorý podproblém H11 riešiť prvý (návrh: kontrola zmlúv s obmedzenou kapacitou, typy A a C)?
2. Povoliť závislosti `scikit-learn`, `statsmodels`, `lightgbm`?
3. Stiahnuť CRZ za 36 mesiacov (~3–4 h, cez noc)?
4. Konflikt záujmov a RPVS: vynechať (odporúčam), alebo pripraviť LIA?
5. Rozdeliť H11 na užšie hypotézy, alebo nechať jednu s podproblémami v karte?
