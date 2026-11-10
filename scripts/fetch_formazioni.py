"""
Scarica le probabili formazioni da Gazzetta.it e le abbina ai giocatori del listone.

Output: data/formazioni.csv  (id, nome, squadra, stato, prob, fonte, aggiornato)
  - id      = id del giocatore in data/quotazioni.csv (cosi' l'app non deve rifare l'abbinamento)
  - stato   = titolare | ballottaggio
  - prob    = probabilita' stimata (0-100) che giochi dall'inizio

Va eseguito DOPO fetch_data.py (serve data/quotazioni.csv).

NOTA: il parser e' scritto "al buio" rispetto all'HTML reale di Gazzetta. Se alla prima
esecuzione estrae 0 squadre, guarda data/debug_formazioni.html e adatta parse_gazzetta().
"""
import csv
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

URL = "https://www.gazzetta.it/Calcio/prob_form/"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
}
BLOCK_MARKERS = ["captcha", "access denied", "just a moment", "are you human", "attention required"]

PROB_TITOLARE = 90        # nell'undici indicato da Gazzetta
PROB_BALLOTTAGGIO = 50    # ballottaggio tra due giocatori

# Parole che chiudono l'elenco dei giocatori di una squadra
STOP_RE = re.compile(
    r"\b(Allenatore|All\.|Arbitro|Panchina|Ballottaggi?|Squalificat\w*|Indisponibil\w*|Diffidat\w*)\b",
    re.IGNORECASE,
)
# "INTER (3-5-2):" oppure "Hellas Verona (4-3-3):"
HEADER_RE = re.compile(
    r"([A-Za-zÀ-ÿ'.\-]+(?: [A-Za-zÀ-ÿ'.\-]+){0,2})\s*\((\d(?:-\d){1,4})\)\s*:"
)
# Alias nome Gazzetta -> nome come appare nel listone (da riempire quando serve)
ALIASES = {
    # "Lautaro": "Martinez L.",
}


def norm(s):
    s = unicodedata.normalize("NFD", str(s or ""))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^A-Z0-9]+", " ", s.upper()).strip()


def team_key(s):
    n = norm(s)
    n = re.sub(r"^(AC|AS|SS|US|FC|SSC|ACF)\s+", "", n)
    if "VERONA" in n:
        return "VER"
    return n.replace(" ", "")[:3]


def name_tokens(s):
    return {t for t in norm(s).split() if len(t) > 1}


def fetch_page():
    try:
        resp = requests.get(URL, headers=HEADERS, timeout=30)
        print(f"   -> {URL}: status={resp.status_code}, bytes={len(resp.content)}", file=sys.stderr)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"ERRORE scaricando {URL}: {e}", file=sys.stderr)
        return None
    low = resp.text.lower()
    hits = [m for m in BLOCK_MARKERS if m in low]
    if hits:
        print(f"   SOSPETTO BLOCCO/CHALLENGE: marker={hits}", file=sys.stderr)
    return resp.text


def split_entry(entry):
    """'Dimarco (Darmian)', 'Dimarco/Darmian', 'Dimarco o Darmian' -> (nomi, ballottaggio?)"""
    entry = re.sub(r"\d+", "", entry).strip(" .;:-")
    if not entry:
        return [], False
    m = re.match(r"^(.*?)\((.*?)\)\s*$", entry)
    if m and m.group(1).strip() and m.group(2).strip():
        return [m.group(1).strip(), m.group(2).strip()], True
    parts = [p.strip() for p in re.split(r"\s*/\s*|\s+o\s+", entry) if p.strip()]
    return parts, len(parts) > 1


def parse_gazzetta(html, known_team_keys):
    """
    Ritorna lista di dict: {squadra, nome, stato}.
    Strategia: nel testo della pagina cerca le intestazioni 'SQUADRA (modulo):' e legge
    l'elenco dei giocatori fino alla prossima intestazione o parola-stop.
    """
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))

    headers = list(HEADER_RE.finditer(text))
    out = []
    for i, m in enumerate(headers):
        words = m.group(1).split()
        # il gruppo puo' includere parole precedenti (es. "Serie A INTER"): si prova dal suffisso piu' corto
        squadra = None
        for k in range(len(words)):
            cand = " ".join(words[k:])
            if team_key(cand) in known_team_keys:
                squadra = cand
                break
        if squadra is None:
            squadra = words[-1]

        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        seg = text[m.end():end]
        stop = STOP_RE.search(seg)
        if stop:
            seg = seg[:stop.start()]

        for entry in re.split(r"[;,]", seg):
            nomi, ballot = split_entry(entry)
            for nome in nomi:
                out.append({
                    "squadra": squadra,
                    "nome": nome,
                    "stato": "ballottaggio" if ballot else "titolare",
                })
    return out, len(headers)


def load_listone(path="data/quotazioni.csv"):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def match_player(listone, nome, squadra):
    nome = ALIASES.get(nome, nome)
    tk = team_key(squadra)
    in_team = [p for p in listone if team_key(p["squadra"]) == tk]
    pool = in_team or listone  # se la squadra non combacia, si prova sul listone intero
    n = norm(nome)

    exact = [p for p in pool if norm(p["nome"]) == n]
    if len(exact) == 1:
        return exact[0]

    toks = name_tokens(nome)
    if not toks:
        return None
    sub = [p for p in pool
           if name_tokens(p["nome"]) and (toks <= name_tokens(p["nome"]) or name_tokens(p["nome"]) <= toks)]
    if len(sub) == 1:
        return sub[0]
    return None


def main():
    if not os.path.exists("data/quotazioni.csv"):
        print("ERRORE: manca data/quotazioni.csv, esegui prima fetch_data.py", file=sys.stderr)
        sys.exit(1)
    listone = load_listone()
    known = {team_key(p["squadra"]) for p in listone if p.get("squadra")}

    print("Scarico probabili formazioni da Gazzetta...")
    html = fetch_page()
    if not html:
        sys.exit(1)
    os.makedirs("data", exist_ok=True)
    with open("data/debug_formazioni.html", "w", encoding="utf-8") as f:
        f.write(html)

    entries, n_teams = parse_gazzetta(html, known)
    if not entries:
        print("ERRORE: nessuna formazione estratta. Guarda data/debug_formazioni.html "
              "e adatta parse_gazzetta().", file=sys.stderr)
        sys.exit(1)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rows, unmatched, seen = [], [], set()
    for e in entries:
        p = match_player(listone, e["nome"], e["squadra"])
        if not p:
            unmatched.append(f"{e['nome']} ({e['squadra']})")
            continue
        if p["id"] in seen:
            continue
        seen.add(p["id"])
        rows.append({
            "id": p["id"],
            "nome": p["nome"],
            "squadra": p["squadra"],
            "stato": e["stato"],
            "prob": PROB_BALLOTTAGGIO if e["stato"] == "ballottaggio" else PROB_TITOLARE,
            "fonte": "gazzetta",
            "aggiornato": now,
        })

    print(f"Squadre trovate: {n_teams}, giocatori letti: {len(entries)}, abbinati: {len(rows)}")
    if unmatched:
        print(f"Non abbinati ({len(unmatched)}): " + "; ".join(unmatched), file=sys.stderr)
    if not rows:
        print("ERRORE: nessun giocatore abbinato al listone", file=sys.stderr)
        sys.exit(1)

    with open("data/formazioni.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["id", "nome", "squadra", "stato", "prob", "fonte", "aggiornato"])
        w.writeheader()
        w.writerows(rows)
    print("OK: data/formazioni.csv")


if __name__ == "__main__":
    main()
