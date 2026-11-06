# Pokyny pro AI agenty (Claude, Codex a další)

## Restart bota po každé změně

Po KAŽDÉ změně kódu nebo konfigurace bota restartuje bota agent sám, uživatel to nedělá ručně:

1. Vypnout starou instanci: najít proces `python bot.py` spuštěný z této složky (okno „Discord Trading Bot (AIBot 2.0)“) a ukončit ho. Před spuštěním nové instance ověřit, že žádná stará neběží (vždy jen jedna instance).
2. Zapnout novou instanci v novém viditelném okně cmd, .bat VŽDY plnou cestou: `cmd /k "C:\Users\<user>\TEst\zalohabot - kopie\start_bot.bat"` (z PowerShellu: `Start-Process cmd -ArgumentList '/k','"C:\Users\<user>\TEst\zalohabot - kopie\start_bot.bat"'`). Varianta `cd ... && start_bot.bat` selhává, když shell nastavuje `NoDefaultCurrentDirectoryInExePath=1`.
3. Ověřit v `bot.log`, že se bot přihlásil („Přihlášen jako …“) a že běží smyčka Scouta. Pak to uživateli potvrdit.

Před restartem pustit testy (`test_*.py`). Když neprojdou, nerestartovat a napsat uživateli, co selhalo.

## Trvalý souhlas uživatele (5. 11. 2026)

Uživatel trvale povolil (a nechce to znovu potvrzovat):
- Nasazení web dashboardu na Vercel produkci (`C:\Users\<user>\Downloads\tradingbot-dashboard`, `npx --yes vercel@latest deploy --prod --yes`, adresa tradebot2.vercel.app) po každé změně dashboardu.
- Odesílání stavu bota do jeho Supabase pro dashboard (web_export).
Na tyto kroky se uživatele znovu neptat, jen je provést a nahlásit výsledek. Před nasazením zálohovat soubory dashboardu do jeho složky `_zaloha_pred_<změna>`.

## Další pravidla

- Upravuje se jen tato záložní kopie, nikdy `C:\AIBot2.0`.
- Bot nikdy nezavírá ani neprodává ruční pozice uživatele, jen kusy, které sám koupil.
- Bot je plně autonomní. Discordové schvalování zůstává v kódu, ale nepoužívá se.
- Před úpravou zazálohovat původní soubory do složky `_zalohy/_zaloha_pred_<změna>/` (všechny zálohy jsou v `_zalohy/`).
