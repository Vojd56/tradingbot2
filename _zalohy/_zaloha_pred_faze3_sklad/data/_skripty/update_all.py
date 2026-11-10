"""Obnoví datový sklad bota a zapíše 00-STAV.md.

Spuštění (ze složky bota):  python data\\_skripty\\update_all.py   [--force] [--source yahoo_us] [--no-vault]
nebo dvojklikem na data\\aktualizovat_data.bat.

Co dělá:
1. Tickery bez souboru naplní ze starého stažení pro backtesty (ceny_*.pkl), pokud tam jsou.
2. U existujících souborů stáhne jen konec (posledních ~10 dní) a nové dny připojí.
3. Když se překryv liší (split, dividenda), stáhne ticker celý znovu; starý soubor předtím zazálohuje.
4. Tickery bez dat stáhne celé od START.
5. Zapíše data/_stav.json, data/00-STAV.md a poznámky ke zdrojům; kopii poznámek do vaultu.
Ticker zkontrolovaný dnes se přeskočí, takže druhý běh ve stejný den nic nestahuje (--force to vypne).
"""

import argparse
import json
import logging
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Dict, List

import sklad
import zdroj_binance
import kontrola
import zdroj_yahoo

BOT_DIR = sklad.DATA_DIR.parent
VAULT_DIR = Path(r"C:\Users\vojte\OneDrive\Dokumenty\my-vault") / "Projects" / "Trading bot data"
START = date(2024, 1, 1)
TAIL_DAYS = 10
STALE_BDAYS = 5
NODATA_RETRY_DAYS = 7    # ticker, který u zdroje vůbec nemá data, se zkusí znovu až po týdnu
SEED_DIR = BOT_DIR / "_analyza_swing" / "_analyza_drzeni"

logger = logging.getLogger("data")


# ------------------------------------------------------------------ seznamy trhů
def universe_us() -> List[str]:
    uni = json.loads((BOT_DIR / "swing_universe.json").read_text(encoding="utf-8"))["tickers"]
    return sorted(set(uni) | {"SPY"})


def universe_eu() -> List[str]:
    sys.path.insert(0, str(BOT_DIR))
    import swing_scanner   # stejný výběr jako bot (T212 + domácí burza podle ISIN)
    return sorted(set(swing_scanner.load_universe_eu()) | {"^STOXX"})


def universe_crypto() -> List[str]:
    sys.path.insert(0, str(BOT_DIR))
    from config import CRYPTO_WATCHLIST
    return list(CRYPTO_WATCHLIST)


SOURCES = {
    "yahoo_us": {"universe": universe_us, "fetch": zdroj_yahoo.fetch, "seed": SEED_DIR / "ceny_US.pkl",
                 "popis": "Yahoo Finance přes yfinance, US akcie S&P 1500 (swing vesmír bota) + SPY. Neoficiální zdroj."},
    "yahoo_eu": {"universe": universe_eu, "fetch": zdroj_yahoo.fetch, "seed": SEED_DIR / "ceny_EU.pkl",
                 "popis": "Yahoo Finance přes yfinance, evropské akcie obchodovatelné na T212 (Xetra, Paříž, "
                          "Amsterdam, Madrid, Curych) + ^STOXX. Neoficiální zdroj."},
    "binance_1d": {"universe": universe_crypto, "fetch": zdroj_binance.fetch, "seed": None,
                   "popis": "Binance Futures /fapi/v1/klines, denní svíčky 00:00–24:00 UTC, watchlist krypta bota."},
}


# ------------------------------------------------------------------ jeden zdroj
def update_source(name: str, symbols: List[str], fetch: Callable, seed, state: Dict, today: date,
                  force: bool = False) -> Dict[str, int]:
    stats = defaultdict(int)
    todo = []
    for s in symbols:
        st = state.get(sklad.key(name, s), {})
        if not force and st.get("checked") == today.isoformat() and st.get("ok"):
            stats["preskoceno"] += 1
        elif not force and st.get("retry_after", "") > today.isoformat():
            stats["odlozeno_bez_dat"] += 1
        else:
            todo.append(s)
    if not todo:
        return dict(stats)

    def done(s: str, ok: bool, action: str, err: str = "", retry_after: str = "") -> None:
        st = {"checked": today.isoformat(), "ok": ok, "action": action, "err": err}
        if retry_after:
            st["retry_after"] = retry_after
        st.update(sklad.describe(name, s))
        state[sklad.key(name, s)] = st
        stats[action] += 1

    # 1) naplnění ze starého stažení
    missing = [s for s in todo if not sklad.path_for(name, s).exists()]
    if missing and seed is not None and Path(seed).exists():
        logger.info(f"{name}: naplňuji {len(missing)} tickerů ze {Path(seed).name}")
        for s, df in zdroj_yahoo.from_seed_pickle(seed, missing, START).items():
            sklad.write_full(name, s, df, today)
            stats["naplneno"] += 1

    # 2) dotažení konce, skupiny podle posledního data
    full: List[str] = []
    groups: Dict[date, List[str]] = defaultdict(list)
    for s in todo:
        old = sklad.read(name, s)
        if old is None or old.empty:
            full.append(s)
        else:
            groups[old.index[-1].date() - timedelta(days=TAIL_DAYS)].append(s)
    for start, syms in sorted(groups.items()):
        logger.info(f"{name}: dotahuji konec pro {len(syms)} tickerů od {start}")
        got = fetch(syms, start, today)
        for s in syms:
            if s not in got:
                done(s, False, "chyba", "zdroj nevrátil data")
                continue
            res, n = sklad.merge_tail(name, s, got[s])
            if res in ("rewrite", "nooverlap"):
                full.append(s)
            else:
                done(s, True, "pridano" if res == "append" else "beze_zmeny")

    # 3) celé stažení (nové tickery a přepsaná historie)
    if full:
        logger.info(f"{name}: stahuji celou historii pro {len(full)} tickerů od {START}")
        got = fetch(full, START, today)
        for s in full:
            existed = sklad.path_for(name, s).exists()
            if s not in got:
                if existed:
                    done(s, False, "chyba", "zdroj nevrátil data (celé stažení)")
                else:   # zdroj ticker nezná: nezkoušet každý běh, jen jednou za týden
                    done(s, False, "bez_dat_u_zdroje", "zdroj ticker nemá",
                         (today + timedelta(days=NODATA_RETRY_DAYS)).isoformat())
                continue
            sklad.write_full(name, s, got[s], today)
            done(s, True, "prepsano_se_zalohou" if existed else "nove")
    return dict(stats)


# ------------------------------------------------------------------ výstupy
def source_summary(name: str, state: Dict, symbols: List[str], today: date) -> Dict:
    rows = [state.get(sklad.key(name, s), {}) for s in symbols]
    lasts = sorted(r["last"] for r in rows if r.get("last"))
    ref = today - timedelta(days=1)
    stale = [s for s, r in zip(symbols, rows)
             if r.get("last") and sklad.business_days_between(date.fromisoformat(r["last"]), ref) > STALE_BDAYS]
    errors = [(s, r.get("err") or "bez dat") for s, r in zip(symbols, rows)
              if not r.get("ok") and not r.get("retry_after")]
    nodata = [s for s, r in zip(symbols, rows) if not r.get("ok") and r.get("retry_after")]
    retry = min((r["retry_after"] for r in rows if not r.get("ok") and r.get("retry_after")), default=None)
    common = max(set(lasts), key=lasts.count) if lasts else None
    firsts = sorted(r["first"] for r in rows if r.get("first"))
    return {"trhu": len(symbols), "ok": sum(1 for r in rows if r.get("ok")), "chyb": len(errors),
            "posledni": common, "nejstarsi_posledni": lasts[0] if lasts else None,
            "od": firsts[0] if firsts else None, "radku": sum(r.get("rows", 0) for r in rows),
            "errors": errors, "stale": stale, "nodata": nodata, "retry": retry}


def write_reports(summaries: Dict[str, Dict], run_stats: Dict[str, Dict], vault: bool,
                  checks: Dict[str, Dict] = None) -> List[Path]:
    when = sklad.now_str()
    lines = ["---", f"aktualizováno: {when}", "tags: [trading-bot, data, stav]", "---", "# Stav tržních dat", "",
             f"Data leží ve složce bota `data\\`, obnova `data\\aktualizovat_data.bat`. Vytvořeno skriptem "
             f"`update_all.py` {when}. Každé tvrzení o ceně uvádí datum posledního řádku, ze kterého vychází.", "",
             "| zdroj | trhů | OK | chyba | zdroj nemá | poslední řádek (většina) | nejstarší poslední řádek | od | řádků |",
             "|---|---|---|---|---|---|---|---|---|"]
    for n, s in summaries.items():
        lines.append(f"| {n} | {s['trhu']} | {s['ok']} | {s['chyb']} | {len(s['nodata'])} | {s['posledni']} | "
                     f"{s['nejstarsi_posledni']} | {s['od']} | {s['radku']} |")
    lines += ["", "## Tento běh", ""]
    for n, st in run_stats.items():
        lines.append(f"- {n}: " + (", ".join(f"{k} {v}" for k, v in sorted(st.items())) or "nic"))
    lines += ["", f"## Chyby (max 30 na zdroj)", ""]
    any_err = False
    for n, s in summaries.items():
        for sym, err in s["errors"][:30]:
            lines.append(f"- {n} / {sym}: {err}")
            any_err = True
        if len(s["errors"]) > 30:
            lines.append(f"- {n}: … a dalších {len(s['errors']) - 30}")
    if not any_err:
        lines.append("Žádné.")
    lines += ["", "## Zdroj ticker nemá (další pokus po týdnu, max 30 na zdroj)", ""]
    if not any(s["nodata"] for s in summaries.values()):
        lines.append("Žádné.")
    for n, s in summaries.items():
        if s["nodata"]:
            lines.append(f"- {n} ({len(s['nodata'])}, nejbližší pokus {s['retry']}): " + ", ".join(s["nodata"][:30])
                         + (" …" if len(s["nodata"]) > 30 else ""))
    lines += ["", f"## Zastaralé (poslední řádek starší než {STALE_BDAYS} obchodních dní, max 30 na zdroj)", ""]
    any_stale = False
    for n, s in summaries.items():
        if s["stale"]:
            any_stale = True
            lines.append(f"- {n}: " + ", ".join(s["stale"][:30]) + (" …" if len(s["stale"]) > 30 else ""))
    if not any_stale:
        lines.append("Žádné.")
    if checks:
        lines += ["", "## Kontroly kvality", "",
                  "Seřazení, duplicitní dny, high ≥ max(open, close), low ≤ min(open, close), nulové ceny, mezery "
                  f"> {kontrola.GAP_BDAYS} obchodních dní (krypto > {kontrola.GAP_DAYS_24_7} dny), skok close > 50 %. "
                  "Data se neopravují, jen hlásí.", "",
                  "| zdroj | souborů | OK | z toho s upozorněním | s chybou | zkontrolováno v tomto běhu |",
                  "|---|---|---|---|---|---|"]
        for n, c in checks.items():
            lines.append(f"| {n} | {c['souboru']} | {c['ok']} | {c['upozorneni']} | {c['chyb']} | {c['zkontrolovano']} |")
        if any(c["problemy"] for c in checks.values()):
            lines += ["", "Nálezy (max 20 na zdroj):", ""]
        for n, c in checks.items():   # chyby první, pak upozornění
            items = sorted(c["problemy"].items(), key=lambda kv: (all(i.startswith("upozornění") for i in kv[1]), kv[0]))
            for sym, iss in items[:20]:
                lines.append(f"- {n} / {sym}: " + "; ".join(iss))
            if len(items) > 20:
                lines.append(f"- {n}: … a dalších {len(items) - 20}")
    files = {"00-STAV.md": "\n".join(lines) + "\n"}
    for n, s in summaries.items():
        files[f"{n}.md"] = "\n".join([
            "---", f"zdroj: {n}", f"aktualizováno: {when}", f"od: {s['od']}", f"do: {s['posledni']}",
            f"trhů: {s['trhu']}", f"řádků: {s['radku']}", "tags: [trading-bot, data]", "---",
            f"# Zdroj {n}", "", SOURCES[n]["popis"], "",
            f"Soubory: `data\\{n}\\<symbol>.csv`, jeden na trh, jen uzavřené dny. Zpět na [[00-STAV]].", "",
            "| sloupec | význam |", "|---|---|",
            "| date | den svíčky (u akcií den burzy, u krypta UTC) |",
            "| open, high, low, close | ceny; u akcií upravené o splity i dividendy jako `auto_adjust=True` v botu |",
            "| volume | objem (kusy, u krypta základní měna) |", "",
            "Po dividendě nebo splitu zdroj přepočítá celou historii; sklad to pozná a ticker stáhne znovu "
            "(starý soubor jde do `data\\_zalohy\\<den>\\`, zálohy se drží 14 dní).", ""])
    written = []
    targets = [sklad.DATA_DIR]
    if vault:
        if VAULT_DIR.parent.exists():
            VAULT_DIR.mkdir(exist_ok=True)
            targets.append(VAULT_DIR)
        else:
            logger.warning(f"Vault {VAULT_DIR.parent} nenalezen, poznámky jen do data\\")
    for d in targets:
        for fn, text in files.items():
            p = d / fn
            p.write_text(text, encoding="utf-8")
            written.append(p)
    return written


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true", help="zkontrolovat i tickery kontrolované dnes")
    ap.add_argument("--source", choices=sorted(SOURCES), action="append", help="jen tento zdroj (lze opakovat)")
    ap.add_argument("--no-vault", action="store_true", help="nezapisovat poznámky do vaultu")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(),
                                  logging.FileHandler(sklad.DATA_DIR / "_update.log", encoding="utf-8")])
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    today = date.today()
    state = sklad.load_state()
    summaries, run_stats = {}, {}
    for name, cfg in SOURCES.items():
        try:
            symbols = cfg["universe"]()
        except Exception as e:
            logger.error(f"{name}: seznam trhů nejde načíst: {e}")
            run_stats[name] = {"chyba_seznamu": 1}
            continue
        if not args.source or name in args.source:
            try:
                run_stats[name] = update_source(name, symbols, cfg["fetch"], cfg["seed"], state, today, args.force)
            except Exception as e:
                logger.exception(f"{name}: obnova selhala: {e}")
                run_stats[name] = {"selhalo": 1}
            sklad.save_state(state)
        summaries[name] = source_summary(name, state, symbols, today)
    checks = kontrola.check_all(state, list(SOURCES))
    sklad.save_state(state)
    sklad.prune_backups(today)
    for p in write_reports(summaries, run_stats, vault=not args.no_vault, checks=checks):
        logger.info(f"zapsáno {p}")
    for name, st in run_stats.items():
        logger.info(f"{name}: {st}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
