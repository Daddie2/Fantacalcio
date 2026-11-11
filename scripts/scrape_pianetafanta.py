"""
Scarica da PianetaFanta le statistiche casa/fuori di ogni giocatore del listone.
Input : data/quotazioni.csv  (serve prima scrape_fantacalcio.py)
Output: data/stats_pianetafanta.csv  con le colonne che si aspetta index.html:
        nome, squadra, golcasa, golfuori, assistcasa, assistfuori, ammcasa, ammfuori, fmcasa, fmfuori
Se qualcosa non torna, a fine esecuzione stampa quanti giocatori sono falliti e perche',
e salva una pagina d'esempio in data/debug_pianetafanta.html.
"""
import csv
import json
import os
import re
import sys
import time
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "it-IT,it;q=0.9",
}
BASE = "https://www.pianetafanta.it/giocatori/"
FIELDS = ["nome", "squadra", "golcasa", "golfuori", "assistcasa", "assistfuori", "ammcasa", "ammfuori", "fmcasa", "fmfuori"]
NEXT_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL)

fail = Counter()          # motivo -> quanti giocatori
debug_saved = False


def make_slug(name):
    s = unicodedata.normalize("NFD", name.lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")     # via gli accenti
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def norm_team(s):
    s = unicodedata.normalize("NFD", str(s or "").upper())
    s = "".join(c for c in s if c.isalpha())
    return s


def same_team(a, b):
    """'ROM' e 'ROMA' sono la stessa squadra: confronto sulle prime 3 lettere."""
    a, b = norm_team(a), norm_team(b)
    if not a or not b:
        return True
    return a == b or a[:3] == b[:3]


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0


def get_data(session, name, team):
    global debug_saved
    url = BASE + make_slug(name)
    try:
        resp = session.get(url, timeout=15)
    except requests.RequestException:
        fail["errore di rete"] += 1
        return None
    if resp.status_code == 404:
        fail["pagina non trovata (404): nome/slug diverso"] += 1
        return None
    if resp.status_code != 200:
        fail[f"HTTP {resp.status_code}"] += 1
        return None
    if not debug_saved:
        debug_saved = True
        with open("data/debug_pianetafanta.html", "w", encoding="utf-8") as f:
            f.write(resp.text)
    m = NEXT_RE.search(resp.text)
    if not m:
        fail["manca __NEXT_DATA__ (la pagina non lo contiene)"] += 1
        return None
    try:
        props = json.loads(m.group(1)).get("props", {}).get("pageProps", {})
    except ValueError:
        fail["JSON non leggibile"] += 1
        return None
    sq = props.get("squadra")
    if sq and not same_team(sq, team):
        fail["squadra diversa (omonimo?)"] += 1
        return None
    casa, fuori = props.get("statCasa") or {}, props.get("statFuori") or {}
    if not isinstance(casa, dict) or not isinstance(fuori, dict) or not (casa or fuori):
        fail["statCasa/statFuori assenti o con altro formato"] += 1
        return None
    fm = lambda d: round(num(d.get("MFG")), 2) if d.get("MFG") else ""
    return {
        "nome": name, "squadra": team,
        "golcasa": casa.get("GoalReal", 0), "golfuori": fuori.get("GoalReal", 0),
        "assistcasa": casa.get("Assist", 0), "assistfuori": fuori.get("Assist", 0),
        "ammcasa": casa.get("Amm", 0), "ammfuori": fuori.get("Amm", 0),
        "fmcasa": fm(casa), "fmfuori": fm(fuori),
    }


def main():
    os.makedirs("data", exist_ok=True)
    quot = "data/quotazioni.csv"
    if not os.path.exists(quot):
        print(f"ERRORE: {quot} non trovato. Esegui prima scrape_fantacalcio.py", file=sys.stderr)
        sys.exit(1)
    with open(quot, encoding="utf-8") as f:
        players = [{"nome": r["nome"], "squadra": r["squadra"]} for r in csv.DictReader(f) if r.get("nome") and r.get("squadra")]
    print(f"Trovati {len(players)} giocatori. Scarico da PianetaFanta...")

    session = requests.Session()
    session.headers.update(HEADERS)
    adapter = requests.adapters.HTTPAdapter(max_retries=requests.adapters.Retry(
        total=2, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504]))
    session.mount("https://", adapter)

    results = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = [ex.submit(get_data, session, p["nome"], p["squadra"]) for p in players]
        for i, fut in enumerate(as_completed(futures), 1):
            r = fut.result()
            if r:
                results.append(r)
            if i % 100 == 0:
                print(f"  {i}/{len(players)} (riusciti: {len(results)})")
            time.sleep(0.05)

    print(f"Riusciti: {len(results)}/{len(players)}")
    for motivo, n in fail.most_common():
        print(f"  falliti per {motivo}: {n}")
    if not results:
        print("ATTENZIONE: nessun dato estratto. Guarda data/debug_pianetafanta.html.", file=sys.stderr)
        sys.exit(1)
    results.sort(key=lambda r: (r["squadra"], r["nome"]))
    with open("data/stats_pianetafanta.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(results)
    print(f"OK: data/stats_pianetafanta.csv ({len(results)} giocatori)")


if __name__ == "__main__":
    main()
