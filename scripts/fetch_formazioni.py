"""
Scarica le probabili formazioni da Gazzetta.it e le abbina ai giocatori del listone.

Output: data/formazioni.csv  (id, nome, squadra, stato, prob, fonte, aggiornato, modulo, slot)
  - id      = id del giocatore in data/quotazioni.csv (cosi' l'app non deve rifare l'abbinamento)
  - stato   = titolare | ballottaggio
  - prob    = probabilita' stimata (0-100) che giochi dall'inizio
  - modulo  = modulo indicato da Gazzetta per la squadra (es. 3-5-2)
  - slot    = posizione nell'undici (0 = portiere, poi difesa, centrocampo, attacco).
              I giocatori in ballottaggio per lo stesso posto condividono lo stesso slot.
              Serve all'app per mostrare la probabile formazione "schierata" a campo.

Va eseguito DOPO fetch_data.py (serve data/quotazioni.csv).

NOTA: il parser e' scritto "al buio" rispetto all'HTML reale di Gazzetta. Se alla prima
esecuzione estrae 0 squadre, guarda data/debug_formazioni.html e adatta parse_gazzetta().
Si assume che Gazzetta elenchi i giocatori in ordine portiere -> difesa -> centrocampo -> attacco.
Se non e' cosi', l'app ripiega da sola sui ruoli del listone.
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
    r"([A-Za-zÀ-ÿ'.\-]+(?: [A-Za-zÀ-ÿ'.\-]+){0,2})\s*(?:\|\s*)?"
    r"(?:\(\s*(\d(?:-\d){1,4})\s*\)\s*(?:\|\s*)?:?|(\d(?:-\d){1,4})(?![\d-])\s*(?:\|\s*)?:?)"
)
# Alias nome Gazzetta -> nome come appare nel listone (da riempire quando serve)
ALIASES = {
    "LAUTARO": ["Martinez L.", "Lautaro Martinez"],
    "LAUTARO MARTINEZ": ["Martinez L."],
    # aggiungi qui altri soprannomi Gazzetta -> nome nel listone
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
    entry = re.sub(r"\d+", "", entry).strip(" .;:-|")
    if not entry:
        return [], False
    m = re.match(r"^(.*?)\((.*?)\)\s*$", entry)
    if m and m.group(1).strip() and m.group(2).strip():
        return [m.group(1).strip(), m.group(2).strip()], True
    parts = [p.strip() for p in re.split(r"\s*/\s*|\s+o\s+", entry) if p.strip()]
    return parts, len(parts) > 1


def parse_legacy(html, known_team_keys):
    """
    Ritorna (lista di dict {squadra, nome, stato, modulo, slot}, numero di intestazioni).
    Strategia: nel testo della pagina cerca le intestazioni 'SQUADRA (modulo):' e legge
    l'elenco dei giocatori fino alla prossima intestazione o parola-stop.
    Ogni voce dell'elenco e' uno 'slot' dell'undici (0 = portiere); piu' nomi nello stesso
    slot sono un ballottaggio.
    Se la pagina riporta la stessa squadra due volte (vista classica + schieramento) si
    tiene solo la prima occorrenza, cosi' gli slot non si mischiano.
    """
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    # " | " tra un elemento HTML e l'altro: se Gazzetta mette ogni giocatore in un <li>/<span>
    # senza virgole, cosi' restano comunque separati
    text = re.sub(r"\s+", " ", soup.get_text(" | ", strip=True))

    headers = list(HEADER_RE.finditer(text))
    out = []
    done_teams = set()
    for i, m in enumerate(headers):
        words = m.group(1).split()
        modulo = m.group(2) or m.group(3)
        loose = m.group(2) is None
        # il gruppo puo' includere parole precedenti (es. "Serie A INTER"): si prova dal suffisso piu' corto
        squadra = None
        for k in range(len(words)):
            cand = " ".join(words[k:])
            if team_key(cand) in known_team_keys:
                squadra = cand
                break
        if squadra is None:
            if loose:
                continue          # "xxx 3-5-2" senza parentesi: vale solo se xxx e' una squadra del listone
            squadra = words[-1]

        tk = team_key(squadra)
        if tk in done_teams:
            continue
        done_teams.add(tk)

        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        seg = text[m.end():end]
        stop = STOP_RE.search(seg)
        if stop:
            seg = seg[:stop.start()]

        slot = 0
        for entry in re.split(r"[;,|]", seg):
            nomi, ballot = split_entry(entry)
            if not nomi:
                continue
            for nome in nomi:
                out.append({
                    "squadra": squadra,
                    "nome": nome,
                    "stato": "ballottaggio" if ballot else "titolare",
                    "modulo": modulo,
                    "slot": slot,
                })
            slot += 1
    return out, len(headers)



MOD_RE = re.compile(r"Modulo\s*:?\s*(?:\|\s*)?(\d(?:-\d){1,4})", re.IGNORECASE)
END_LINEUP_RE = re.compile(r"DISPOSIZIONE|VISTA\s+(?:TESTUALE|IN CAMPO)|Panchina\s*:", re.IGNORECASE)
BALLOT_RE = re.compile(
    r"([A-Za-zÀ-ÿ'. ]+?)\s*-\s*([A-Za-zÀ-ÿ'. ]+?)\s+(\d{1,3})\s*-\s*(\d{1,3})\s*%"
)


def _team_players(listone, key):
    return [p for p in listone if team_key(p["squadra"]) == key]


def _resolve_side(listone, nome, hkey, akey):
    """'H' / 'A' se il nome appartiene in modo univoco alla squadra di casa / ospite, altrimenti None."""
    h = _match_one(_team_players(listone, hkey), nome)
    a = _match_one(_team_players(listone, akey), nome)
    if h and not a:
        return "H"
    if a and not h:
        return "A"
    return None


def _lineup_items(region):
    """Spezza il testo dell'undici in elementi ('N', nome) / ('#', numero di maglia)."""
    items = []
    for chunk in region.split("|"):
        for m in re.finditer(r"\d+|[^\d]+", chunk):
            t = m.group().strip(" .-:;,")
            if not t:
                continue
            items.append(("#", int(t)) if t.isdigit() else ("N", t))
    # salta allenatore/date finche' non si arriva a un "nome seguito da numero"
    for i in range(len(items) - 1):
        if items[i][0] == "N" and items[i + 1][0] == "#":
            return items[i:]
    return []


def _split_two_columns(items, listone, hkey, akey):
    """Ritorna (casa, ospite): liste di nomi nell'ordine dell'undici (portiere per primo).
    Riconosce da solo se il testo e' riga per riga ('nome n. n. nome') o a colonne."""
    types = "".join(t for t, _ in items)
    if types.startswith("N##N"):                      # riga: nome casa, n., n., nome ospite
        home, away = [], []
        i = 0
        while i + 3 < len(items) and types[i:i + 4] == "N##N" and len(home) < 11:
            home.append(items[i][1]); away.append(items[i + 3][1]); i += 4
        return home, away
    names = [v for t, v in items if t == "N"][:22]    # nome + numero, a colonne o alternati
    if len(names) < 22:
        half = len(names) // 2
        return names[:half], names[half:]
    col_h, col_a = names[:11], names[11:]
    alt_h, alt_a = names[0::2], names[1::2]
    def score(h, a):
        return (sum(_resolve_side(listone, n, hkey, akey) == "H" for n in h)
                + sum(_resolve_side(listone, n, hkey, akey) == "A" for n in a))
    return (alt_h, alt_a) if score(alt_h, alt_a) > score(col_h, col_a) else (col_h, col_a)


def parse_gazzetta(html, known_team_keys, listone=None):
    """Struttura reale di Gazzetta: per ogni partita 'Ultimo aggiornamento', i due nomi squadra,
    'Modulo: x-x-x' (casa) e 'Modulo: y-y-y' (ospite), poi i 22 giocatori con numero di maglia,
    poi Panchina / Ballottaggio (es. 'Yeboah-Lauberbach 60-40%') / Indisponibili."""
    listone = listone or []
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" | ", strip=True))

    blocks = re.split(r"Ultimo\s+aggiornamento", text, flags=re.IGNORECASE)[1:]
    out, done = [], set()
    for blk in blocks:
        mods = list(MOD_RE.finditer(blk))
        if len(mods) < 2:
            continue
        # nomi delle due squadre: chunk senza cifre prima del primo "Modulo" che sono squadre note
        teams = []
        for chunk in re.split(r"\s*\|\s*", blk[:mods[0].start()]):
            c = chunk.strip()
            if c and not re.search(r"\d", c) and team_key(c) in known_team_keys and c not in teams:
                teams.append(c)
        if len(teams) < 2:
            continue
        home_t, away_t = teams[0], teams[1]
        hkey, akey = team_key(home_t), team_key(away_t)
        if hkey in done or akey in done:
            continue
        rest = blk[mods[1].end():]
        end = END_LINEUP_RE.search(rest)
        region = rest[:end.start()] if end else rest[:1500]
        home, away = _split_two_columns(_lineup_items(region), listone, hkey, akey)
        sides = [(home_t, mods[0].group(1), home), (away_t, mods[1].group(1), away)]

        # ballottaggi con percentuali reali: "Yeboah-Lauberbach 60-40%"
        ballots = []
        for bm in re.finditer(r"Ballottagg\w*\s*:(.*?)(?=Squalificat|Indisponibil|Diffidat|Altri\s*:|Panchina|Ballottagg|$)", rest, re.IGNORECASE):
            for pm in BALLOT_RE.finditer(bm.group(1).replace("|", " ")):
                ballots.append((pm.group(1).strip(), pm.group(2).strip(), int(pm.group(3)), int(pm.group(4))))

        for team, modulo, names in sides:
            lineup = {norm(n): (n, i) for i, n in enumerate(names)}
            def in_lineup(n):
                nn = norm(n)
                if nn in lineup:
                    return lineup[nn]
                tk = name_tokens(n)
                for k, v in lineup.items():
                    kt = name_tokens(k)
                    if tk and kt and (tk <= kt or kt <= tk):
                        return v
                return None
            probs, extra = {}, []
            for a, b, pa, pb in ballots:
                la, lb = in_lineup(a), in_lineup(b)
                if la and not lb:
                    probs[la[1]] = pa; extra.append((b, la[1], pb))
                elif lb and not la:
                    probs[lb[1]] = pb; extra.append((a, lb[1], pa))
                elif la and lb:
                    probs[la[1]] = pa; probs[lb[1]] = pb
            for i, n in enumerate(names):
                out.append({"squadra": team, "nome": n, "modulo": modulo, "slot": i,
                            "stato": "ballottaggio" if i in probs else "titolare",
                            "prob": probs.get(i)})
            seen_extra = {norm(n) for n in names}
            for n, slot, pr in extra:
                if norm(n) in seen_extra:
                    continue
                seen_extra.add(norm(n))
                out.append({"squadra": team, "nome": n, "modulo": modulo, "slot": slot,
                            "stato": "ballottaggio", "prob": pr})
        done.update([hkey, akey])
    return out, len(done)


def load_listone(path="data/quotazioni.csv"):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _qta(p):
    for k in ("qta", "QTA", "Qt.A", "QtA", "fvm", "FVM"):
        try:
            return float(str(p.get(k, "")).replace(",", "."))
        except ValueError:
            continue
    return 0.0


def _match_one(pool, nome):
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
    if len(sub) > 1:
        # omonimi nella stessa squadra: prende quello con la quotazione piu' alta (di solito il titolare)
        return max(sub, key=_qta)
    return None


def match_player(listone, nome, squadra):
    tk = team_key(squadra)
    in_team = [p for p in listone if team_key(p["squadra"]) == tk]
    pool = in_team or listone  # se la squadra non combacia, si prova sul listone intero
    candidates = list(ALIASES.get(norm(nome), [])) + [nome]
    for cand in candidates:
        hit = _match_one(pool, cand)
        if hit:
            return hit
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

    entries, n_teams = parse_gazzetta(html, known, listone)
    if not entries:
        print("   parser Gazzetta: 0 partite, provo il parser alternativo", file=sys.stderr)
        entries, n_teams = parse_legacy(html, known)
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
            "prob": e.get("prob") if e.get("prob") is not None else (PROB_BALLOTTAGGIO if e["stato"] == "ballottaggio" else PROB_TITOLARE),
            "fonte": "gazzetta",
            "aggiornato": now,
            "modulo": e["modulo"],
            "slot": e["slot"],
        })

    print(f"Squadre trovate: {n_teams}, giocatori letti: {len(entries)}, abbinati: {len(rows)}")
    per_team = {}
    for e in entries:
        per_team.setdefault(e["squadra"], set()).add(e["slot"])
    for sq, slots in sorted(per_team.items()):
        flag = "" if len(slots) == 11 else "   <-- attesi 11 slot, controlla il parser"
        print(f"   {sq}: {len(slots)} slot{flag}", file=sys.stderr)
    if unmatched:
        print(f"Non abbinati ({len(unmatched)}): " + "; ".join(unmatched), file=sys.stderr)
    if not rows:
        print("ERRORE: nessun giocatore abbinato al listone", file=sys.stderr)
        sys.exit(1)

    with open("data/formazioni.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f, fieldnames=["id", "nome", "squadra", "stato", "prob", "fonte", "aggiornato", "modulo", "slot"]
        )
        w.writeheader()
        w.writerows(rows)
    print("OK: data/formazioni.csv")


if __name__ == "__main__":
    main()
