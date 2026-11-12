#!/usr/bin/env python3
"""
Scarica da pianetafanta.it le statistiche di ogni giocatore:
generale, casa, fuori e forma (ultime 3/5/10 giornate).

I dati stanno gia' nella pagina di ogni giocatore, dentro il blocco JSON di Next.js
(chiavi statGen, statCasa, statFuori, forma3, forma5, forma10): non serve cliccare nulla.
Voti e fantamedia NON vengono presi da qui: restano quelli di fantacalcio.it.

Output: data/stats_pianetafanta.csv (statistiche avanzate) e data/voti_giornate.csv (voti per giornata).
Nome e squadra sono quelli di fantacalcio.it (data/quotazioni.csv), cosi' la pagina abbina i giocatori.
Va lanciato DOPO scrape_fantacalcio.py. Salvalo come scripts/scrape_pianetafanta.py.

Versione veloce:
  - download dei giocatori in parallelo (WORKERS thread, una Session per thread)
  - raccolta slug in parallelo (pagine quotazioni + un thread per ruolo di classifica)
  - cache degli slug in data/slugs.txt (rinnovata ogni SLUGS_MAX_AGE_H ore;
    FORCE_SLUGS=1 per forzare la raccolta). Ricordati di committare anche data/slugs.txt.
  - WORKERS e DELAY regolabili da variabile d'ambiente (PF_WORKERS, PF_DELAY).
"""
import csv
import json
import os
import re
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

import requests

BASE = "https://www.pianetafanta.it"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
}
WORKERS = int(os.environ.get("PF_WORKERS", "6"))     # richieste in parallelo
DELAY = float(os.environ.get("PF_DELAY", "0.3"))     # pausa per worker tra una richiesta e l'altra
MIN_ROWS = 100     # sotto questa soglia non sovrascrivo il CSV esistente
PF_OUT = "data/stats_pianetafanta.csv"   # letto da index.html (Statistiche Avanzate)
SLUGS_FILE = "data/slugs.txt"
SLUGS_MAX_AGE_H = 24 * 7                 # la lista giocatori cambia raramente
MAX_RANK_PAGES = 80

SCOPES = (("g", "statGen"), ("c", "statCasa"), ("f", "statFuori"),
          ("f3", "forma3"), ("f5", "forma5"), ("f10", "forma10"))
FIELDS = (("pv", "PresenzeVoto"), ("tit", "Titolare"), ("sost", "Sostituito"),
          ("sub", "Subentrato"), ("min", "Minuti"), ("amm", "Amm"), ("esp", "Esp"),
          ("gf", "GoalReal"), ("gs", "GoalSub"), ("ass", "Assist"), ("au", "Autorete"),
          ("rt", "RigoreTra"), ("rs", "RigoreSba"), ("rp", "RigorePar"), ("rsub", "RigoreSub"))
SCOPE_LABEL = {"g": "generale", "c": "casa", "f": "fuori", "f3": "forma 3", "f5": "forma 5", "f10": "forma 10"}
FIELD_LABEL = {"pv": "presenze con voto", "tit": "titolare", "sost": "sostituito", "sub": "subentrato",
               "min": "minuti", "amm": "ammonizioni", "esp": "espulsioni", "gf": "gol", "gs": "gol subiti",
               "ass": "assist", "au": "autogol", "rt": "rigori segnati", "rs": "rigori sbagliati",
               "rp": "rigori parati", "rsub": "rigori subiti"}
PF_COLUMNS = (["nome", "squadra", "golcasa", "golfuori", "assistcasa", "assistfuori", "fmcasa", "fmfuori",
               "aggiornato", "pf_generale_presenze", "pf_generale_giornate"]
              + [f"pf_{SCOPE_LABEL[s]}_{FIELD_LABEL[f]}" for s, _ in SCOPES for f, _ in FIELDS])

SLUG_RE = re.compile(r"giocatori/([a-z0-9][a-z0-9\-]*)")
PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)')

_local = threading.local()


def session():
    """Una Session per thread: riusa la connessione TLS senza condividere stato tra thread."""
    s = getattr(_local, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update(HEADERS)
        _local.s = s
    return s


def season_label():
    today = date.today()
    start = today.year if today.month >= 7 else today.year - 1
    return f"{start}_{start + 1}"


def get(url, tries=3):
    for i in range(tries):
        try:
            r = session().get(url, timeout=30)
            if r.status_code == 200:
                return r.text
            print(f"   {url}: status {r.status_code}", file=sys.stderr)
            if r.status_code == 404:
                return None
            if r.status_code in (429, 403, 503):
                time.sleep(5 * (i + 1))   # il sito ci sta limitando: rallento
                continue
        except requests.RequestException as e:
            print(f"   {url}: {e}", file=sys.stderr)
        time.sleep(2 * (i + 1))
    return None


def slugs_in(text):
    return set(SLUG_RE.findall(text or ""))


def _fetch_quotazioni_page(r):
    url = BASE + "/quotazioni-fantacalcio" + (f"?ruolo={r}" if r else "")
    found = slugs_in(get(url))
    print(f"Quotazioni {r or 'base'}: {len(found)} giocatori", file=sys.stderr)
    time.sleep(DELAY)
    return found


def _fetch_ranking_role(args):
    """Scorre le pagine di classifica di un ruolo (in serie: ogni pagina dipende dalla precedente)."""
    r, season = args
    slugs = set()
    for p in range(1, MAX_RANK_PAGES):
        url = (f"{BASE}/statistiche-fantacalcio/classifica?tipo=medie-voto"
               f"&s={season}&t=0&dir=DESC&p={p}&ruolo={r}")
        found = slugs_in(get(url))
        if not found or not (found - slugs):
            break
        slugs |= found
        time.sleep(DELAY)
    print(f"Classifica {r}: {len(slugs)} giocatori", file=sys.stderr)
    return slugs


def load_cached_slugs():
    """La data di generazione sta nella prima riga del file ('# generated: <epoch>'):
    la data di modifica non e' affidabile, dopo un checkout in Actions e' sempre 'adesso'."""
    if os.environ.get("FORCE_SLUGS") == "1" or not os.path.exists(SLUGS_FILE):
        return None
    with open(SLUGS_FILE, encoding="utf-8") as f:
        lines = [l.strip() for l in f if l.strip()]
    if not lines or not lines[0].startswith("# generated:"):
        return None
    try:
        age_h = (time.time() - float(lines[0].split(":", 1)[1])) / 3600
    except ValueError:
        return None
    slugs = sorted({l for l in lines[1:] if not l.startswith("#")})
    if age_h > SLUGS_MAX_AGE_H or len(slugs) < MIN_ROWS:
        return None
    print(f"Slug da cache ({SLUGS_FILE}, {age_h:.0f} h fa): {len(slugs)} giocatori", file=sys.stderr)
    return slugs


def collect_slugs():
    """Elenco dei giocatori: pagina quotazioni + classifiche per ruolo (unione), in parallelo."""
    cached = load_cached_slugs()
    if cached:
        return cached

    slugs = set()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for found in ex.map(_fetch_quotazioni_page, ("", "P", "D", "C", "T", "A")):
            slugs |= found
        season = season_label()
        for found in ex.map(_fetch_ranking_role, [(r, season) for r in ("P", "D", "C", "A")]):
            slugs |= found
    print(f"Slug raccolti: {len(slugs)} giocatori in totale", file=sys.stderr)

    result = sorted(slugs)
    if len(result) >= MIN_ROWS:
        with open(SLUGS_FILE, "w", encoding="utf-8") as f:
            f.write(f"# generated: {time.time():.0f}\n" + "\n".join(result) + "\n")
    return result


def extract_props(html):
    """Estrae l'oggetto con statGen/statCasa/... dal flusso Next.js della pagina."""
    chunks = []
    for m in PUSH_RE.finditer(html):
        try:
            chunks.append(json.loads('"' + m.group(1) + '"'))
        except ValueError:
            pass
    blob = "".join(chunks)
    i = blob.find('"statGen"')
    if i < 0:
        return None
    start = blob.rfind('{"nome":', 0, i)
    if start < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(blob, start)
    except ValueError:
        return None
    return obj


def clean(v):
    if isinstance(v, float):
        return round(v, 2)
    return v


def build_row(slug, obj, stamp):
    row = {
        "slug": slug,
        "nome": obj.get("nome", ""),
        "squadra": obj.get("squadra", ""),
        "ruolo": obj.get("ruolo", ""),
        "giornate": obj.get("giornataMax", ""),
        "aggiornato": stamp,
        "g_pres": clean((obj.get("statGen") or {}).get("Presenze", "")),
    }
    for s, key in SCOPES:
        block = obj.get(key) or {}
        for f, src in FIELDS:
            v = block.get(src)
            row[f"{s}_{f}"] = "" if v is None else clean(v)
    return row


VOTI_COLUMNS = ["nome", "squadra", "giornata", "voto", "fantavoto", "gf", "gs", "rp", "rc",
                "rpos", "rneg", "ass", "amm", "esp", "au"]
VOTI_OUT = "data/voti_giornate.csv"


def norm(s):
    s = unicodedata.normalize("NFD", str(s or ""))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^A-Z0-9]+", " ", s.upper()).strip()


def load_quotazioni(path="data/quotazioni.csv"):
    """Giocatori di fantacalcio.it (scritti dallo script precedente), per squadra (prime 3 lettere)."""
    index = {}
    try:
        with open(path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                index.setdefault(norm(r.get("squadra"))[:3], []).append(r)
    except OSError:
        pass
    return index


def same_name(a, b):
    ta, tb = norm(a).split(), norm(b).split()
    ca, cb = [t for t in ta if len(t) > 1], [t for t in tb if len(t) > 1]
    if not ca or not cb:
        return False
    if not (all(t in cb for t in ca) or all(t in ca for t in cb)):
        return False
    ia, ib = [t for t in ta if len(t) == 1], [t for t in tb if len(t) == 1]
    return not (ia and ib and ia[0] != ib[0])


def match_quotazione(index, obj):
    ruolo = str(obj.get("ruolo", "")).upper()
    ruolo = "C" if ruolo == "T" else ruolo
    cands = [q for q in index.get(norm(obj.get("squadra"))[:3], [])
             if same_name(q.get("nome"), obj.get("nome"))]
    if len(cands) > 1:
        cands = [q for q in cands if q.get("ruolo") == ruolo] or cands
    return cands[0] if len(cands) == 1 else None


def num(v):
    return v if isinstance(v, (int, float)) else 0


def build_voti(q, obj):
    """Voti e bonus per giornata, dal blocco 'andamento' della pagina giocatore.
    voto = media delle tre testate; fantavoto = media dei tre fantavoti."""
    out = []
    for a in obj.get("andamento") or []:
        if a.get("VotoSiNo") != 1:
            continue
        voto = a.get("MediaVoto3")
        if not isinstance(voto, (int, float)):
            voto = a.get("MediaVoto2")
        if not isinstance(voto, (int, float)) or voto <= 0:
            continue
        fvs = [a.get(k) for k in ("VotoFanta", "VotoFantaC", "VotoFantaT") if isinstance(a.get(k), (int, float))]
        fv = round(sum(fvs) / len(fvs), 2) if fvs else voto
        tra, sba = num(a.get("RigoreTra")), num(a.get("RigoreSba"))
        out.append({
            "nome": q["nome"], "squadra": q["squadra"], "giornata": a.get("Giornata"),
            "voto": round(voto, 2), "fantavoto": fv,
            "gf": num(a.get("GoalReal")), "gs": num(a.get("GoalSub")), "rp": num(a.get("RigorePar")),
            "rc": tra + sba, "rpos": tra, "rneg": sba,
            "ass": num(a.get("Assist")), "amm": num(a.get("Amm")), "esp": num(a.get("Esp")),
            "au": num(a.get("Autorete")),
        })
    return out


def build_pf_row(q, obj, stamp):
    """Riga per index.html: colonne fisse (gol/assist/fm casa-fuori) + pf_* con tutti gli altri dati."""
    casa, fuori, gen = (obj.get(k) or {} for k in ("statCasa", "statFuori", "statGen"))

    def n(v):
        return "" if v is None else clean(v)

    row = {
        "nome": q["nome"], "squadra": q["squadra"],
        "golcasa": n(casa.get("GoalReal")), "golfuori": n(fuori.get("GoalReal")),
        "assistcasa": n(casa.get("Assist")), "assistfuori": n(fuori.get("Assist")),
        "fmcasa": n(casa.get("MFG")), "fmfuori": n(fuori.get("MFG")),
        "aggiornato": stamp,
        "pf_generale_presenze": n(gen.get("Presenze")), "pf_generale_giornate": n(obj.get("giornataMax")),
    }
    for s, key in SCOPES:
        block = obj.get(key) or {}
        for f, src in FIELDS:
            row[f"pf_{SCOPE_LABEL[s]}_{FIELD_LABEL[f]}"] = n(block.get(src))
    return row


def fetch_player(slug):
    """Eseguito nei thread: scarica e fa il parsing JSON (la parte pesante), niente stato condiviso."""
    html = get(f"{BASE}/giocatori/{slug}")
    obj = extract_props(html) if html else None
    time.sleep(DELAY)
    return slug, html, obj


def main():
    t0 = time.time()
    os.makedirs("data", exist_ok=True)
    slugs = collect_slugs()
    print(f"Trovati {len(slugs)} giocatori da scaricare ({time.time() - t0:.0f}s)")
    if len(slugs) < MIN_ROWS:
        print("ERRORE: troppo pochi giocatori trovati, struttura del sito cambiata?", file=sys.stderr)
        sys.exit(1)

    stamp = datetime.now(timezone.utc).strftime("%d/%m %H:%M UTC")
    rows, failed = [], 0
    qindex, voti_rows, pf_rows, abbinati = load_quotazioni(), [], [], 0
    debug_saved = False

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for i, (slug, html, obj) in enumerate(ex.map(fetch_player, slugs), 1):
            if not obj:
                failed += 1
                if not debug_saved and html:
                    with open("data/debug_pianeta.html", "w", encoding="utf-8") as f:
                        f.write(html)
                    debug_saved = True
            else:
                rows.append(build_row(slug, obj, stamp))
                q = match_quotazione(qindex, obj)
                if q:
                    abbinati += 1
                    pf_rows.append(build_pf_row(q, obj, stamp))
                    voti_rows.extend(build_voti(q, obj))
            if i % 50 == 0:
                print(f"   {i}/{len(slugs)} (errori: {failed}, {time.time() - t0:.0f}s)")

    if len(rows) < MIN_ROWS or failed > len(slugs) * 0.3:
        print(f"ERRORE: solo {len(rows)} giocatori letti, {failed} falliti: non sovrascrivo i CSV",
              file=sys.stderr)
        sys.exit(1)

    if len(pf_rows) >= MIN_ROWS:
        with open(PF_OUT, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=PF_COLUMNS)
            w.writeheader()
            w.writerows(pf_rows)
        print(f"OK: {PF_OUT} ({len(pf_rows)} giocatori abbinati a fantacalcio.it, {failed} pagine non lette)")
    else:
        print(f"ERRORE: solo {len(pf_rows)} giocatori abbinati: manca data/quotazioni.csv?", file=sys.stderr)
        sys.exit(1)
    if abbinati >= MIN_ROWS and voti_rows:
        with open(VOTI_OUT, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=VOTI_COLUMNS)
            w.writeheader()
            w.writerows(voti_rows)
        print(f"OK: {VOTI_OUT} ({len(voti_rows)} righe, {abbinati} giocatori abbinati a fantacalcio.it)")
    else:
        print(f"ATTENZIONE: voti per giornata non scritti (abbinati {abbinati}): manca data/quotazioni.csv?",
              file=sys.stderr)
    print(f"Completato in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()