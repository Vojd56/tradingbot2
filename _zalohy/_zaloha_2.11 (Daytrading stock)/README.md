# 🤖 Discord Trading Bot – 100% Autonomní Multi-Asset Trading Desk

Kompletní Python implementace Discord bota simulujícího 6členný institucionální trading tým (**Scout, News, Chart, Skeptik, Sizer, Autonomní Executor**) s **plně autonomním rozhodováním přes Gemini LLM, napojením na burzy Trading 212 & Binance Futures Testnet, implementací SFP strategie Jiřího Přibyla a ochranou proti plýtvání Gemini API kredity**.

---

## 👥 Role v Autonomním Týmu

| Role | Člen týmu | Odpovědnost a Pravidla |
| :--- | :--- | :--- |
| 🔭 **1. SCOUT** | Tržní detektor & Risk Monitor | • Sleduje 3 trhy: **US akcie**, **Evropské akcie** a **Kryptoměny 24/7**.<br>• **Ochrana kreditů:** Skenuje akcie pouze v jejich obchodních hodinách (EU 09:00–17:30, US 15:30–22:00 CET). O víkendech a nocích šetří kredity a sleduje výhradně krypto.<br>• **Aktivní SL/TP monitoring:** Každých 5 minut hlídá otevřené pozice a při zasažení SL/TP pozici automaticky uzavře na příslušné burze! |
| 📰 **2. NEWS** | Fundamentální analytik | Vysvětlí důvod pohybu ve 2 větách na základě zpráv. Pokud není jasný fundament, striktně *"Žádný jasný důvod"*. |
| 📊 **3. CHART** | Technický analytik | Spočítá Entry, Stop-Loss (striktně max 2 % pod entry) a Take-Profit (minimální poměr **RRR 1:2**). U krypta integruje **SFP Swing Failure Pattern** dle metodiky Jiřího Přibyla. |
| 🧐 **4. SKEPTIK** | Risk Officer | Najde 3 věcné důvody proti obchodu. Pokud News nenašel důvod, verdikt MUSÍ BÝT **"PRESKOČIŤ"**. Jinak **"KÚPIŤ"** / **"POČKAŤ"**. |
| 💰 **5. SIZER** | Money Manager | Alokuje max 5 % z $10,000 kapitálu (= $500). Dodržuje pravidlo rizika 1–2 % dle Přibyla. Max 5 otevřených pozic současně. |
| ⚡ **6. AUTONOMNÍ EXECUTOR** | Rozhodovatel & Exekuce | **100% autonomní bez nutnosti lidského schvalování.** LLM tým prodiskutuje fundament, techniku i rizika: pokud doporučí nákup, **bot samostatně odešle objednávku na Trading 212 (akcie) nebo Binance Futures (krypto)** a otevře pozici v evidenci. |

---

## 📈 Strategie Jiřího Přibyla: Intradenní Swing Failure Pattern (15M dno)

Bot integruje výzkum a metodiku z materiálu *Výzkum obchodování na finančních a krypto trzích (Jiří Přibyl)* přizpůsobenou pro **intradenní daytrading na 15minutovém časovém rámci**:
- **Identifikace likvidity (15M dno):** Hledání klíčového Swing Low na 15M svíčkách (úroveň, kde leží lokální stop-lossy retailových traderů za posledních 5–15 hodin).
- **Sweep knotem:** 15M svíčka prorazí pod 15M dno, ale uzavře tělem zpět **nad** tímto dnem (odmítnutí nižších cen).
- **Objemová absorpce:** Svíčka vykazuje nadprůměrný objem (> 1.15x 20periodového průměru na 15M), což potvrzuje nákupní absorpci velkých hráčů.
- **Parametry obchodu:**
  - **Entry:** Uzavírací cena potvrzující 15M svíčky.
  - **Stop-Loss:** Pod nejnižší knot sweepu s bufferem 0.05 %.
  - **Take-Profit 1:** Striktně poměr RRR 1:2 (realizace zisku + posun SL na Break-Even).
  - **Take-Profit 2:** Následující lokální 15M Swing High (vrchol).
  - **Risk management:** Risk 1–2 % kapitálu na obchod.

---

## ⏰ Obchodní Hodiny & Ochrana Proti Plýtvání Kredity

Bot respektuje časové pásmo uživatele **Europe/Prague (CET/CEST)**:
- 🇪🇺 **Evropské akcie (XETRA, Euronext):** Pondělí až Pátek **09:00 – 17:30 CET** (např. `SAP.DE`, `ASML.AS`, `SIE.DE`, `AIR.PA`).
- 🇺🇸 **Americké akcie (NYSE, NASDAQ):** Pondělí až Pátek **15:30 – 22:00 CET** (např. `NVDA`, `AAPL`, `TSLA`, `MSFT`, `AMZN`).
- ⚡ **Kryptoměny (Binance Futures):** **24/7/365 nepřetržitě** (`BTCUSDT`, `ETHUSDT`, `SOLUSDT`, `BNBUSDT`).
- 🛡️ **Ochrana kreditů:** Mimo obchodní hodiny akciových burz a o víkendech Scout akcie **vůbec neskenuje** a nevolá zbytečně Gemini API. Běží pouze nepřetržité sledování kryptoměn.

---

## 🎮 Dostupné Discord Slash Příkazy

| Příkaz | Popis |
| :--- | :--- |
| **`/scout <ticker>`** | Provede kompletní analýzu akcie (`NVDA`, `SAP.DE`) nebo kryptoměny (`BTCUSDT`, `ETHUSDT`) 6členným týmem a autonomně nakoupí. |
| **`/scan`** | Ručně proskenuje aktuálně otevřené trhy (s respektováním obchodních hodin) a autonomně otevře vhodné pozice. |
| **`/crypto`** | Proskenuje kryptoměny na Binance Futures a vyhodnotí SFP formace a likviditní knoty dle Jiřího Přibyla. |
| **`/hours`** | Zobrazí přehlednou kartu s aktuálním stavem burz (EU, US, Krypto) a ochranou kreditů v pražském čase. |
| **`/binance`** | Zobrazí zůstatek účtu, volnou marži a otevřené pozice na **Binance Futures Testnet**. |
| **`/t212`** | Zobrazí celkovou hodnotu, volnou hotovost a otevřené pozice na **Trading 212 Demo**. |
| **`/positions`** | Zobrazí všechny otevřené paper pozice bota spolu s živými portfolii z Trading 212 i Binance. |
| **`/close <ticker>`** | Manuální předčasné uzavření pozice (na burze i v evidenci). |
| **`/trades`** | Zobrazí historii schválených, zamítnutých a uzavřených obchodů. |
| **`/status`** | Přehled dnešních alertů, napojených účtů a konfigurace. |
| **`/help`** | Podrobná nápověda s vysvětlením rolí a metodiky. |

---

## ⚙️ Konfigurace a Spuštění

Všechny klíče se načítají z lokálních systémových proměnných nebo souboru `.env`:
```env
# Discord
DISCORD_BOT_TOKEN="tvuj_discord_token"
DISCORD_CHANNEL_ID="0"

# Google Gemini API
GEMINI_API_KEY="tvuj_gemini_api_key"
GEMINI_MODEL="gemini-2.5-flash"

# Trading 212 Demo
TRADING_212_API_KEY="tvuj_t212_api_key"
TRADING_212_SECRET_KEY="tvuj_t212_secret_key"
TRADING_212_ENV="demo"

# Binance Futures Testnet
BINANCE_API_KEY="tvuj_binance_api_key"
BINANCE_SECRET="tvuj_binance_secret"
BINANCE_FUTURES_URL="https://testnet.binancefuture.com"
```

### Spuštění bota
```bash
python bot.py
```
Nebo přes připravený skript `start_bot.bat`.

## Změny 26. 10. 2026 – ochrana kapitálu (audit 5.1–5.3)

Původní soubory před úpravami jsou ve složce `_zaloha_pred_upravami/`. Bot zůstává plně autonomní; schvalování na Discordu je v kódu, ale nepoužívá se.

### 1. Stop-Loss přímo na burze a ověřené uzavírání pozic
- Nový modul **`position_manager.py`** – jediná cesta pro otevření, uzavření a synchronizaci pozic (používá ji smyčka Scouta, `/scout`, `/close` i tlačítko „Uzavřít pozici předčasně“).
- **Binance Futures:** hned po nákupu se založí `STOP_MARKET` (SL) a `TAKE_PROFIT_MARKET` (TP) jako `reduceOnly` na množství bota, trigger podle mark price. Nejdřív přes Algo Order API (`/fapi/v1/algoOrder`), při nedostupnosti přes `/fapi/v1/order`. **Pokud SL založit nejde, bot pozici okamžitě zavře.** Tržní příkazy používají `newOrderRespType=RESULT`, takže bot zná skutečně vyplněné množství a cenu.
- **Trading 212:** po vyplnění nákupu se založí `STOP` příkaz (GOOD_TILL_CANCEL) na množství bota. TP hlídá bot softwarově (T212 nemá OCO). Pozn.: podle dokumentace T212 API podporuje na **live** účtu jen tržní příkazy – stop na burze funguje jen na demu.
- Pozice se v `paper_trades.json` označí jako **CLOSED až po potvrzení prodeje burzou**. Když prodej selže, zůstane OPEN, na Discord jde 🚨 a bot to zkusí znovu v dalším průchodu. Pokud selže nákup na T212, pozice se už nezapíše jako otevřená.
- Každý průchod smyčky zjistí, zda pozici nezavřel SL/TP příkaz na burze (pak ji uzavře v evidenci a zruší zbylé příkazy), a doplní chybějící Stop-Loss.

### 2. Bot nesahá na ruční pozice uživatele
- Bot prodává **jen množství, které sám koupil** (`bot_qty`). Ruční akcie ve stejném tickeru na T212 zůstanou.
- Na Binance bot neotevře obchod na symbolu, kde už je pozice, kterou neotevřel.
- Obchod bez order ID z burzy (nebyl na burze proveden) se uzavře jen v evidenci – nic se neprodává.

### 3. Zaokrouhlení cen (DOGE a levné coiny)
- Nový modul **`price_utils.py`** (`round_price`, `fmt_price`): počet desetinných míst podle řádu ceny místo pevných 2. Dřív u DOGE vycházel SL == Entry.
- Ceny pro příkazy na Binance se zaokrouhlují dolů na `tickSize` z `/fapi/v1/exchangeInfo` (`binance_client.format_price`).
- `gemini_team.py`: minimální riziko je 0,1 % z ceny místo pevného 1 centu.

### 4. Obchodní hodiny a konec seance
- **`market_hours.py`** přepsán: počítá v časovém pásmu burzy (America/New_York, Europe/Berlin) včetně svátků NYSE/XETRA/Euronext a zkrácených dnů NYSE.
- Vstupní okno (nové nákupy): US 09:45–15:30 New York, EU 09:15–17:00 Berlín.
- Flat-by: bot uzavře své akciové pozice v 15:50 New York (EU 17:20) a pozice držené přes noc po otevření další seance. Nastavení v `config.py` (`US_NO_ENTRIES_*`, `EU_NO_ENTRIES_*`, `US_FLAT_BY`, `EU_FLAT_BY`).
- T212 příkazy jdou jen v řádné seanci (`extendedHours=False`).

### Testy (offline, bez volání API)
- `python test_position_manager.py` – 15 scénářů (SL na burze, fallback, nouzové zavření, ruční pozice, selhání prodeje).
- `python test_market_hours.py` – 17 scénářů (posun času, svátky, zkrácené dny, flat-by).

### Známé otevřené body
- Evropské akcie se na T212 posílají se špatným tickerem (`SAP.DE_US_EQ`), nákup proto selže (bot to jen nahlásí).
- Executor v `gemini_team.py` může přebít Skeptika (podmínka OR); Time Machine propouští při chybě (fail-open) a nepočítá s poplatky/slippage.
- `config.py` obsahuje výchozí hodnoty Binance klíčů přímo v kódu – patří jen do `.env`.

### 5. Doplněno večer 26. 10. 2026
- `bot.py`: časový limit 45 s na ticker a 180 s na Gemini (jedno zaseknuté stažení už nezastaví smyčku), po každém průchodu řádek `📋 SCOUT SOUHRN` (prověřeno / bez signálu / obsazeno / signály / nakoupeno / důvody zamítnutí), log do `bot.log` (UTF-8, rotace 5 MB × 3).
- `scout.py`: oprava výpočtu objemu. Krypto: pohyb a objem za skutečných posledních 60 minut (1min svíčky) proti průměru dokončených hodin. Akcie: dnešní objem proti 20dennímu průměru přepočtenému na uplynulou část seance (min. 25 %). Dřív se objem podhodnocoval cca 2–4×.
- `kontrola_reality.py`: jednorázová kontrola (jen čtení) – porovná data Scouta s nezávislými daty Binance/Yahoo a vypíše pozice a ochranné příkazy na Binance testnetu a T212.
