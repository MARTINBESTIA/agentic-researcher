# Posúdenie oprávneného záujmu (LIA) – Problem Radar, Brána 0

> Šablóna na vyplnenie, nie právna rada. Pred väčším objemom oslovení sa poraď s právnikom.
> Vyplň kópiu v `data/LIA.md` (priečinok `data/` je v `.gitignore`), šablónu nechaj prázdnu.
> Po vyplnení a podpísaní nastav v `config.yaml` hodnotu `privacy.lia_completed: true`.

- Prevádzkovateľ: {MENO_PRIEZVISKO}, kontakt: {EMAIL_ODOSIELATELA}
- Dátum: {DÁTUM} · verzia: 1
- Ďalšia revízia: {DÁTUM + 6 mesiacov}, alebo skôr pri zmene spracúvania (nový zdroj, nový účel, vyšší objem)

## 1. Opis spracúvania

| | |
|---|---|
| Aké údaje | meno a tituly, funkcia, organizácia, pracovný e-mail a telefón, URL verejného profilu, zdrojová URL a úryvok (≤ 300 znakov), väzba na problém, stav komunikácie; z rozhovorov poznámky v pseudonymizovanej podobe |
| Koho sa týkajú | pracovníci organizácií a verejní funkcionári, ktorých pracovný kontakt zverejnila organizácia alebo oni sami; osoby verejne citované k danému problému |
| Odkiaľ | weby organizácií, úradné zverejnenia (napr. oznámenia o obstarávaní), Register právnických osôb (iba meno a funkcia štatutára), verejné články a programy podujatí, výsledky vyhľadávania |
| Rozsah | najviac 15 osôb na hypotézu a 3 hypotézy naraz (rádovo desiatky osôb); najviac 10 oslovení denne |
| Kde | lokálne na šifrovanom disku, bez cloudu; GUI dostupné iba z tohto počítača (127.0.0.1) |
| Ako dlho | neoslovení 90 dní; po poslednej komunikácii najviac 12 mesiacov; po odmietnutí 30 dní; po námietke okamžitý výmaz |
| Komu sa poskytujú | nikomu |
| GDPR sa uplatní? | áno; príprava podnikania nie je čisto osobná ani domáca činnosť (čl. 2 ods. 2 písm. c GDPR) |

## 2. Test účelu: je záujem oprávnený?

- **Záujem:** pred investovaním času a peňazí do produktu overiť rozhovormi s ľuďmi, ktorí za daný
  proces zodpovedajú, či problém naozaj existuje, ako ho dnes riešia a kto by za riešenie platil
  (prieskum trhu, customer discovery).
- **Zákonnosť:** ide o prípravu podnikania, ktorú chráni sloboda podnikania (čl. 16 Charty
  základných práv EÚ). Odôvodnenie 47 GDPR uznáva ako možný oprávnený záujem dokonca priamy marketing;
  prosba o rozhovor bez ponuky je menej invazívna.
- **Prínos pre oslovených:** môžu ovplyvniť, či a ako vznikne riešenie ich problému. Účasť je dobrovoľná.

Doplň vlastnými slovami: {PREČO TO ROBÍM, PRE KTORÉ PROBLÉMY}

## 3. Test nevyhnutnosti: dá sa to urobiť s menším zásahom?

- Rozhovor s konkrétnym človekom, ktorý za proces zodpovedá, sa bez jeho kontaktu urobiť nedá.
- Menej invazívne cesty, ktoré sa použijú prednostne, kde stačia:
  - generický kontakt organizácie s prosbou o nasmerovanie;
  - odporúčanie od človeka, ktorý už s rozhovorom súhlasil;
  - verejné podujatia a diskusie.
- Minimalizácia:
  - iba polia z časti 1;
  - žiadne súkromné kontakty, adresy, fotografie ani dátumy narodenia;
  - obsah kontaktných stránok sa neukladá;
  - e-maily sa nehádajú a obfuskované adresy sa nedekódujú.

## 4. Test vyváženosti: neprevážia záujmy oslovených?

- **Povaha údajov:** bežné pracovné údaje, žiadne osobitné kategórie (čl. 9 GDPR).
- **Primerané očakávania:** kto pri svojej funkcii zverejnil pracovný kontakt, môže očakávať pracovné
  otázky k svojej agende. Pri radových zamestnancoch to platí menej, preto sa vyberajú osoby podľa
  funkcie a väzby na problém, nie celé zoznamy zamestnancov.
- **Možný dopad:** nevyžiadaná správa a stratený čas. Riziko je nízke, zmierňujú ho záruky.
- **Záruky:**
  - nič sa neodosiela automaticky, každú správu schvaľuje a posiela človek;
  - najviac jedno pripomenutie a denný limit 10 oslovení;
  - jednoduché odmietnutie v každej správe; pri námietke okamžitý výmaz a zápis do zoznamu
    potlačenia, aby osoba nebola oslovená znova;
  - informácia podľa čl. 14 GDPR v prvej správe;
  - lokálne uloženie, šifrovanie disku, maskovanie kontaktov v logoch, pseudonymizácia v správach;
  - žiadne rozhodnutia s právnymi účinkami: priorita iba zoraďuje zoznam, rozhoduje človek;
  - pri prebiehajúcom verejnom obstarávaní žiadny neformálny kontakt s obstarávateľom.

## 5. Záver

- [ ] Oprávnený záujem pri uvedených zárukách prevažuje nad záujmami a právami oslovených osôb.
- [ ] Neprevažuje. Spracúvanie upravím (napr. iba generické kontakty) alebo ho nebudem robiť.

Podpis: ____________________ Dátum: ____________

## 6. Evidencia žiadostí

Osobné údaje sem nepíš (identifikátory sú v databáze ako hash v zozname potlačenia).

| Dátum | Typ (námietka / výmaz / prístup / oprava) | Vybavené dňa | Poznámka |
|---|---|---|---|
| | | | |

---

## 7. Informácia pre oslovené osoby (čl. 14 GDPR) – plné znenie na zverejnenie

Zverejni napr. ako jednoduchú webovú stránku a jej adresu doplň do `outreach.sender.privacy_notice_url`.

> **Informácia o spracúvaní osobných údajov – výskumné rozhovory**
>
> **Kto spracúva:** {MENO_PRIEZVISKO}, {EMAIL_ODOSIELATELA}.
>
> **Aké údaje:** meno, funkcia, organizácia, pracovný e-mail alebo telefón, prípadne odkaz na verejný
> profil. Ak súhlasíte s rozhovorom, aj poznámky z neho v pseudonymizovanej podobe.
>
> **Odkiaľ ich mám:** z verejne dostupných zdrojov: z webu vašej organizácie, z úradných zverejnení,
> z Registra právnických osôb, z verejných článkov alebo programov podujatí. Konkrétny zdroj je
> uvedený v správe, ktorú ste dostali.
>
> **Prečo:** aby som vás mohol požiadať o krátky rozhovor o vašej pracovnej skúsenosti s témou
> uvedenou v správe. Rozhovor slúži na pochopenie problému skôr, než by vznikol akýkoľvek produkt
> alebo služba. Nejde o ponuku ani predaj.
>
> **Právny základ:** oprávnený záujem (čl. 6 ods. 1 písm. f GDPR), ktorým je prieskum pred založením
> podnikania.
>
> **Komu ich poskytujem:** nikomu. Údaje sú uložené iba na mojom šifrovanom počítači.
>
> **Ako dlho:** ak neodpoviete, najviac 90 dní; po poslednej komunikácii najviac 12 mesiacov. Ak
> odmietnete, údaje do 30 dní vymažem. Ak namietnete alebo požiadate o výmaz, vymažem ich hneď.
> Ponechám si iba jednosmerný odtlačok (hash) vášho kontaktu, aby som vás omylom neoslovil znova.
>
> **Vaše práva:** prístup, oprava, výmaz, obmedzenie spracúvania a námietka. Stačí odpísať na
> {EMAIL_ODOSIELATELA}. Máte tiež právo podať návrh na začatie konania na Úrade na ochranu osobných
> údajov Slovenskej republiky (dataprotection.gov.sk).
>
> **Automatizované rozhodovanie:** nepoužívam.
