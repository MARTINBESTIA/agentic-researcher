# Od doménového prehľadu ku konkrétnej ponuke – karty organizácií (schéma v15)

Zadanie 17. 10. 2026. Prehľad domény je spoločný pre odvetvie; oslovenie konkrétnej organizácie však potrebuje
odpovede pre **ňu**: komu pomáham → v akom pracovnom úkone → čo sa zmení → aký minimálny vstup potrebujem → ako
overíme prínos. Tento dokument opisuje dátový tok, stavy, konfiguráciu, limity výskumu, GUI, export, migráciu
a praktický postup na troch organizáciách.

## 1. Čo bolo zle a čo sa zmenilo

| Pred v15 | v15 |
|---|---|
| Návrh prínosu bol jeden pre všetkých zákazníkov hypotézy (`build_package` vzal prvý všeobecný návrh). Kancelária so zhodou segmentu aj kancelária, ktorá riešenie už používa, dostali rovnakú otázku. | Ponuka sa berie z **karty organizácie** pre danú rolu a verziu karty. Bez karty dostane zákazník iba situačnú otázku; všeobecný návrh ostáva v balíku ako označený podklad. |
| Citát o zosúlaďovaní dokladov sprostredkovateľského orgánu eurofondov sa použil ako dôkaz procesu komerčnej účtovnej kancelárie. | Každý citát sa posúdi **voči cieľu** (doména alebo organizácia): relevantnosť, význam, stav potreby a program. Iný program alebo iný typ aktéra = „iba kontext“, nie dôkaz procesu. |
| Referencia „firma X používa produkt Y“ vytvorila väzbu „organizácia v dôkaze o probléme“. | Väzba `uses_solution` („používa riešenie – over zostávajúcu medzeru“), stav potreby „podľa zdroja riešená“. |
| Opis funkcie od dodávateľa („ušetrí 80 % času“) sa miešal s dôkazmi problému. | Dodávateľ (pravidlo podľa domény má prednosť pred odhadom modelu) ide do skupiny existujúcich riešení. Úspora dodávateľa sa nikdy neprenáša na inú organizáciu. |
| Všeobecný rozcestník prípravných trhových konzultácií označil organizáciu ako „prebiehajúce obstarávanie“ a návrh sa zablokoval. | Rozcestník je **kandidát na overenie**: návrh smie vzniknúť, schválenie čaká (`procurement_unverified`), kým to človek overí. Potvrdené obstarávanie vyžaduje konkrétny záznam s predmetom, lehotou v budúcnosti a súvisom s hypotézou. |
| NBS / ministerstvo sa zaradili medzi zákazníkov podľa slov v dôkaze. | **Zhoda so segmentom** sa posúdi zvlášť. Nesúlad nevymaže kontakt: s dátovou alebo expertnou väzbou ostane v tej roli, inak „preveriť rolu“ (mimo fronty prvého oslovenia). |
| Prázdny proces s otázkou hlásil „pripravené – pokrytie všetkých častí“; `data_needed` bez odkazu sa uložil ako zdrojový fakt. | Pokrytie: podložené / hypotéza / len otázka / chýba. „Pripravené“ vyžaduje proces a rozhodnutie (úkon) aspoň ako hypotézu a ≥ 2 podložené časti. Položka dát bez odkazu je **návrh požadovaného vstupu**. |

## 2. Dátový tok

```
dôkazy (claims) ──► posúdenie voči cieľu (evidence_assessments, logika rel-2)
                       │  pravidlá kódu: program, typ aktéra, vydavateľ, význam, stav potreby
                       ▼
              skupiny podkladov: proces a problém │ o tejto organizácii │ existujúce riešenia │ protiargumenty │ iba kontext
                       │
prehľad domény ────────┤ (všeobecný, označený – nikdy fakt o organizácii)
profil organizácie ────┤ (register, overený web, dôvod zaradenia, zhoda segmentu, obstarávanie; funkcie ľudí bez mien)
odpovede jej kontaktov ┤ (iba tejto organizácie, bez mien)
dáta organizácie ──────┤ (importy event logu, procesné reporty, experimenty Brány 2)
výsledky výskumu ──────┘
                       ▼
           úloha org_card (presný model)  ──►  kontroly kódom  ──►  hodnotiteľ org_card_check (opora │ použiteľnosť)
                       ▼
           org_cards (verzia; hodnotenie; 3 pripravenosti; ďalší krok)  ──►  research_tasks z medzier
                       ▼                                                       │ (verejné zdroje, limity)
           ponuka v návrhu oslovenia (karta + verzia)  ◄──────────────────────┘ ďalšia verzia karty
                       ▼
           odpoveď (outreach_outcomes s verziou karty) ──► zastaranie iba kariet a návrhov tejto organizácie
```

Moduly: `radar/relevance.py` (posúdenie a skupiny), `radar/recipients.py` (segment, stav potreby, obstarávanie),
`radar/opportunity.py` (karta, kontroly, pripravenosť, výskum, akcia, export, porovnanie), `radar/db/opportunity.py`
(SQL), `radar/gui/org_card_panel.py` (GUI).

## 3. Relevantnosť dôkazov

Cieľ posúdenia: `domain:<kľúč domény>` (prehľad) alebo `card:<organizácia>:<hypotéza>` (karta). Pravidlá kódu
(`assess_claim`) určia strop; voliteľné spresnenie rýchlym modelom (`org_cards.model_relevance`) ho môže iba znížiť:

- **relevantnosť:** priama / prenositeľná analógia / iba kontext / nesúvisiaca / nejasná. Súkromný cieľ a citát
  z iného programu (eurofondy, LIFE, verejné obstarávanie, štátny rozpočet) → kontext. Súkromný cieľ a dotknutý
  verejný subjekt → analógia (problém) alebo kontext. Zhodné slová („účtovníctvo“, „doklady“) nestačia.
- **význam:** problém, opis procesu, existujúce riešenie, funkcia produktu, skúsenosť po nasadení, protiargument, kontext.
  Marketing dodávateľa nie je dôkaz problému.
- **stav potreby v zdroji:** historická (napr. zistenie kontroly), podľa zdroja riešená, neznáma. Verejný zdroj
  nikdy nepotvrdí **aktuálnu** potrebu – tú potvrdí iba odpoveď kontaktu.
- výber: rôznorodosť (najviac `per_document` citátov z jedného dokumentu), kvóty skupín, nesúvisiace sa vynechajú.

Posúdenie človeka (`assessed_by = human`) sa nikdy neprepíše modelom ani kódom.

## 4. Adresát

V „Koho osloviť“ pribudli stĺpce **zhoda so segmentom**, **stav potreby**, **riešenie** a **obstarávanie** a úroveň
odporúčania **„preveriť rolu“**. Pravidlá (`radar/recipients.py`, bez modelu):

- verejná inštitúcia pri súkromnom segmente (a naopak) nie je zákazník; SK NACE z registra mimo filtra segmentu je iba „neznáme – over“ (filter navrhol model a nie je dosť spoľahlivý na vyradenie);
- stav potreby: posledná vlastná odpoveď organizácie (potvrdená / vyvrátená), inak „podľa zdroja riešená“ pri väzbe
  `uses_solution`, inak hypotéza. „Bez odpovede“ nie je vyvrátenie; odpoveď inej organizácie stav nemení;
- obstarávanie: `none` / `candidate` / `confirmed` / `closed` s dôvodom a ďalším krokom. GUI: Brána 0 → Koho osloviť →
  „Obstarávanie na overenie“ → *Overené: nič súvisiace neprebieha* alebo *Potvrdené súvisiace obstarávanie*.
  Potvrdené = iba oficiálna cesta (PTK, vysvetľovanie súťažných podkladov), nové návrhy sa nevytvoria.

## 5. Karta organizácie

Bloky: prečo práve táto organizácia; pracovný prípad (spúšťač, jednotka práce, roly, vstup, kroky, nástroje, výsledok,
výnimky); prekážka (pozorovaný jav **oddelene** od hypotéz príčiny); možnosť zásahu (kto môže konať, čo sa zmení, čo je
mimo dosahu); dnešné riešenie („nevieme“ ≠ „nepoužívajú nič“); najviac 3 možnosti (procesná zmena, nastavenie
existujúceho nástroja, integrácia, automatizácia úkonu, kontrola úplnosti a kvality, podpora rozhodovania, zmena
kapacít/zodpovedností, analytika) s mechanizmom, závislosťami a nevýhodami; malá skúška (vstup, výstup, porovnanie
s dnešným postupom, metrika, obmedzenia, podmienka nepokračovať – rozsah je návrh); čo potrebujeme od partnera (jeden
krok a alternatíva bez dát; technický príklad ≠ vzorka na preukázanie účinku); hlavná otázka a najviac 5 otázok – každá
s rozhodnutím, ktoré zmení; ponuka; voliteľný príklad „dnes → po zmene“ (vždy označený **ilustrácia**, nikdy dôkaz);
rozpory zvlášť.

**Stav poznania každého výroku:** `source_fact` (zdroj), `contact_reported` (iba z odpovedí), `data_observed` (iba
z dát), `hypothesis` (s odôvodnením a spôsobom vyvrátenia), `proposal`, `unknown`.

**Kontroly kódom** (`code_check_card`) – výrok sa nemaže, iba zoslabí a zapíše dôvod:

- fakt bez platného citátu (alebo s citátom iba z „kontextu“) → hypotéza;
- fakt v blokoch o organizácii bez podkladu o nej (iba všeobecný podklad) → hypotéza („všeobecný podklad o odvetví“);
- pozorovaný jav podložený iba referenciou používania produktu → hypotéza (historický problém);
- úspora dodávateľa mimo bloku „dnešné riešenie“ → citát sa odstráni, výhrada;
- výrok kontaktu bez odpovede, pozorovanie bez dát → hypotéza; čísla bez podkladu → výhrada;
- hypotéza bez odôvodnenia / vyvrátenia → výhrada (nižšia priorita, nie zamietnutie). Návrh **merania** („skúsime
  zmerať, ktoré typy dokladov potrebujú najviac opráv“) prejde; rovnaký obsah tvrdený ako fakt o firme neprejde;
- najviac 3 možnosti, známy typ zásahu, mechanizmus; úplnosť skúšky; hlavná otázka; percentá alebo istý prínos bez
  merania; predplatné/cena pre držiteľa dát alebo experta.

**Hodnotiteľ** (`org_card_check`, rola reviewer) vráti dve nezávislé časti: *opora tvrdení* a *použiteľnosť návrhu*.
Zlyhanie = „čaká na hodnotiteľa“, nikdy schválenie. Stav karty: skontrolované / na kontrolu / čaká na hodnotiteľa /
upravené človekom / nahradené; zastaranie a rozpor sa zobrazujú zvlášť.

## 6. Pripravenosť (tri nezávislé úrovne) a hodnotenie

1. **Na rozhovor:** dôvod výberu, zhoda roly, zrozumiteľná hypotéza alebo otázka, overený kanál (menný kontakt môže
   chýbať, ak je organizačný). Kontroly odoslania (zdroj kontaktu, obstarávanie, potlačenie, hodnotiteľ návrhu) sú
   samostatné v Osloveniach.
2. **Na návrh skúšky:** konkrétny úkon, kandidátny zásah (vybraný v GUI, inak prvý), vstup, rozsah (výstup
   a obmedzenia), metrika, závislosti; zvlášť **súhlas** (rozhovor/pilot dohodnutý) a **dáta** (vzorka alebo
   skontrolovaný import).
3. **Na tvrdenie o prínose:** experiment na dátach organizácie, ktorý prekonal relevantné jednoduché porovnanie,
   s platnou metodikou (význam entity a štítka), nie syntetický, verdikt „pokračovať“ (`exp_tasks.verdict`) a človekom
   priradený k úkonu karty. Inak dôvod pri každom experimente (napr. „E6: neprekonal relevantné jednoduché porovnanie“).

**Hodnotiaca tabuľka** (prečo tento adresát, konkrétny úkon, dnešný postup, prekážka, právomoc zasiahnuť, existujúce
riešenie/medzera, kandidátny zásah, minimálny vstup, metrika, neistota): *jasné s podkladom / jasná hypotéza / chýba /
rozpor*. Žiadna percentuálna pravdepodobnosť úspechu. **Ďalší krok** sa odvodí z prvej nesplnenej úrovne.

Neúplná karta nezastaví Brány 0–2; blokuje sa iba konkrétny nepodložený sľub (kontroly návrhu oslovenia).

## 7. Ponuka v návrhu oslovenia

`outreach.build_package` použije aktuálnu kartu (organizácia + hypotéza + rola): potreba z pozorovaného javu (so stavom
poznania), výstup a prospech z ponuky, rozhodnutie z „čo sa zmení“, minimálny vstup z prvého kroku, hlavná otázka
a otázky na rozhovor z karty. Návrh nesie `card_id` a `card_version`. Pripravenosť návrhu upozorní na zastaranú kartu
alebo nový rozpor. Bez karty: zákazník iba situačná otázka; držiteľ dát a expert všeobecný návrh pre svoju rolu (ich
vlastný prospech – dátová kontrola, overiteľný rozbor; nikdy predplatné). Obchodné podmienky (`outreach.value_offer`)
sa nemenia: veta o bezplatnom deme a skúške iba komerčnému adresátovi s konkrétnym výstupom. Neodoslaný návrh, ktorý nevychádza z aktuálnej karty (starší návrh alebo staršia verzia karty), má v Osloveniach upozornenie a tlačidlo **Nahradiť návrhom z karty** (iba neupravený a neschválený návrh; nový vznikne v akcii návrhov).

## 8. Cielený výskum medzier

Z medzier karty vzniknú úlohy (`research_tasks`): otázka, rozhodnutie, ktoré odpoveď zmení (adresát → výber riešenia →
návrh skúšky), najvhodnejší zdroj a dopyty s názvom organizácie (a `site:` overeného webu). Interné otázky (časy,
frekvencia, dôvod zdržania, skutočné používanie, rozpočet) sa **nehľadajú** – idú na kontakt, vzorku alebo event log.
Do limitu sa vždy dostane aspoň jedna otázka o existujúcom riešení. Výskum používa existujúci SearXNG a fetcher
(robots, blokované domény, LinkedIn sa nesťahuje), doslovné odseky bez e-mailov a telefónov
(`domain_evidence` s kľúčom `org:<id>`, rozsah „organizácia“ iba pre jej overený web alebo odsek s jej IČO – samotná zhoda názvu nestačí; nález iba z iných odsekov je „čiastočný“). Dopyty sú krátke podľa druhu medzery (názov bez právnej formy, pri overenom webe najprv `site:`), odseky webu organizácie uložené skoršou úlohou sa použijú aj pre ďalšie otázky.
Výsledok: nájdené / čiastočné / rozpor / verejne nezistiteľné / zdroj nedostupný. Odpoveď modelu bez platného úryvku
sa neuloží ako nález. Úloha sa po pokuse neopakuje; už stiahnutá URL sa nesťahuje znova; výpadok = otázka ostáva.

| nastavenie (`org_cards`) | predvolené |
|---|---|
| `research_max_tasks_per_card` | 3 úlohy na kartu a beh |
| `research_max_queries_per_task` | 2 dopyty |
| `research_max_documents_per_task` | 3 dokumenty |
| `research_max_seconds_per_card` | 300 s (aj zvyšok času behu) |
| `research_max_attempts` | 1 pokus na úlohu |
| `research_model_calls` | true (úloha `gap_answer`; false = iba úryvky) |

## 9. Verzie, zastaranie, žiadna hromadná regenerácia

Hash vstupov karty zahŕňa dôkazy s posúdením, prehľad domény (id a hash), doménové a organizačné podklady, odpovede,
dáta, výskum, profil, rolu a **verzie schémy karty, promptov karty a kontroly, obchodnej politiky a logiky
relevantnosti**. Ukladá sa poskytovateľ a model/nasadenie. Nová verzia vznikne iba pri zmene hashu alebo na žiadosť;
staršie ostávajú (nahradené). Zmena verzie promptu/politiky kartu iba **označí ako zastaranú** (`mark_stale`, bez
volania modelu); obnoví sa zvolená dávka (sústredený režim, vyžiadaná karta) alebo príkazom. Schválenie sa na novú
verziu neprenáša.

## 10. Učenie z odpovedí

Zápis výsledku (`outcomes.record_outcome`, GUI karta → *Zapísať odpoveď organizácie*) sa viaže na organizáciu,
tím/rolu, hypotézu, otázku a verziu karty. Štruktúrované časti: posledný prípad, frekvencia, používaný nástroj,
nevyriešená časť, vlastník procesu, spôsob rozhodovania o kúpe, dostupnosť dát; dôvod nezáujmu (problém neexistuje /
už vyriešené / nesprávny adresát / nízka priorita / rozpočet / nevhodná ponuka / iné / nezistené). Automaticky
vyťažené informácie (`source != human`) majú príznak na kontrolu človekom. Odpoveď označí na obnovu **iba** karty
a návrhy tejto organizácie; ak odporuje schválenej karte, rozpor sa zobrazí aj pri nej. Prázdne „bez odpovede“ kartu
nemení. Potvrdenie jedným kontaktom sa neprenáša na odvetvie ani iný tím.

## 11. Event log a overovanie prínosu

Žiadosť o event log z karty (`event_logs.build_request(card_id=…)`) vznikne iba vtedy, keď log odpovedá na otázku
karty (poradie, trvanie, čakanie, vrátenia, termíny). Pri vyťažovaní dokladov sa ponúkne alternatíva: vhodné dokumenty
a správny výsledok, prechod posledného prípadu alebo krátky záznam práce. Procesný report ostáva deterministický
(uplynutý čas ≠ pracovný čas ≠ čakanie). Event log so samotnými časovými značkami nepodporí tvrdenie o ušetrenom
pracovnom čase. Benefit guard: pozri pripravenosť 3 – neplatný, syntetický alebo horší výsledok než jednoduché
porovnanie nepodporí obchodný sľub.

## 12. Sústredený režim prípravy oslovení

Brána 0 → Doména a prínos → *Sústredený režim prípravy oslovení* (najviac `focus_max_orgs` = 3 organizácie naraz,
aj naprieč hypotézami), alebo `python -m radar org-cards --focus on --hypothesis 4 --org 4 --org 100`. Autopilot potom
v každom kole najprv vytvorí/obnoví ich karty a medzery (akcia `org_cards`) a návrhy oslovení; nový všeobecný zber
dostane iba zvyšný čas (bez vyhradeného podielu). Hypotézy ani plán sa nemažú. Mimo režimu autopilot karty nevytvára.

## 13. GUI, CLI a export

**Klikacia cesta:** Brána 0 → vyber hypotézu → záložka *Doména a prínos* → časť *Karta organizácie* → vyber
organizáciu a rolu → *Vytvoriť / obnoviť kartu* → *Spustiť akciu Karty organizácií* (úloha na pozadí v správcovi
úloh, dá sa zrušiť; vykreslenie stránky model nevolá). Pri karte: päť odpovedí, tri pripravenosti, ďalší krok;
v rozbaľovacích sekciách celá karta s exportom (*Stiahnuť kartu (Markdown)*), hodnotenie a kontroly, dôkazy s relevantnosťou,
medzery (doplnenie odpovede alebo nové hľadanie), výber skúšky, priradenie experimentu, zápis odpovede, ručná oprava
(nová verzia s autorom „človek“), schválenie a technické údaje. Akcia je aj v Správcovi akcií („Karty organizácií
a ponuky“).

```
python -m radar org-cards --run --target 4:4 --target 100:4 --target 33:11      # vytvoriť/obnoviť 3 karty (+ výskum)
python -m radar org-cards --list                                               # karty a pripravenosť
python -m radar org-cards --target 4:4 --target 100:4 --target 33:11 --export exports/karty.md
python -m radar org-cards --target 4:4 --target 100:4 --target 33:11 --compare reports/porovnanie.md
```

Export a dosiér (`brana0/karty/…md`, `brana0/prehlad_domeny_<id>.md`, tabuľka v README dosiéru) sú predvolene
pseudonymizované; fyzická osoba – podnikateľ je bez mena. `--compare` bez `--with-names` nahradí názvy organizácií
a odkazy.

## 14. Migrácia v15

Iba pridávanie (záloha pred migráciou): tabuľky `evidence_assessments`, `org_cards`, `research_tasks`; stĺpce
`value_proposals.card_id/card_version/recipient_role/stale_reason`, `outreach_outcomes` (verzia karty, otázka,
štruktúrované časti, dôvod nezáujmu, príznak kontroly, zdroj), `org_hypotheses.procurement_*` a `segment_fit_json`,
`eventlog_requests.card_id`, `outreach.card_id/card_version`. Staré nejasné údaje sa iba označia: obstarávanie zo
všeobecnej stránky → kandidát (URL a poznámka „v15: neoverené“), väzba z citátu typu existujúce riešenie →
`uses_solution`, `data_needed` bez odkazu → návrh vstupu. Staré prehľady, schválené návrhy, poznámky, odpovede a merania
ostávajú.

## 15. Cloud a súkromie

Smerovanie podľa úloh: `org_card` a `gap_answer` → presný model, `org_card_check` → hodnotiteľ, `evidence_relevance` →
rýchly. Všetky štyri sú v `cloud.allowed_tasks` (verejné údaje o právnickej osobe; funkcie ľudí bez mien). Karta
fyzickej osoby – podnikateľa sa volá s `personal=True` a do cloudu ide iba pri `privacy.cloud_personal_data: true`.
Rozpočet cloudu sa nemení. Nič sa neodosiela.

## 16. Praktický postup na troch organizáciách

1. Brána 0 → hypotéza → Doména a prínos → zapni sústredený režim a vyber tri organizácie s rôznou situáciou (zhoda
   segmentu bez potvrdenej bolesti; organizácia s používaným riešením; organizácia s doloženou procesnou udalosťou).
2. Spusti akciu „Karty organizácií a ponuky“ (alebo `org-cards --run --target …`).
3. Prečítaj päť odpovedí a hodnotenie; pri medzerách s kontaktom ako zdrojom ich nechaj na rozhovor.
4. Vyber skúšku, skontroluj ponuku a hlavnú otázku; návrh oslovenia (akcia „Pripraviť návrhy oslovení“) prevezme kartu.
5. Odosielaš ručne. Odpoveď zapíš ku karte → karta sa označí na obnovu → ďalšia verzia s odpoveďou.
6. Export: *Stiahnuť kartu (Markdown)* alebo `org-cards --export`; porovnanie starého a nového formátu `--compare`.

Výsledok vyhodnotenia z 17. 10. 2026 (zmrazená kópia databázy zo 16. 10.; reálne uložené podklady, žiadne
zákaznícke odpovede): pozri `docs/organization_opportunities_sample.md` (pseudonymizované).

## 17. Obmedzenia

- Karta je návrh modelu nad verejnými podkladmi: hodnotiteľ je pomôcka, nie náhrada kontroly človekom. Žiadna karta
  nie je overenie u zákazníka – aktuálnu potrebu potvrdí iba odpoveď kontaktu.
- Relevantnosť je v prvom rade pravidlami kódu (slovník programov, verejných a súkromných aktérov); nezvyčajné
  formulácie môžu skončiť ako „nejasná“ alebo „analógia“. Spresnenie modelom je voliteľné.
- Overenie obstarávania z výsledku vyhľadávania je heuristické (dátum, slovo „lehota“, prekryv slov s hypotézou);
  potvrdenie a vylúčenie robí človek.
- Pracovný prípad je jeden na kartu; ďalší prípad tej istej organizácie vznikne ako ďalšia karta s iným `process_key`
  (ručne alebo novou žiadosťou).
