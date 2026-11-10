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
