"""
Scarica da PianetaFanta le statistiche casa/fuori di ogni giocatore del listone.
Input : data/quotazioni.csv       (serve prima scrape_fantacalcio.py)
        data/slug_override.csv    (opzionale, a mano: nome,slug - ha la precedenza su tutto)
Output: data/stats_pianetafanta.csv  con le colonne che si aspetta index.html:
        nome, squadra, golcasa, golfuori, assistcasa, assistfuori, ammcasa, ammfuori, fmcasa, fmfuori
        data/slug_trovati.csv        cache automatica degli slug scoperti (nome,slug)
        data/slug_mancanti.csv       nomi che nemmeno la ricerca automatica ha risolto

Come trova gli slug:
- PianetaFanta usa cognome + iniziali del nome per distinguere gli omonimi (pessina-m, pessina-ma).
  Per ogni nome che non risponde con lo slug "ovvio" lo script prova: cognome + iniziali date dal
  listone (Pessina Mas. -> pessina-mas, pessina-ma, pessina-m), poi cognome + ogni lettera a-z,
  poi il solo cognome.
- Ogni pagina viene accettata solo se la squadra scritta nella pagina coincide con quella del
  listone. Le varianti non "ovvie" richiedono sempre la squadra verificata, cosi' gli omonimi
  non entrano nel CSV.
- Gli slug scoperti vengono salvati in data/slug_trovati.csv e riusati ai run successivi.
"""
import csv
import html as htmllib
import json
import os
import re
import string
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from urllib3.util.retry import Retry

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "it-IT,it;q=0.9",
}
BASE = "https://www.pianetafanta.it/giocatori/"
FIELDS = ["nome", "squadra", "golcasa", "golfuori", "assistcasa", "assistfuori", "ammcasa", "ammfuori", "fmcasa", "fmfuori"]
NEXT_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL)
RSC_RE = re.compile(r'self\.__next_f\.push\(\[\d+,"(.*?)"\]\)', re.DOTALL)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)
TEAM_RE = re.compile(r"([^\n·]{2,40}?)\s*·\s*(?:portiere|difensore|centrocampista|attaccante)", re.IGNORECASE)

OVERRIDE_PATH = "data/slug_override.csv"
CACHE_PATH = "data/slug_trovati.csv"
MISSING_PATH = "data/slug_mancanti.csv"

fail = Counter()                 # motivo -> quanti giocatori
examples = defaultdict(list)     # motivo -> alcuni nomi di esempio
missing = []                     # (nome, squadra) non risolti
unverified = []                  # nomi accettati senza poter verificare la squadra
found_slugs = {}                 # nome -> slug scoperto in questo run
OVERRIDES = {}
CACHE = {}
debug_saved = False


def record_fail(reason, name):
    fail[reason] += 1
    if len(examples[reason]) < 12:
        examples[reason].append(name)


def make_slug(name):
    s = unicodedata.normalize("NFD", name.lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")     # via gli accenti
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def load_slug_csv(path, target):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            nome, slug = (r.get("nome") or "").strip(), (r.get("slug") or "").strip()
            if nome and slug:
                target[nome] = slug.strip("/").split("/")[-1].split("?")[0]   # accetta anche l'URL intero


def split_name(name):
    """'Esposito F.P.' -> ('Esposito', 'fp'); 'Pessina Mas.' -> ('Pessina', 'mas'); 'N'Dicka' -> ("N'Dicka", '')."""
    clean = name.replace("*", "").strip()
    toks = clean.split()
    ini = ""
    while len(toks) > 1 and (toks[-1].endswith(".") or (len(toks[-1]) <= 2 and toks[-1][0].isupper())):
        ini = re.sub(r"[^A-Za-z]", "", toks.pop()).lower() + ini
    return " ".join(toks), ini


def slug_candidates(name):
    """Lista di (slug, sicuro). 'sicuro' = ok anche se la squadra non e' verificabile."""
    out, seen = [], set()

    def add(slug, safe=False, raw=False):
        s = slug if raw else make_slug(slug)
        if s and s not in seen:
            seen.add(s)
            out.append((s, safe))

    if name in OVERRIDES:
        add(OVERRIDES[name], safe=True, raw=True)
    if name in CACHE:
        add(CACHE[name], safe=True, raw=True)

    clean = name.replace("*", "").strip()
    sur, ini = split_name(name)
    add(clean, safe=True)
    add(clean.replace("'", "").replace("’", ""), safe=True)
    add(re.sub(r"[-'’]", "", clean), safe=True)

    # varianti del solo cognome (base per iniziali e scansione)
    bases = []
    for b in (make_slug(sur), make_slug(re.sub(r"['’]", "", sur)), make_slug(re.sub(r"[-'’]", "", sur))):
        if b and b not in bases:
            bases.append(b)
    words = sur.split()
    if len(words) > 1:
        for w in (words[-1], words[0]):
            b = make_slug(w)
            if b and b not in bases:
                bases.append(b)

    # cognome + iniziali del listone, dalla piu' lunga alla piu' corta
    if ini:
        for b in bases:
            for k in range(len(ini), 0, -1):
                add(f"{b}-{ini[:k]}")

    # cognome da solo
    for b in bases:
        add(b)

    # scansione cognome + lettera (le prime due basi, per limitare le richieste)
    for b in bases[:2]:
        if ini:
            for c in string.ascii_lowercase:
                add(f"{b}-{ini[0]}{c}")
        else:
            for c in string.ascii_lowercase:
                add(f"{b}-{c}")
    return out


def norm_team(s):
    s = unicodedata.normalize("NFD", str(s or "").upper())
    return "".join(c for c in s if c.isalpha())


def team_ok(page_team, team):
    """True/False se confrontabile, None se la squadra della pagina non e' nota."""
    a, b = norm_team(page_team), norm_team(team)
    if not a or not b:
        return None
    return a[:3] == b[:3] or a in b or b in a


def page_team(html):
    """Squadra dall'intestazione della pagina ('FROSINONE · CENTROCAMPISTA ...')."""
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = htmllib.unescape(re.sub(r"<[^>]+>", "\n", text))
    m = TEAM_RE.search(text)
    return m.group(1).strip() if m else None


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
          f"statCasa={'statCasa' in html} GoalReal={'GoalReal' in html} team={page_team(html)!r}", file=sys.stderr)


def build_row(name, team, props):
    """Ritorna (riga, motivo_errore)."""
    casa, fuori = props.get("statCasa") or {}, props.get("statFuori") or {}
    if not isinstance(casa, dict) or not isinstance(fuori, dict) or not (casa or fuori):
        return None, "statCasa/statFuori assenti o con altro formato"
    fm = lambda d: round(num(d.get("MFG")), 2) if d.get("MFG") else ""
    return {
        "nome": name, "squadra": team,
        "golcasa": casa.get("GoalReal", 0), "golfuori": fuori.get("GoalReal", 0),
        "assistcasa": casa.get("Assist", 0), "assistfuori": fuori.get("Assist", 0),
        "ammcasa": casa.get("Amm", 0), "ammfuori": fuori.get("Amm", 0),
        "fmcasa": fm(casa), "fmfuori": fm(fuori),
    }, None


def get_data(session, name, team):
    global debug_saved
    any_page = False
    last_reason = None

    for slug, safe in slug_candidates(name):
        url = BASE + slug
        try:
            resp = session.get(url, timeout=15)
        except requests.RequestException:
            record_fail("errore di rete", name)
            return None
        if resp.status_code == 404:
            continue
        if resp.status_code != 200:
            record_fail(f"HTTP {resp.status_code}", name)
            return None
        any_page = True

        props, _via = extract_props(resp.text)
        if props is None:
            if not debug_saved:
                debug_saved = True
                with open("data/debug_pianetafanta.html", "w", encoding="utf-8") as f:
                    f.write(resp.text)
                diag(url, resp)
            last_reason = "nessun JSON con statCasa/statFuori nella pagina"
            continue

        ok = team_ok(props.get("squadra") or page_team(resp.text), team)
        if ok is False:
            last_reason = "squadra diversa (omonimo?)"
            continue
        if ok is None and not safe:
            last_reason = "squadra non verificabile sulla variante"
            continue

        row, reason = build_row(name, team, props)
        if row is None:
            last_reason = reason
            continue
        if ok is None:
            unverified.append(name)
        if slug != make_slug(name):
            found_slugs[name] = slug
        return row

    if not any_page:
        record_fail("pagina non trovata (404 su tutte le varianti)", name)
    else:
        record_fail(last_reason or "errore sconosciuto", name)
    missing.append((name, team))
    return None


def main():
    os.makedirs("data", exist_ok=True)
    quot = "data/quotazioni.csv"
    if not os.path.exists(quot):
        print(f"ERRORE: {quot} non trovato. Esegui prima scrape_fantacalcio.py", file=sys.stderr)
        sys.exit(1)
    load_slug_csv(OVERRIDE_PATH, OVERRIDES)
    load_slug_csv(CACHE_PATH, CACHE)
    print(f"Slug manuali: {len(OVERRIDES)}, slug in cache: {len(CACHE)}", flush=True)
    with open(quot, encoding="utf-8") as f:
        players = [{"nome": r["nome"], "squadra": r["squadra"]} for r in csv.DictReader(f) if r.get("nome") and r.get("squadra")]
    print(f"Trovati {len(players)} giocatori. Scarico da PianetaFanta...", flush=True)

    session = requests.Session()
    session.headers.update(HEADERS)
    adapter = requests.adapters.HTTPAdapter(max_retries=Retry(
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
    print(f"  squadra verificata sulla pagina: {len(results) - len(unverified)}, non verificabile: {len(unverified)}", flush=True)
    if len(unverified) > len(results) * 0.5:
        print("  ATTENZIONE: la squadra quasi mai verificabile, guarda la riga [diag] / data/debug_pianetafanta.html", flush=True)
    for motivo, n in fail.most_common():
        print(f"  falliti per {motivo}: {n}", flush=True)
        print(f"     esempi: {', '.join(examples[motivo])}", flush=True)

    if found_slugs:
        print(f"  slug scoperti automaticamente: {len(found_slugs)}", flush=True)
        for nome, slug in sorted(found_slugs.items()):
            print(f"     {nome} -> {BASE}{slug}", flush=True)
        merged = dict(CACHE)
        merged.update(found_slugs)
        with open(CACHE_PATH, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["nome", "slug"])
            for nome in sorted(merged):
                w.writerow([nome, merged[nome]])

    if missing:
        missing.sort()
        with open(MISSING_PATH, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["nome", "slug", "squadra"])
            for nome, squadra in missing:
                w.writerow([nome, "", squadra])
        print(f"  Nomi non risolti: {len(missing)} -> {MISSING_PATH}: compila slug e copia le righe in {OVERRIDE_PATH}", flush=True)
    elif os.path.exists(MISSING_PATH):
        os.remove(MISSING_PATH)

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
