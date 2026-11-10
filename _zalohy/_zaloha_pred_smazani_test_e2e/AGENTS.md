# Pokyny pro AI agenty (Claude, Codex a další)

## Restart bota po každé změně

Po KAŽDÉ změně kódu nebo konfigurace bota restartuje bota agent sám, uživatel to nedělá ručně:

1. Vypnout starou instanci: najít proces `python bot.py` spuštěný z této složky (okno „Discord Trading Bot (AIBot 2.0)“) a ukončit ho. Před spuštěním nové instance ověřit, že žádná stará neběží (vždy jen jedna instance).
2. Zapnout novou instanci v novém viditelném okně cmd: `start "Discord Trading Bot (AIBot 2.0)" cmd /k "cd /d C:\Users\vojte\TEst\zalohabot - kopie && start_bot.bat"`.
3. Ověřit v `bot.log`, že se bot přihlásil („Přihlášen jako …“) a že běží smyčka Scouta. Pak to uživateli potvrdit.

Před restartem pustit testy (`test_*.py`). Když neprojdou, nerestartovat a napsat uživateli, co selhalo.

## Další pravidla

- Upravuje se jen tato záložní kopie, nikdy `C:\AIBot2.0`.
- Bot nikdy nezavírá ani neprodává ruční pozice uživatele, jen kusy, které sám koupil.
- Bot je plně autonomní. Discordové schvalování zůstává v kódu, ale nepoužívá se.
- Před úpravou zazálohovat původní soubory do složky `_zaloha_pred_<změna>/`.
