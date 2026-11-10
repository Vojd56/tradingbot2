# Prompt pro Gemini: rozbor výsledků trading bota + rešerše AI trading botů

Jak použít:
1. V aplikaci Gemini zapni **Deep Research** (nebo model Pro s funkcí Hledat).
2. Přilož soubory ze složky `gemini-rozbor/` (8 souborů): `00_souhrn_bota.md` a `data_1` až `data_7` (CSV, oddělovač středník). Pokud Deep Research nepovolí tolik příloh, přilož aspoň `00_souhrn_bota.md`, `data_1`, `data_2`, `data_3` a `data_4`. Všechny soubory jsou i na GitHubu: https://github.com/Vojd56/tradingbot2/tree/main/gemini-rozbor
3. Vlož celý text pod čarou.

---

Jsi zkušený kvantitativní trader a výzkumník algoritmického obchodování. Kriticky a poctivě posoudíš výsledky mého autonomního trading bota a pak uděláš rešerši na internetu. Piš česky, srozumitelně pro začátečníka, ale s čísly.

## Kontext
- Můj bot je v Pythonu a běží na mém PC s Windows. Postup: **skener → analytik (LLM Gemini) → risk manager (kód) → exekutor (kód)**. Obchoduje na **Trading 212 demo (účet v EUR)** a **Binance Futures testnet**. Vše je zatím DEMO.
- Popis všech strategií, pravidel, výsledků a toho, co už jsem testoval a nefungovalo, je v přiloženém souboru **00_souhrn_bota.md**. Přečti ho celý jako první.
- Surová data jsou v přílohách (data k 10. 11. 2026):
  - `data_1_intradenni_obchody_demo.csv` – všech 84 živých demo obchodů (akcie Scout + krypto), vstup, stop, cíl, výsledek, důvod výstupu, zdůvodnění Gemini a námitky skeptika.
  - `data_2_swing_pozice.csv` – swing pozice stop A (Gemini), B (bez Gemini), C (náhoda), E (insideři) a živé stopy A na T212 demo, vč. MFE/MAE v R.
  - `data_3_swing_rozhodnuti.csv` – hlasování Gemini o nákupech (důvody a protiargumenty), Lite tým D, večerní kontroly držení, zrušené nákupy a výpadky.
  - `data_4_krypto_stara_metoda.csv` – papírové krypto obchody „staré metody“ (Gemini tým).
  - `data_5_krypto_high20_dny.csv` – denní kontroly krypto strategie HIGH20.
  - `data_6_stiny_orb_pullback.csv` – stínové testy ORB15 a pullback.
  - `data_7_scout_signaly.csv` – všechny signály Scoutu (koupit / přeskočit / nákup selhal) s verdiktem Gemini.
- Celý zdrojový kód bota, logy a stav jsou veřejně na GitHubu: **https://github.com/Vojd56/tradingbot2** (hlavní soubory: `config.py`, `scout.py`, `gemini_team.py`, `momentum_scanner.py`, `swing_rules.py`, `swing_scanner.py`, `swing_analyst.py`, `swing_live.py`, `swing_shadow.py`, `daily_breakout.py`, `risk_guard.py`, `position_manager.py`; složka `_analyza_swing/` obsahuje výsledky backtestů). Pokud kód dokážeš přečíst, ověř pravidla přímo v něm.

## Moje omezení
- Malý účet: ostrý start plánuji s 1000 Kč + 2000 Kč měsíčně na Trading 212 (EUR účet, přepočet měn ~0,15 % na stranu).
- Česká daň: zisk z akcií je osvobozen po 3 letech držení nebo když prodeje za rok nepřesáhnou 100 000 Kč.
- Gemini API mám jen na free tieru (denní limity na model, občas chyba 503). PC neběží 24/7.
- Swing akcie se nesmí koupit a prodat v jeden den. Bot má být plně autonomní a nikdy nesmí zavřít moje ruční pozice.
- Chci zachovat design 4 agentů (skener, analytik, risk manager, exekutor).

## Úkol 1: Celkový verdikt
1. Co si o výsledcích myslíš? Ke každé části (intradenní akcie Scout, swing stopy A/B/C/E, krypto HIGH20, krypto „stará metoda“, stíny ORB a pullback) napiš verdikt: **pokračovat / upravit / vypnout** a proč.
2. Vždy řekni, jestli je vzorek dost velký na závěr. Spočítej nebo odhadni, kolik obchodů je potřeba, aby byl výsledek statisticky průkazný (např. pro PF 1,3 vs 1,0), a podle toho rozliš „jasný závěr“ a „zatím jen náznak“.
3. Porovnej stopu A (Gemini) se stopou B (stejný skener bez Gemini), C (náhoda) a s indexem. Přidává Gemini zatím nějakou hodnotu, nebo jen stojí API volání?

## Úkol 2: Kde bot rozhodl špatně
Projdi data obchod po obchodu a najdi:
- konkrétní obchody, kde bot rozhodl špatně (uveď ID / ticker / datum z dat a proč to byla chyba),
- **opakující se vzory chyb** (např. stop příliš blízko vzhledem k volatilitě, nákup po velkém pohybu nahoru, nákup bez skutečného katalyzátoru, špatná denní doba, náklady velké vůči riziku, chyby kvůli nedostatku hotovosti, pojistka z jedné strategie blokuje jinou strategii),
- místa, kde Gemini napsal přesvědčivé zdůvodnění, ale obchod skončil ztrátou, a jestli se to dalo poznat dopředu (např. námitky skeptika, které se potvrdily),
- technické a provozní chyby (zrušené nákupy, výpadky Gemini, limity pozic a sektorů, které zablokovaly dobré obchody).
U každého vzoru uveď, kolik obchodů a kolik USD/EUR nebo R to stálo, a navrhni konkrétní opravu jako pravidlo, které jde napsat do kódu.

## Úkol 3: Kde bot rozhodl dobře
- Které obchody a rozhodnutí byly dobré (včetně dobrých **ne**rozhodnutí, např. přeskočení signálu, který by prodělal, nebo veto skeptika)?
- Co mají společného výherní obchody (katalyzátor, typ zprávy, denní doba, objem, trh, sektor, vzdálenost stopu)?
- Které části bota bys určitě zachoval?

## Úkol 4: Nové metody výběru (co nejvyšší úspěšnost)
Navrhni **5–10 metod nebo filtrů**, podle kterých by bot měl vybírat obchody, aby byl co nejúspěšnější. Podmínky:
- **Neopakuj** metody ze sekce 3 v `00_souhrn_bota.md`, které už neprošly, pokud nemáš nový silný důkaz (pak ho uveď a vysvětli, proč by to tentokrát mělo vyjít).
- Ke každé metodě uveď: přesné pravidlo (vstup, stop, výstup, velikost pozice, trh, časový rámec), zdroj (studie, kniha, ověřený výsledek), jaké výsledky má po započtení nákladů a jestli funguje i po roce 2020, jaká data potřebuju (zdarma dostupná?) a jak ji otestovat proti náhodnému výběru.
- Zvaž i filtry pro stávající strategie (např. kdy vůbec neobchodovat, filtr režimu trhu, filtr kvality katalyzátoru, filtr nákladů).
- Řekni, kde má smysl LLM (Gemini) a kde ne. Kde má LLM rozhodovat, kde jen dávat veto a kde je lepší čistý kód.
- Seřaď návrhy podle očekávaného přínosu a jednoduchosti. Na konec dej každý návrh i jako YAML:

```yaml
- nazev: ...
  trh: US akcie | EU akcie | krypto
  casovy_ramec: ...
  vstup: ...
  stop: ...
  vystup: ...
  velikost: ...
  role_llm: zadna | veto | klasifikace zprav | rozhodnuti
  data: ...
  zdroj: ...
  ocekavany_PF_po_nakladech: ...
  jak_testovat: ...
```

## Úkol 5: Rešerše na internetu – AI trading boti
Najdi a projdi co nejvíc o AI a LLM trading botech:
- **Open-source projekty** (např. TradingAgents, FinRL, FinGPT, FinMem, FinAgent, AI Hedge Fund, Freqtrade + FreqAI, Jesse, Hummingbot, NautilusTrader, Lean/QuantConnect a další): co dělají, jaké strategie a pravidla používají, jaké mají **ověřené** výsledky (živě vs. backtest).
- **Veřejné soutěže a živé testy LLM** (např. Alpha Arena, StockBench, InvestorBench, živé portfolia GPT/Claude/Gemini, ETF řízené AI jako AIEQ): kdo vydělal, kdo prodělal a proč.
- **Vědecké studie 2023–2026** o LLM v obchodování: kde LLM pomáhá (čtení zpráv, sentiment, klasifikace událostí), kde škodí (čísla, halucinace, look-ahead kontaminace backtestů).
- **Pravidla a osvědčené postupy**, které úspěšné boty spojují: řízení rizika, velikost pozice, pojistky, kdy bot vypnout, jak se vyhnout přeučení a zkreslení backtestu, jak správně porovnávat s náhodou a s indexem.
- Co z toho je realistické pro **malý účet v EUR na Trading 212** a free tier Gemini.
Pro každé tvrzení uveď zdroj s odkazem a datem. Odděl ověřené výsledky (auditované, živé obchody) od marketingu a tvrzení bez důkazu.

## Pravidla odpovědi
- Nic si nevymýšlej. Když něco v datech nenajdeš nebo to nejde spočítat, napiš to.
- Ke každému číslu z mých dat uveď, z kterého souboru pochází. Ceny a pohyby uváděj s datem.
- Odděluj **fakta** (data, zdroje) od **tvých názorů a odhadů**.
- Buď kritický. Raději mi řekni, že něco nefunguje a mám to vypnout, než abys mě uklidňoval.

## Formát výstupu
1. **Shrnutí na 10 řádků** – nejdůležitější závěry a 3 kroky, které mám udělat hned.
2. Verdikt po strategiích (tabulka: strategie | obchodů | výsledek | průkazné? | verdikt | proč).
3. Špatná rozhodnutí (tabulka vzorů + konkrétní příklady + oprava).
4. Dobrá rozhodnutí.
5. Nové metody výběru seřazené podle přínosu + YAML.
6. Rešerše AI trading botů (projekty, soutěže, studie, pravidla) se zdroji.
7. Doporučený plán na 4–8 týdnů: co vypnout, co upravit, co přidat jako stínový test a podle čeho po týdnech poznám, že to funguje.
