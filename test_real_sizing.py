"""Testy velikosti akciových obchodů ze skutečného zůstatku T212 (trading212.compute_stock_qty)."""

import sys

from trading212 import compute_stock_qty

failures = 0


def check(name, cond, detail=""):
    global failures
    if cond:
        print(f"PASS {name}")
    else:
        failures += 1
        print(f"FAIL {name} {detail}")


# 1 165 CZK, akcie za 700 USD, kurz CZK->USD 0,043: 20 % = 233 CZK ≈ 10,02 USD -> 0,0143 -> 0,01 akcie
r = compute_stock_qty(1165.0, 1165.0, 700.0, 0.043, 20.0, 2.0)
check("malý účet: alokace 20 % hodnoty", abs(r["alloc_acct"] - 233.0) < 1e-6, r)
check("malý účet: akcie za 700 USD -> 0,01 ks", r["shares"] == 0.01, r)
# Stejný účet, akcie za 1 500 USD: 10,02 / 1500 = 0,0067 -> 0 akcií -> nákup se zablokuje
r = compute_stock_qty(1165.0, 1165.0, 1500.0, 0.043, 20.0, 2.0)
check("malý účet: akcie za 1 500 USD -> 0 ks, zablokováno", r["shares"] == 0.0, r)

# 250 000 CZK: 50 000 CZK * 0,043 = 2 150 USD / 700 = 3,0714 -> 3,07 akcie
r = compute_stock_qty(250000.0, 250000.0, 700.0, 0.043, 20.0, 2.0)
check("velký účet: alokace 50 000 CZK", abs(r["alloc_acct"] - 50000.0) < 1e-6, r)
check("velký účet: 3,07 akcie", r["shares"] == 3.07, r)

# Volná hotovost menší než 20 % strop: 10 000 CZK volných, 98 % = 9 800 CZK
r = compute_stock_qty(250000.0, 10000.0, 700.0, 0.043, 20.0, 2.0)
check("strop volnou hotovostí", abs(r["alloc_acct"] - 9800.0) < 1e-6, r)
check("strop volnou hotovostí: 0,6 akcie", r["shares"] == 0.6, r)  # 9800*0.043/700 = 0.602

# Stejná měna (fx = 1): 1 000 USD účet, 20 % = 200 USD, cena 50 -> 4 akcie
r = compute_stock_qty(1000.0, 1000.0, 50.0, 1.0, 20.0, 2.0)
check("stejná měna: 4 akcie", r["shares"] == 4.0, r)

# Zaokrouhlení dolů na 0,01 a ochrana proti chybě plovoucí čárky
r = compute_stock_qty(100.0, 100.0, 69.0, 1.0, 20.0, 0.0)  # 20/69 = 0.2898 -> 0.28
check("zaokrouhlení dolů", r["shares"] == 0.28, r)
r = compute_stock_qty(145.0, 145.0, 100.0, 1.0, 20.0, 0.0)  # 29/100 = 0.29 přesně
check("přesná hodnota 0,29 se nezaokrouhlí na 0,28", r["shares"] == 0.29, r)

# Nulová volná hotovost / neplatná cena
check("bez volné hotovosti", compute_stock_qty(1000.0, 0.0, 50.0, 1.0, 20.0, 2.0)["shares"] == 0.0)
check("nulová cena", compute_stock_qty(1000.0, 1000.0, 0.0, 1.0, 20.0, 2.0)["shares"] == 0.0)

if failures:
    print(f"{failures} FAILED")
    sys.exit(1)
print("ALL OK")
