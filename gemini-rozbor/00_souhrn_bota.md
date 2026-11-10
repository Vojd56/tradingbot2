# Souhrn trading bota a jeho výsledků (data k 10. 11. 2026 večer)

Všechny účty jsou DEMO (Trading 212 demo v EUR, Binance Futures testnet). Výsledky pokrývají krátké období (24. 10. – 10. 11. 2026), vzorky jsou malé.

## 1. Co bot dělá (architektura)
Python bot na Windows PC (neběží 24/7, uživatel ho občas vypne). Čtyři role: **skener → analytik (LLM Gemini) → risk manager (kód) → exekutor (kód)**. Oznámení přes Discord, webový dashboard. Gemini je na free tieru Google AI Studio (denní limity, občas chyba 503 „high demand“).

### 1a. Intradenní akcie „Scout“ (živě na T212 demo, od 26. 10.)
- Dynamický výběr: ranní briefing zpráv + skener US (každých 30 min 12:00–21:00 Praha) a EU (09:00–17:30), gappeři, neobvyklý objem (≥ 1,5× průměru), výsledky hospodaření.
- Spouštěč: pohyb za 1 h ≥ 3 % nebo objem ≥ 2× nebo jiné. Gemini posoudí zprávu (katalyzátor) a „skeptik“ vypíše námitky; kód nastaví stop a cíl (RRR ≥ 1:2 po nákladech).
- Velikost: riziko 0,5 % kapitálu vč. nákladů (FX 0,15 %/strana + skluz 0,05 %), stop na burze. Vše se zavírá do 15:50 NY (konec seance).
- Pojistky: denní ztráta max 300 USD, po 5 ztrátách v řadě pauza 2 h (každá další ztráta znovu), STOP soubor.

### 1b. Swing akcie (držení dny až týdny, od 2. 11.)
Pravidla (společná pro stopy): vesmír US S&P 1500 + EU (Xetra, Paříž, Amsterdam, Madrid, Curych, Londýn), obrat ≥ 10 mil. USD/EUR, cena ≥ 5, nad SMA50 i SMA200, žádný vstup 5 dní před výsledky, index pod SMA200 = žádné vstupy. Skóre trend/momentum/objem. Vstup další den limitem; stop = max(support, 2× ATR14) na burze; první cíl +2R (prodat půlku), zbytek chandelier stop (22denní max − 3× ATR22, jen nahoru), BE při +1R, časový stop 10 dní bez +0,5R. Riziko 0,5 %/obchod, max 20 % na akcii, max 10 pozic (od 9. 11., předtím 5), 1 pozice na sektor a trh, celkové otevřené riziko max 5 % (předtím 3 %).
- **Stopa A** (Gemini vybere kandidáty, 3 hlasy různých modelů, nákup při ≥ 2/3) – **živě na T212 demo** + virtuální kopie. Večer Gemini kontrola HOLD/TIGHTEN/SELL (SELL jen s citovanou zprávou, stop nikdy dolů).
- **Stopa B** – stejný skener bez Gemini (papír, 2000 EUR).
- **Stopa C** – náhodný výběr ze stejných kandidátů (placebo, papír).
- **Stopa D** – tým 6 rolí na Gemini Flash Lite (papír) – **vypnuta 6. 11.**: 21 posouzení, 0 nákupů (skeptik vždy WAIT kvůli RSI ~70).
- **Stopa E** – nákupy insiderů (CEO nákup ≥ 100k USD, akcie 4–22 % pod 60denním maximem a odráží se, žádné jiné prodeje insiderů; SEC Form 4), bez Gemini, papír. Zatím 0 signálů (backtest ~7 obchodů/rok).

### 1c. Krypto (Binance testnet)
- **HIGH20** (živě od 28. 10.): průraz 20denního maxima na denních svíčkách, vstup 01–03 Praha, stop 1,5× ATR na burze, výstup 00:00 UTC. Backtest 2 roky PF 1,35, +30 %, DD −6 %, obchoduje ~9 % dní.
- **Stará metoda** (papír od 5. 11.): pohyb 1 h ≥ 3 % / objem ≥ 2× / SFP, tým 5 rolí Gemini rozhodne, 500 USD/obchod, riziko 10 USD.

### 1d. Stínové testy
- **ORB15** (průraz rozpětí prvních 15 min, papír, 29. 10. – 6. 11., vypnut): varianta A půlka na +2R, B držet do konce dne.
- **Scout pullback** (od 9. 11.): u signálů Scoutu porovnává 3 vstupy – koupit hned / po poklesu 2–5,5 % z denního maxima / pokles do 30–180 min.

## 2. Výsledky (čísla spočítaná z přiložených souborů)

### Intradenní akcie Scout (živě demo, 26. 10. – 10. 11.), soubor data_1
- 64 uzavřených obchodů, **součet −182,2 USD**, výhra 16/64 (25 %). Průměrná výhra +8,71 USD, průměrná ztráta −6,70 USD.
- Podle důvodu výstupu: stop-loss na burze 33× (0 výher, −270,4 USD); konec seance 24× (11 výher, +6,3 USD); take-profit 3× (+89,3 USD); přes noc 4× (−7,3 USD).
- 20 nákupů selhalo (hlavně málo volné hotovosti na T212, min. množství, chyba načtení účtu) – data_7, akce BUY_FAILED.
- Dne 4. 11. bylo 12 ztrát v řadě.

### Krypto, soubor data_1 (živé) a data_4/data_5
- Živé krypto obchody 24.–29. 10. (starý Gemini režim): 13 obchodů, −2,28 USD; SFP 2 obchody +4,33 USD; HIGH20 1 obchod −24,13 USD (29. 10.). Od té doby HIGH20 neměl signál (100 denních kontrol „bez signálu“).
- Stará metoda (papír 5.–10. 11.): 36 uzavřených, **−266,08 USD, −25,5 R**, výhra 4/36 (11 %); 32× stop-loss, 4× take-profit.

### Swing (2.–10. 11.), soubory data_2 a data_3
| Stopa | Equity (start 2000 EUR) | Uzavřeno | Výhry | Součet R | Pozn. |
|---|---|---|---|---|---|
| A (Gemini) | 2007,93 | 0 | 0 | 0 | 6 otevřených, peak 2017,54 |
| B (bez Gemini) | 1990,86 | 3 | 1 | −1,18 | 10 otevřených |
| C (náhoda) | 1978,62 | 3 | 0 | −1,78 | pauza do 16. 11. po 3 ztrátách |
| E (insideři) | 2000,00 | 0 | 0 | 0 | žádný signál |
| Benchmark US (SPY) | 1991,98 | | | | |
| Benchmark EU (STOXX 600) | 2038,28 | | | | |

- Živá stopa A na T212 demo: 6 otevřených pozic (EHC, AJG, MANH, DSY.PA, GRF.MC, PCTY), **20 nákupů zrušeno** – část kvůli tomu, že limit 5 swing pozic byl plný, část kvůli sektorovému limitu (EU akcie měly sektor „Unknown“ do 3. 11.) a od 9.–10. 11. kvůli tomu, že **intradenní pojistka „5 ztrát za sebou“ blokovala i swing nákupy** (ROIV, FME.DE …).
- Risk manager stínových stop odmítl kandidáty hlavně „plno (5 pozic)“ (343×) a „sektor Unknown už obsazen“ (99×).
- Gemini výpadky (503) 10. 11. přerušily hlasování stopy A.
- Skener: denně US ~620–780 akcií projde filtrem, ~15–24 kandidátů; EU ~66–113 projde, ~21–25 kandidátů.

### Stíny, soubor data_6
- ORB15: 34 uzavřených, varianta A **−19,0 R**, varianta B −7,9 R; náklady tvořily velkou část rizika (jen 1 ze 34 prošel kontrolou nákladů).
- Pullback (9. 11., 1 den): koupit hned 12 obchodů +1,82 R; po poklesu 10 obchodů +0,18 R; časový pokles 6 obchodů +0,08 R.

## 3. Co už bylo otestováno a NEFUNGOVALO (neopakovat bez nového důkazu)
Backtesty 2013–2026 se započtením nákladů, kontrolou proti náhodným vstupům a českým daním:
- Krypto SFP (sweep/failure swing) za 30 dní −70,7 R vč. poplatků. 12 krypto strategií za 2 roky – vyhrál jen HIGH20.
- HIGH20 na indexech a kovech (NAS100, SP500, US2000, zlato, stříbro): po nákladech záporný.
- IBS na ETF (PF 0,89), efekt konce měsíce (~1 %/rok), dual momentum (10,9 %/rok vs index 13,5 %), PEAD jen z cen (PF 1,15).
- Akciový swing ~20 dní: průraz 52týdenního maxima (PF 1,20, slábne po 2020), pullback (PF ~1,0), po výsledcích EPS surprise (PF 1,75 v 2013–19, 0,99 od 2020), přepínání režimů (PF ≤ 1,0), shluky insiderů (PF 1,13 = náhoda), nákup po zprávách 8-K na konci dne (PF 0,77–0,91).
- Výběr akcií: reziduální momentum + FIP + nízká idiosynkratická volatilita (= náhoda), překvapení v tržbách/hrubém zisku (horší než náhoda), kvalitativní screen ROIC/FCF/F-score (= náhoda), short squeeze (netestovatelné). **Jediný úspěch: oportunistické nákupy CEO (PF 1,99, 102 obchodů) = stopa E.**
- Dlouhodobé investování 2007–2026: nejvíc vydělá prostý buy-and-hold S&P 500; HAA má poloviční drawdown, ale po daních méně.
- Rešerše jiných AI/LLM botů (2. 11.): žádný dlouho běžící LLM bot živě neporazil S&P 500 (AIEQ, Alpha Arena, ChatGPT micro-cap); LLM užitečné na čtení textu a veto rizika, ne na čísla; backtesty před datem znalostí modelu jsou kontaminované.

## 4. Omezení uživatele
- Účet T212 v EUR, plán ostrého startu 1000 Kč + 2000 Kč měsíčně. Česká daň (osvobození po 3 letech držení, limit prodejů 100 000 Kč/rok).
- Gemini na free tieru (denní limity na model). PC není zapnuté 24/7.
- Swing akcie se nesmí koupit a prodat týž den. Bot nikdy nezavírá ruční pozice uživatele. Bot má být plně autonomní.
- Uživatel chce zachovat design 4 agentů (skener, analytik, risk manager, exekutor).
