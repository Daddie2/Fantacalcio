"""
Scarica quotazioni, statistiche, voti e indisponibili da fantacalcio.it.

Ogni fase e' isolata: se una fallisce (anche con un'eccezione) le altre
girano comunque, e i CSV delle fasi riuscite vengono salvati. Alla fine lo
script esce con codice 1 se una fase CRITICA e' fallita, cosi' il run su
GitHub risulta rosso invece di fallire in silenzio.
"""
import os
import sys
import re
import csv
import time
import traceback
import requests
from datetime import date
from bs4 import BeautifulSoup, NavigableString, Tag, Comment

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

URLS = {
    "quotazioni": "https://www.fantacalcio.it/quotazioni-fantacalcio",
    "statistiche": "https://www.fantacalcio.it/statistiche-serie-a",
    "indisponibili": "https://www.fantacalcio.it/indisponibili-serie-a",
}

# Template per le pagine di statistiche di una stagione specifica (storiche).
# La pagina statistiche-serie-a "semplice" (in URLS sopra) punta invece
# sempre da sola alla stagione IN CORSO, senza bisogno di specificarla.
STATS_SEASON_URL_TEMPLATE = "https://www.fantacalcio.it/statistiche-serie-a/{season}/italia"

BLOCK_MARKERS = [
    "captcha", "attention required", "access denied",
    "just a moment", "checking your browser", "are you human",
]


def current_season_label():
    """
    Calcola l'etichetta stagione corrente nel formato usato dal sito
    (es. '2026-27'). La Serie A parte convenzionalmente ad agosto: se
    siamo tra luglio e dicembre la stagione è annoCorrente-annoProssimo,
    altrimenti annoScorso-annoCorrente.
    """
    today = date.today()
    start_year = today.year if today.month >= 7 else today.year - 1
    end_short = (start_year + 1) % 100
    return f"{start_year}-{end_short:02d}"


def previous_season_label():
    """Etichetta della stagione precedente a quella corrente, stesso formato."""
    today = date.today()
    start_year = (today.year if today.month >= 7 else today.year - 1) - 1
    end_short = (start_year + 1) % 100
    return f"{start_year}-{end_short:02d}"


def fetch_page(url, retries=3):
    """
    Scarica una pagina HTML con diagnostica su status/dimensione/blocchi.
    Riprova su errori di rete, 403, 429 e 5xx (i runner di GitHub a volte
    vengono rallentati o respinti); non riprova sugli altri 4xx (es. 404).
    """
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            resp = SESSION.get(url, timeout=30)
            print(f"   -> {url}: status={resp.status_code}, bytes={len(resp.content)} "
                  f"(tentativo {attempt}/{retries})", file=sys.stderr)
            if resp.status_code in (403, 429) or resp.status_code >= 500:
                last_err = f"HTTP {resp.status_code}"
                time.sleep(2 * attempt)
                continue
            if resp.status_code >= 400:
                last_err = f"HTTP {resp.status_code}"
                break
            # Senza charset nell'header requests assume ISO-8859-1 e rovina
            # gli accenti (es. 'Konè'), che poi non combaciano piu' col listone.
            if "charset" not in (resp.headers.get("Content-Type") or "").lower():
                resp.encoding = "utf-8"
            text = resp.text
            text_lower = text.lower()
            hits = [m for m in BLOCK_MARKERS if m in text_lower]
            if hits:
                print(f"   SOSPETTO BLOCCO/CHALLENGE su {url}: marker trovati={hits}", file=sys.stderr)
            return text
        except requests.RequestException as e:
            last_err = str(e)
            print(f"   errore di rete su {url} (tentativo {attempt}/{retries}): {e}", file=sys.stderr)
            time.sleep(2 * attempt)
    print(f"ERRORE scaricando {url}: {last_err}", file=sys.stderr)
    return None


def clean_number(val):
    """Pulisce un valore numerico da stringa (es. '1.234,5' -> 1234.5)."""
    if not val:
        return 0
    clean = re.sub(r'[^\d,.\-]', '', str(val))
    if ',' in clean and '.' in clean:
        clean = clean.replace('.', '').replace(',', '.')
    else:
        clean = clean.replace(',', '.')
    try:
        return float(clean)
    except ValueError:
        return 0


def build_header_labels(header_row):
    """
    Espande gli header rispettando gli attributi colspan.

    Su fantacalcio.it la cella header 'Calciatore' copre con colspan le
    colonne icona (checkbox/preferiti/ruolo) + la colonna nome. Se non
    espandiamo il colspan, l'indice degli header non corrisponde più
    all'indice delle celle nelle righe dati (che invece sono tutte celle
    singole), e ogni colonna dopo 'Calciatore' risulta disallineata.
    """
    labels = []
    for cell in header_row.find_all(['th', 'td']):
        try:
            colspan = int(cell.get('colspan', 1))
        except (TypeError, ValueError):
            colspan = 1
        text = cell.get_text(strip=True)
        labels.extend([text] * max(colspan, 1))
    return labels


def extract_from_table(html, required_keywords):
    """Trova la tabella giusta cercando keyword negli header (espansi per colspan)."""
    soup = BeautifulSoup(html, 'html.parser')
    tables = soup.find_all('table')

    for table in tables:
        header_row = table.find('tr')
        if not header_row:
            continue
        headers = build_header_labels(header_row)
        header_text = ' '.join(headers).upper()
        if any(k in header_text for k in required_keywords):
            return table, headers

    return None, None


def extract_ruolo(row):
    """
    Estrae il ruolo Classic (P/D/C/A) da una riga della tabella quotazioni.

    Fonte esatta, verificata sull'HTML reale del sito: l'attributo
    data-filter-role-classic sul <tr> stesso, es.
    <tr ... data-filter-role-classic="c" ...>. Non serve interpretare
    icone o classi CSS.
    """
    valid = {'P', 'D', 'C', 'A'}

    val = (row.get('data-filter-role-classic') or '').strip().upper()
    if val in valid:
        return val

    # Fallback se l'attributo sparisse: data-value dello span dentro
    # <th class="player-role-classic">
    classic_th = row.find(class_='player-role-classic')
    if classic_th:
        span = classic_th.find('span', class_='role')
        if span:
            val2 = (span.get('data-value') or '').strip().upper()
            if val2 in valid:
                return val2

    return '?'


def parse_quotazioni(html):
    """Estrae le quotazioni dalla tabella reale di fantacalcio.it."""
    table, headers = extract_from_table(html, ['CALCIATORE', 'FVM', 'QUOTAZ'])
    if not table:
        print("ATTENZIONE: tabella quotazioni non trovata, struttura pagina cambiata", file=sys.stderr)
        return []

    # QI/QA/FVM compaiono due volte (Classic + Mantra): prendiamo la prima
    # occorrenza di ciascuno = Classic. Il nome giocatore non si trova per
    # testo header (la cella 'Calciatore' ha colspan su più colonne): è
    # sempre la colonna immediatamente prima di 'Sq'.
    col_map = {}
    seen = {'qi': False, 'qta': False, 'fvm': False}
    squadra_idx = None
    for i, h in enumerate(headers):
        h_clean = h.upper().replace(' ', '').replace('.', '').replace('/', '')
        if h_clean in ('SQ', 'SQUADRA', 'TEAM') and squadra_idx is None:
            squadra_idx = i
        elif h_clean == 'QI' and not seen['qi']:
            col_map['qi'] = i
            seen['qi'] = True
        elif h_clean == 'QA' and not seen['qta']:
            col_map['qta'] = i
            seen['qta'] = True
        elif 'FVM' in h_clean and not seen['fvm']:
            col_map['fvm'] = i
            seen['fvm'] = True

    if squadra_idx is None or squadra_idx == 0:
        print("ATTENZIONE: colonna 'Sq' non trovata nella tabella quotazioni", file=sys.stderr)
        return []

    col_map['squadra'] = squadra_idx
    col_map['nome'] = squadra_idx - 1

    players = []
    rows = table.find_all('tr')[1:]
    for row in rows:
        cols = row.find_all(['td', 'th'])
        row_data = [c.get_text(strip=True) for c in cols]

        nome_idx = col_map['nome']
        if nome_idx >= len(row_data):
            continue
        nome = row_data[nome_idx]
        if not nome or len(nome) < 2:
            continue

        squadra = row_data[col_map['squadra']] if col_map['squadra'] < len(row_data) else ''

        qta_idx = col_map.get('qta')
        qta = clean_number(row_data[qta_idx]) if qta_idx is not None and qta_idx < len(row_data) else 0

        fvm_idx = col_map.get('fvm')
        fvm = clean_number(row_data[fvm_idx]) if fvm_idx is not None and fvm_idx < len(row_data) else 0

        ruolo = extract_ruolo(row)

        players.append({
            'id': f"p_{nome.lower().replace(' ', '_').replace('.', '')}",
            'ruolo': ruolo,
            'nome': nome,
            'squadra': squadra,
            'qta': qta,
            'fvm': fvm
        })

    return players


STAT_ALIASES = {
    'PV': 'pv', 'PRESENZE': 'pv', 'MV': 'mv', 'FM': 'fm', 'FANTAMEDIA': 'fm',
    'GF': 'gf', 'GOL': 'gf', 'GS': 'gs', 'RP': 'rp', 'RC': 'rc',
    'R+': 'rpos', 'R-': 'rneg', 'ASS': 'ass', 'ASSIST': 'ass',
    'AMM': 'amm', 'ESP': 'esp', 'AU': 'au',
}
STAT_FIELDS = ('pv', 'mv', 'fm', 'gf', 'gs', 'rp', 'rc', 'rpos', 'rneg', 'ass', 'amm', 'esp', 'au')
STATS_CSV_HEADERS = ['nome', 'squadra'] + list(STAT_FIELDS)


def parse_statistiche(html):
    """Estrae le statistiche dalla tabella di fantacalcio.it."""
    table, headers = extract_from_table(html, ['PV', 'MV', 'FANTAMEDIA', 'PRESENZE'])
    if not table:
        print("ATTENZIONE: tabella statistiche non trovata, struttura pagina cambiata", file=sys.stderr)
        return []

    col_map = {}
    squadra_idx = None
    for i, h in enumerate(headers):
        h_clean = h.upper().replace(' ', '').replace('.', '')
        if h_clean in ('SQ', 'SQUADRA', 'TEAM') and squadra_idx is None:
            squadra_idx = i
        elif STAT_ALIASES.get(h_clean) and STAT_ALIASES[h_clean] not in col_map:
            col_map[STAT_ALIASES[h_clean]] = i

    if squadra_idx is None or squadra_idx == 0:
        print("ATTENZIONE: colonna 'Sq' non trovata nella tabella statistiche", file=sys.stderr)
        return []

    col_map['squadra'] = squadra_idx
    col_map['nome'] = squadra_idx - 1
    print(f"   colonne statistiche riconosciute: {sorted(k for k in col_map)}", file=sys.stderr)

    stats = []
    rows = table.find_all('tr')[1:]
    for row in rows:
        cols = row.find_all(['td', 'th'])
        row_data = [c.get_text(strip=True) for c in cols]

        nome_idx = col_map['nome']
        if nome_idx >= len(row_data):
            continue
        nome = row_data[nome_idx]
        if not nome or len(nome) < 2:
            continue

        entry = {
            'nome': nome,
            'squadra': row_data[col_map['squadra']] if col_map['squadra'] < len(row_data) else '',
        }
        for field in STAT_FIELDS:
            idx = col_map.get(field)
            entry[field] = clean_number(row_data[idx]) if idx is not None and idx < len(row_data) else 0

        stats.append(entry)

    return stats


# ---------------------------------------------------------------------------
# INDISPONIBILI
# ---------------------------------------------------------------------------
# La pagina e' organizzata per squadra: stemma + nome squadra, poi tre blocchi
# "Infortunati / Squalificati / Diffidati", ogni giocatore con il nome in
# grassetto e sotto il testo libero della redazione. Non esiste archivio
# storico: la pagina mostra solo la situazione ATTUALE.
# I diffidati non vengono inclusi (sono ancora disponibili).
#
# Per non dipendere da un'unica struttura HTML ci sono DUE parser
# indipendenti (uno guidato dai tag, uno dal testo); si usa quello che
# estrae piu' record. Se nessuno dei due trova abbastanza dati la fase
# fallisce in modo rumoroso e il CSV precedente resta com'e'.

CATEGORY_LABELS = {'infortunati', 'squalificati', 'diffidati'}
SECTION_END_RE = re.compile(
    r'^(indisponibili della serie a|squalificati della serie a|'
    r'infortunati della serie a|prossimo turno|classifica)\s*$', re.I)
KNOWN_TEAMS_FALLBACK = {
    'Atalanta', 'Bologna', 'Cagliari', 'Como', 'Cremonese', 'Empoli', 'Fiorentina',
    'Frosinone', 'Genoa', 'Hellas Verona', 'Verona', 'Inter', 'Juventus', 'Lazio',
    'Lecce', 'Milan', 'Monza', 'Napoli', 'Parma', 'Pisa', 'Roma', 'Salernitana',
    'Sampdoria', 'Sassuolo', 'Spezia', 'Torino', 'Udinese', 'Venezia',
}
NAME_PARTICLES = {'de', 'di', 'da', 'del', 'della', 'dal', 'van', 'von', 'der', 'den',
                  'dos', 'la', 'le', 'el', 'al', 'ben', 'ter', 'ten', 'mc'}


def _norm_label(text):
    return re.sub(r'\s+', ' ', text or '').strip().strip('-:•– ').lower()


def _known_team_names(soup):
    names = set()
    for img in soup.find_all('img'):
        label = (img.get('alt') or '').strip()
        if label.lower().startswith('stemma'):
            n = label[len('stemma'):].strip(' :-')
            if n:
                names.add(n)
    return names or set(KNOWN_TEAMS_FALLBACK)


def _detail_after(tag):
    """
    Testo che segue il nome del giocatore (causa e tempi di rientro), senza
    sconfinare nel giocatore successivo. Se il nome e' avvolto in un
    contenitore (<p>), sale di un livello alla volta (max 3).
    """
    node = tag
    for _ in range(3):
        parts = []
        for sib in node.next_siblings:
            if isinstance(sib, Comment):
                continue
            if isinstance(sib, Tag):
                if (sib.name in ('strong', 'b', 'li', 'ul', 'ol', 'img', 'h2', 'h3', 'h4', 'h5', 'h6')
                        or sib.find(['strong', 'b', 'img'])):
                    break
                parts.append(sib.get_text(' ', strip=True))
            elif isinstance(sib, NavigableString):
                parts.append(str(sib).strip())
        text = re.sub(r'\s+', ' ', ' '.join(p for p in parts if p)).strip(' :-–')
        if text:
            return text
        parent = node.parent
        if parent is None or parent.name in ('ul', 'ol', 'body', 'html', 'section', 'article', 'main'):
            break
        node = parent
    return ''


def _parse_indisponibili_tags(soup, default_category='infortunati'):
    """Parser a tag: stemma -> squadra, <a>/<strong> di categoria, <strong> = giocatore."""
    records = []
    current_team = None
    current_category = None

    for tag in soup.find_all(['img', 'a', 'strong', 'b', 'h2', 'h3', 'h4', 'h5', 'h6']):
        if tag.name == 'img':
            label = (tag.get('alt') or tag.get('title') or '').strip()
            if label.lower().startswith('stemma'):
                current_team = label[len('stemma'):].strip(' :-') or label
                current_category = default_category  # ogni squadra riparte da qui
            continue

        text = tag.get_text(strip=True)
        low = _norm_label(text)

        if tag.name in ('h2', 'h3', 'h4', 'h5', 'h6'):
            if records and SECTION_END_RE.match(text.strip()):
                break  # finita la parte per squadre: SEO, prossimo turno, classifica
            if low in CATEGORY_LABELS:
                current_category = low
            continue

        if tag.name == 'a':
            if len(low) <= 20 and low in CATEGORY_LABELS:
                current_category = low
            continue

        # strong / b
        if low in CATEGORY_LABELS:
            current_category = low
            continue
        if current_team and current_category in ('infortunati', 'squalificati'):
            if not text or low == 'nessuno' or len(text) <= 1 or len(text) > 40:
                continue
            records.append({
                'nome': text,
                'squadra': current_team,
                'categoria': current_category,
                'dettaglio': _detail_after(tag),
            })
    return records


def _looks_like_name(s):
    """True per stringhe tipo 'Kristensen T.', 'Pellegrini Lo.', 'De Silvestri'."""
    if not s or len(s) > 35 or ',' in s or any(ch.isdigit() for ch in s):
        return False
    words = s.split()
    if not 1 <= len(words) <= 4:
        return False
    for w in words:
        if w.lower() in NAME_PARTICLES:
            continue
        if not w[0].isupper():
            return False
    return True


def _parse_indisponibili_text(soup, team_names, default_category='infortunati'):
    """
    Parser a testo: linearizza la pagina e segue la sequenza
    squadra -> 'Infortunati' -> (nome, descrizione)* -> 'Squalificati' ...
    Non dipende da tag, classi o alt delle immagini.
    """
    raw = soup.get_text('\n').split('\n')
    lines = [re.sub(r'\s+', ' ', l).strip() for l in raw]
    lines = [l for l in lines if l]
    n = len(lines)
    names_l = {t.lower(): t for t in team_names}

    def is_label(k):
        return k < n and _norm_label(lines[k]) in CATEGORY_LABELS

    def is_team(k):
        return k < n and _norm_label(lines[k]) in names_l

    def starts_player(k):
        return (k + 1 < n and _looks_like_name(lines[k]) and not is_label(k) and not is_team(k)
                and _norm_label(lines[k]) != 'nessuno'
                and not is_label(k + 1) and not is_team(k + 1) and len(lines[k + 1]) >= 20)

    records = []
    team = None
    cat = None
    i = 0
    while i < n:
        line = lines[i]
        low = _norm_label(line)
        if records and SECTION_END_RE.match(line):
            break
        if low in names_l and is_label(i + 1):
            team = names_l[low]
            cat = default_category
            i += 1
            continue
        if low in CATEGORY_LABELS:
            cat = low
            i += 1
            continue
        if team and cat in ('infortunati', 'squalificati') and starts_player(i):
            parts = [lines[i + 1]]
            j = i + 2
            while (j < n and not is_label(j) and not is_team(j)
                   and not starts_player(j) and not SECTION_END_RE.match(lines[j])):
                parts.append(lines[j])
                j += 1
            records.append({
                'nome': line, 'squadra': team, 'categoria': cat,
                'dettaglio': ' '.join(parts).strip(),
            })
            i = j
            continue
        i += 1
    return records


def parse_indisponibili(html):
    """Estrae infortunati + squalificati (con squadra e testo della redazione)."""
    soup = BeautifulSoup(html, 'html.parser')
    teams = _known_team_names(soup)
    by_tags = _parse_indisponibili_tags(soup)
    by_text = _parse_indisponibili_text(soup, teams)
    print(f"   indisponibili: {len(by_tags)} record dal parser a tag, "
          f"{len(by_text)} dal parser a testo ({len(teams)} squadre note)", file=sys.stderr)
    # Si sceglie il parser che riconosce piu' squadre diverse (segno che
    # attribuisce bene i giocatori), a parita' quello con piu' record.
    def score(rs):
        return (len({r['squadra'] for r in rs}), len(rs))
    records = by_tags if score(by_tags) >= score(by_text) else by_text

    # Rimuove duplicati esatti mantenendo l'ordine di apparizione
    seen = set()
    unique_records = []
    for r in records:
        key = (r['nome'], r['squadra'], r['categoria'])
        if key not in seen:
            seen.add(key)
            unique_records.append(r)

    n_teams = len({r['squadra'] for r in unique_records})
    n_inj = sum(1 for r in unique_records if r['categoria'] == 'infortunati')
    n_sq = sum(1 for r in unique_records if r['categoria'] == 'squalificati')
    print(f"   indisponibili scelti: {len(unique_records)} ({n_inj} infortunati, "
          f"{n_sq} squalificati) su {n_teams} squadre", file=sys.stderr)
    return unique_records


def save_csv(data, path, headers):
    if not data:
        return False
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(data)
    return True


def save_debug_html(html, path):
    with open(path, 'w', encoding='utf-8') as f:
        f.write(html)


# Voti ufficiali per singola giornata (una pagina per giornata).
VOTI_URL_TEMPLATE = "https://www.fantacalcio.it/voti-fantacalcio-serie-a/{season}/{giornata}"
VOTI_ALIASES = {
    'VOTO': 'voto', 'V': 'voto', 'FV': 'fantavoto', 'FANTAVOTO': 'fantavoto',
    'GF': 'gf', 'GOL': 'gf', 'GS': 'gs', 'RP': 'rp', 'RC': 'rc',
    'R+': 'rpos', 'R-': 'rneg', 'ASS': 'ass', 'AMM': 'amm', 'ESP': 'esp', 'AU': 'au',
}
VOTI_FIELDS = ('voto', 'fantavoto', 'gf', 'gs', 'rp', 'rc', 'rpos', 'rneg', 'ass', 'amm', 'esp', 'au')
VOTI_CSV_HEADERS = ['nome', 'squadra', 'giornata'] + list(VOTI_FIELDS)


def csv_has_column(path, col):
    try:
        with open(path, encoding='utf-8') as f:
            return col in f.readline().strip().split(',')
    except OSError:
        return False


def parse_voti_giornata(html, giornata):
    """Estrae voto, fantavoto ed eventi (gol, assist, amm...) di una giornata.
    Parser guidato dagli header, come per le statistiche: cerca ogni tabella
    che abbia una colonna 'Voto'. Se manca la colonna Fantavoto lo calcola
    con i bonus/malus standard (gol +3, assist +1, amm -0.5, esp -1,
    rigore parato +3, rigore sbagliato -3, autogol -2; esclusi gol subiti)."""
    soup = BeautifulSoup(html, 'html.parser')
    out = []
    for table in soup.find_all('table'):
        rows = table.find_all('tr')
        if not rows:
            continue
        cmap = {}
        for i, h in enumerate(build_header_labels(rows[0])):
            k = VOTI_ALIASES.get(h.upper().replace(' ', '').replace('.', ''))
            if k and k not in cmap:
                cmap[k] = i
        if 'voto' not in cmap:
            continue
        heading = table.find_previous(['h2', 'h3', 'h4'])
        squadra = heading.get_text(strip=True) if heading else ''
        for row in rows[1:]:
            cells = row.find_all(['td', 'th'])
            texts = [c.get_text(strip=True) for c in cells]
            nome = ''
            for t in texts:
                if len(re.findall(r'[A-Za-z\u00c0-\u00ff]', t)) >= 3 and not re.fullmatch(r'[\d.,\-+ ]+', t):
                    nome = t
                    break

            def val(k):
                i = cmap.get(k)
                if i is None or i >= len(texts):
                    return None
                try:
                    return float(texts[i].replace(',', '.'))
                except ValueError:
                    return None

            voto = val('voto')
            if not nome or voto is None:
                continue
            e = {k: (val(k) or 0) for k in VOTI_FIELDS if k not in ('voto', 'fantavoto')}
            fv = val('fantavoto')
            if fv is None:
                fv = (voto + 3 * e['gf'] + e['ass'] - 0.5 * e['amm'] - e['esp']
                      + 3 * e['rp'] - 3 * e['rneg'] - 2 * e['au'])
            e.update({'nome': nome, 'squadra': squadra, 'giornata': giornata, 'voto': voto, 'fantavoto': fv})
            out.append(e)
    return out


def scrape_voti_giornate():
    """Scarica i voti di tutte le giornate giocate -> data/voti_giornate.csv.
    Non blocca il run se fallisce: e' un dato aggiuntivo."""
    season = current_season_label()
    all_rows, found, missing = [], 0, 0
    for g in range(1, 39):
        html = fetch_page(VOTI_URL_TEMPLATE.format(season=season, giornata=g), retries=1)
        if html and g == 1:
            save_debug_html(html, "data/debug_voti.html")
        rows = parse_voti_giornata(html, g) if html else []
        if rows:
            all_rows.extend(rows)
            found += 1
            missing = 0
        else:
            missing += 1
            if (found and missing >= 2) or (not found and missing >= 3):
                break
        time.sleep(0.5)
    if save_csv(all_rows, "data/voti_giornate.csv", VOTI_CSV_HEADERS):
        print(f"OK: data/voti_giornate.csv ({len(all_rows)} righe, {found} giornate)")
        return True
    print("ATTENZIONE: nessun voto di giornata estratto, vedi data/debug_voti.html "
          "(URL/struttura pagina da verificare)", file=sys.stderr)
    return False


# ---------------------------------------------------------------------------
# FASI (ognuna isolata dalle altre)
# ---------------------------------------------------------------------------

def stage_indisponibili():
    print("Scarico indisponibili...")
    html = fetch_page(URLS["indisponibili"])
    if not html:
        return False
    save_debug_html(html, "data/debug_indisponibili.html")
    injured = parse_indisponibili(html)
    n_teams = len({r['squadra'] for r in injured})
    if len(injured) < 5 or n_teams < 3:
        print(f"ERRORE: estratti solo {len(injured)} indisponibili su {n_teams} squadre: "
              f"struttura pagina cambiata? data/indisponibili.csv NON aggiornato "
              f"(resta la versione precedente).", file=sys.stderr)
        m = re.search(r'id="team-\d+"', html)
        start = m.start() if m else max(html.find('Infortunati') - 300, 0)
        print("   --- estratto HTML per diagnosi ---", file=sys.stderr)
        print(html[start:start + 1500], file=sys.stderr)
        print("   --- fine estratto ---", file=sys.stderr)
        return False
    headers = ['nome', 'squadra', 'categoria', 'dettaglio']
    if save_csv(injured, "data/indisponibili.csv", headers):
        print(f"OK: data/indisponibili.csv ({len(injured)} giocatori)")
        return True
    print("ERRORE: impossibile salvare indisponibili.csv", file=sys.stderr)
    return False


def stage_quotazioni():
    print("Scarico quotazioni...")
    html = fetch_page(URLS["quotazioni"])
    if not html:
        return False
    save_debug_html(html, "data/debug_quotazioni.html")
    players = parse_quotazioni(html)
    if not players:
        print("ERRORE: nessun giocatore estratto dalle quotazioni", file=sys.stderr)
        print("   HTML salvato in data/debug_quotazioni.html per analisi", file=sys.stderr)
        return False
    headers = ['id', 'ruolo', 'nome', 'squadra', 'qta', 'fvm']
    if not save_csv(players, "data/quotazioni.csv", headers):
        print("ERRORE: impossibile salvare quotazioni.csv", file=sys.stderr)
        return False
    print(f"OK: data/quotazioni.csv ({len(players)} giocatori)")
    n_unknown = sum(1 for p in players if p['ruolo'] == '?')
    if n_unknown:
        print(f"   ATTENZIONE: ruolo non riconosciuto per {n_unknown} giocatori "
              f"(vedi commento in extract_ruolo)", file=sys.stderr)
    return True


def stage_statistiche():
    print("Scarico statistiche...")
    html = fetch_page(URLS["statistiche"])
    if not html:
        return False
    save_debug_html(html, "data/debug_statistiche.html")
    stats = parse_statistiche(html)
    if not stats:
        print("ERRORE: nessuna statistica estratta", file=sys.stderr)
        print("   HTML salvato in data/debug_statistiche.html per analisi", file=sys.stderr)
        return False
    if not save_csv(stats, "data/stats_curr.csv", STATS_CSV_HEADERS):
        print("ERRORE: impossibile salvare stats_curr.csv", file=sys.stderr)
        return False
    print(f"OK: data/stats_curr.csv ({len(stats)} giocatori)")
    return True


def stage_stats_prev():
    # Statistiche stagione precedente: dati storici, non cambiano più,
    # quindi si scaricano una sola volta e poi si salta se già presenti.
    prev_path = "data/stats_prev.csv"
    if os.path.exists(prev_path) and csv_has_column(prev_path, 'gf'):
        print(f"Statistiche stagione precedente già presenti in {prev_path}, salto il download.")
        return True
    prev_season = previous_season_label()
    print(f"Statistiche stagione precedente mancanti, scarico {prev_season}...")
    prev_url = STATS_SEASON_URL_TEMPLATE.format(season=prev_season)
    html_prev = fetch_page(prev_url)
    if not html_prev:
        return False
    save_debug_html(html_prev, "data/debug_statistiche_prev.html")
    stats_prev = parse_statistiche(html_prev)
    if not stats_prev:
        print(f"ERRORE: nessuna statistica estratta per la stagione {prev_season} "
              f"(controlla che l'URL/formato stagione sia corretto: {prev_url})", file=sys.stderr)
        return False
    if not save_csv(stats_prev, prev_path, STATS_CSV_HEADERS):
        print(f"ERRORE: impossibile salvare {prev_path}", file=sys.stderr)
        return False
    print(f"OK: {prev_path} ({len(stats_prev)} giocatori, stagione {prev_season})")
    return True


def stage_voti():
    print("Scarico voti per giornata...")
    return scrape_voti_giornate()


def run_stage(label, fn, critical, failures, warnings):
    try:
        ok = fn()
    except Exception:
        print(f"ECCEZIONE nella fase '{label}':", file=sys.stderr)
        traceback.print_exc()
        ok = False
    if not ok:
        (failures if critical else warnings).append(label)


def main():
    os.makedirs("data", exist_ok=True)
    failures, warnings = [], []

    # Gli indisponibili cambiano ogni giorno: prima di tutto, cosi' nessun
    # problema nelle altre fasi puo' impedirne l'aggiornamento.
    run_stage("indisponibili", stage_indisponibili, True, failures, warnings)
    time.sleep(1)
    run_stage("quotazioni", stage_quotazioni, True, failures, warnings)
    time.sleep(1)
    run_stage("statistiche", stage_statistiche, True, failures, warnings)
    time.sleep(1)
    # Dati "nice to have": un loro fallimento non fa fallire il run.
    run_stage("statistiche stagione precedente", stage_stats_prev, False, failures, warnings)
    run_stage("voti per giornata", stage_voti, False, failures, warnings)

    if warnings:
        print(f"ATTENZIONE: fasi non critiche fallite: {', '.join(warnings)}", file=sys.stderr)
    if failures:
        print(f"ERRORE: fasi critiche fallite: {', '.join(failures)}", file=sys.stderr)
        sys.exit(1)
    print("Tutti i dati scaricati con successo.")


if __name__ == "__main__":
    main()
