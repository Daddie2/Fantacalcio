"""
Scarica da PianetaFanta le statistiche casa/fuori di ogni giocatore del listone.
Input : data/quotazioni.csv  (serve prima scrape_fantacalcio.py)
Output: data/stats_pianetafanta.csv  con le colonne che si aspetta index.html:
        nome, squadra, golcasa, golfuori, assistcasa, assistfuori, ammcasa, ammfuori, fmcasa, fmfuori

Novita' rispetto alla versione precedente:
- se la pagina non ha __NEXT_DATA__ (Next.js con App Router), cerca "statCasa"/"statFuori"
  nei blocchi self.__next_f.push(...) o direttamente nell'HTML;
- alla prima pagina senza dati stampa nel log una riga [diag] (titolo, dimensione, presenza di
  __NEXT_DATA__ / __next_f / statCasa) e salva l'HTML in data/debug_pianetafanta.html;
- per ogni motivo di fallimento stampa fino a 12 nomi di esempio.
"""
import csv
import json
import os
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "it-IT,it;q=0.9",
}
BASE = "https://www.pianetafanta.it/giocatori/"
FIELDS = ["nome", "squadra", "golcasa", "golfuori", "assistcasa", "assistfuori", "ammcasa", "ammfuori", "fmcasa", "fmfuori"]
NEXT_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL)
RSC_RE = re.compile(r'self\.__next_f\.push\(\[\d+,"(.*?)"\]\)', re.DOTALL)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)

fail = Counter()                 # motivo -> quanti giocatori
examples = defaultdict(list)     # motivo -> alcuni nomi di esempio
debug_saved = False


def record_fail(reason, name):
    fail[reason] += 1
    if len(examples[reason]) < 12:
        examples[reason].append(name)


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


def find_obj(text, key):
    """Cerca '"key":{...}' dentro un testo e ritorna l'oggetto JSON (dict) oppure None."""
    needle = '"%s":' % key
    i = text.find(needle)
    while i >= 0:
        k = i + len(needle)
        while k < len(text) and text[k] == " ":
            k += 1
        if k < len(text) and text[k] == "{":
            try:
                obj, _ = json.JSONDecoder().raw_decode(text, k)
                if isinstance(obj, dict):
                    return obj
            except ValueError:
                pass
        i = text.find(needle, i + 1)
    return None


def extract_props(html):
    """Ritorna (props, via) con props = {'squadra', 'statCasa', 'statFuori'} oppure (None, None)."""
    m = NEXT_RE.search(html)
    if m:
        try:
            props = json.loads(m.group(1)).get("props", {}).get("pageProps", {})
            if props:
                return props, "pages-router"
        except ValueError:
            pass

    # Next.js App Router: i dati stanno nei blocchi self.__next_f.push([1,"..."])
    chunks = []
    for c in RSC_RE.findall(html):
        try:
            chunks.append(json.loads('"' + c + '"'))
        except ValueError:
            continue
    blob = "".join(chunks)
    casa = find_obj(blob, "statCasa") if blob else None
    fuori = find_obj(blob, "statFuori") if blob else None
    if casa is None and fuori is None:
        casa, fuori = find_obj(html, "statCasa"), find_obj(html, "statFuori")
    if casa is not None or fuori is not None:
        return {"squadra": None, "statCasa": casa or {}, "statFuori": fuori or {}}, "app-router"
    return None, None


def diag(url, resp):
    html = resp.text
    t = TITLE_RE.search(html)
    title = t.group(1).strip()[:80] if t else None
    print(f"[diag] {url} status={resp.status_code} bytes={len(html)} title={title!r} "
          f"__NEXT_DATA__={'__NEXT_DATA__' in html} __next_f={'__next_f' in html} "
          f"statCasa={'statCasa' in html} GoalReal={'GoalReal' in html}", file=sys.stderr)


def get_data(session, name, team):
    global debug_saved
    url = BASE + make_slug(name)
    try:
        resp = session.get(url, timeout=15)
    except requests.RequestException:
        record_fail("errore di rete", name)
        return None
    if resp.status_code == 404:
        record_fail("pagina non trovata (404): nome/slug diverso", name)
        return None
    if resp.status_code != 200:
        record_fail(f"HTTP {resp.status_code}", name)
        return None

    props, via = extract_props(resp.text)
    if props is None:
        if not debug_saved:
            debug_saved = True
            with open("data/debug_pianetafanta.html", "w", encoding="utf-8") as f:
                f.write(resp.text)
            diag(url, resp)
        record_fail("nessun JSON con statCasa/statFuori nella pagina", name)
        return None

    sq = props.get("squadra")
    if sq and not same_team(sq, team):
        record_fail("squadra diversa (omonimo?)", name)
        return None
    casa, fuori = props.get("statCasa") or {}, props.get("statFuori") or {}
    if not isinstance(casa, dict) or not isinstance(fuori, dict) or not (casa or fuori):
        record_fail("statCasa/statFuori assenti o con altro formato", name)
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
    print(f"Trovati {len(players)} giocatori. Scarico da PianetaFanta...", flush=True)

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
                print(f"  {i}/{len(players)} (riusciti: {len(results)})", flush=True)
            time.sleep(0.05)

    print(f"Riusciti: {len(results)}/{len(players)}", flush=True)
    for motivo, n in fail.most_common():
        print(f"  falliti per {motivo}: {n}", flush=True)
        print(f"     esempi: {', '.join(examples[motivo])}", flush=True)
    if not results:
        print("ATTENZIONE: nessun dato estratto. Guarda la riga [diag] e data/debug_pianetafanta.html.", file=sys.stderr)
        sys.exit(1)
    results.sort(key=lambda r: (r["squadra"], r["nome"]))
    with open("data/stats_pianetafanta.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(results)
    print(f"OK: data/stats_pianetafanta.csv ({len(results)} giocatori)")


if __name__ == "__main__":
    main()
