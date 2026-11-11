"""
Scarica quotazioni, statistiche e indisponibili da fantacalcio.it.
"""
import os
import sys
import re
import csv
import requests
from datetime import date
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
}

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



def fetch_page(url):
    """Scarica una pagina HTML, con diagnostica su status/dimensione/blocchi."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        print(f"   -> {url}: status={resp.status_code}, bytes={len(resp.content)}", file=sys.stderr)
        resp.raise_for_status()

        text_lower = resp.text.lower()
        hits = [m for m in BLOCK_MARKERS if m in text_lower]
        if hits:
            print(f"   SOSPETTO BLOCCO/CHALLENGE su {url}: marker trovati={hits}", file=sys.stderr)

        return resp.text
    except requests.RequestException as e:
        print(f"ERRORE scaricando {url}: {e}", file=sys.stderr)
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
        elif h_clean == 'PV' and 'pv' not in col_map:
            col_map['pv'] = i
        elif h_clean == 'MV' and 'mv' not in col_map:
            col_map['mv'] = i
        elif h_clean == 'FM' and 'fm' not in col_map:
            col_map['fm'] = i

    if squadra_idx is None or squadra_idx == 0:
        print("ATTENZIONE: colonna 'Sq' non trovata nella tabella statistiche", file=sys.stderr)
        return []

    col_map['squadra'] = squadra_idx
    col_map['nome'] = squadra_idx - 1

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
        for field in ('pv', 'mv', 'fm'):
            idx = col_map.get(field)
            entry[field] = clean_number(row_data[idx]) if idx is not None and idx < len(row_data) else 0

        stats.append(entry)

    return stats


def parse_indisponibili(html):
    """
    Estrae i calciatori indisponibili (infortunati + squalificati), con
    squadra e dettaglio (causa e tempi di rientro, testo libero così come
    pubblicato dal sito - non lo spezzo in campi separati "causa"/"data"
    perché il sito scrive tutto in prosa libera con formulazioni non
    standardizzate, es. "da metà novembre", "recuperabile da inizio
    ottobre": una regex sarebbe troppo fragile).

    ATTENZIONE: questa pagina mostra solo la situazione ATTUALE. Non
    esiste un archivio storico degli indisponibili per stagioni passate
    su fantacalcio.it (a differenza delle statistiche), quindi non c'è
    parametro stagione da gestire qui.

    I diffidati non vengono inclusi: sono ancora disponibili, solo a
    rischio squalifica alla prossima ammonizione.
    """
    soup = BeautifulSoup(html, 'html.parser')
    records = []
    current_team = None
    current_category = None
    category_labels = {'infortunati', 'squalificati', 'diffidati'}

    for tag in soup.find_all(['img', 'a', 'strong', 'b']):
        if tag.name == 'img':
            alt = (tag.get('alt') or '').strip()
            if alt.lower().startswith('stemma'):
                current_team = alt[len('Stemma'):].strip() or alt
                current_category = 'infortunati'  # ogni squadra riparte da qui
            continue

        text = tag.get_text(strip=True)
        text_lower = text.lower().strip('- ')

        if tag.name == 'a' and 'infortunati' in text_lower:
            current_category = 'infortunati'
            continue

        if text_lower in category_labels:
            current_category = text_lower
            continue

        if tag.name in ('strong', 'b') and current_team and current_category in ('infortunati', 'squalificati'):
            if not text or text == 'Nessuno' or len(text) <= 1:
                continue

            dettaglio = ''
            parent = tag.find_parent(['li', 'p', 'div'])
            if parent:
                full_text = parent.get_text(' ', strip=True)
                dettaglio = full_text[len(text):].strip(' :-') if full_text.startswith(text) else full_text

            records.append({
                'nome': text,
                'squadra': current_team or '',
                'categoria': current_category,
                'dettaglio': dettaglio,
            })

    if not records:
        print("ATTENZIONE: nessun indisponibile estratto, struttura pagina cambiata", file=sys.stderr)

    # Rimuove eventuali duplicati esatti mantenendo l'ordine di apparizione
    seen = set()
    unique_records = []
    for r in records:
        key = (r['nome'], r['squadra'], r['categoria'])
        if key not in seen:
            seen.add(key)
            unique_records.append(r)

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


def main():
    os.makedirs("data", exist_ok=True)
    ok = True

    print("Scarico quotazioni...")
    html = fetch_page(URLS["quotazioni"])
    if html:
        save_debug_html(html, "data/debug_quotazioni.html")
        players = parse_quotazioni(html)
        if players:
            headers = ['id', 'ruolo', 'nome', 'squadra', 'qta', 'fvm']
            if save_csv(players, "data/quotazioni.csv", headers):
                print(f"OK: data/quotazioni.csv ({len(players)} giocatori)")
                n_unknown = sum(1 for p in players if p['ruolo'] == '?')
                if n_unknown:
                    print(f"   ATTENZIONE: ruolo non riconosciuto per {n_unknown} giocatori "
                          f"(vedi commento in extract_ruolo)", file=sys.stderr)
            else:
                print("ERRORE: impossibile salvare quotazioni.csv", file=sys.stderr)
                ok = False
        else:
            print("ERRORE: nessun giocatore estratto dalle quotazioni", file=sys.stderr)
            print("   HTML salvato in data/debug_quotazioni.html per analisi", file=sys.stderr)
            ok = False
    else:
        ok = False

    print("Scarico statistiche...")
    html = fetch_page(URLS["statistiche"])
    if html:
        save_debug_html(html, "data/debug_statistiche.html")
        stats = parse_statistiche(html)
        if stats:
            headers = ['nome', 'squadra', 'pv', 'mv', 'fm']
            if save_csv(stats, "data/stats_curr.csv", headers):
                print(f"OK: data/stats_curr.csv ({len(stats)} giocatori)")
            else:
                print("ERRORE: impossibile salvare stats_curr.csv", file=sys.stderr)
                ok = False
        else:
            print("ERRORE: nessuna statistica estratta", file=sys.stderr)
            print("   HTML salvato in data/debug_statistiche.html per analisi", file=sys.stderr)
            ok = False
    else:
        ok = False

    # 2b. Statistiche stagione precedente: dati storici, non cambiano più,
    # quindi si scaricano una sola volta e poi si salta se già presenti.
    prev_path = "data/stats_prev.csv"
    if os.path.exists(prev_path):
        print(f"Statistiche stagione precedente già presenti in {prev_path}, salto il download.")
    else:
        prev_season = previous_season_label()
        print(f"Statistiche stagione precedente mancanti, scarico {prev_season}...")
        prev_url = STATS_SEASON_URL_TEMPLATE.format(season=prev_season)
        html_prev = fetch_page(prev_url)
        if html_prev:
            save_debug_html(html_prev, "data/debug_statistiche_prev.html")
            stats_prev = parse_statistiche(html_prev)
            if stats_prev:
                headers = ['nome', 'squadra', 'pv', 'mv', 'fm']
                if save_csv(stats_prev, prev_path, headers):
                    print(f"OK: {prev_path} ({len(stats_prev)} giocatori, stagione {prev_season})")
                else:
                    print(f"ERRORE: impossibile salvare {prev_path}", file=sys.stderr)
            else:
                print(f"ERRORE: nessuna statistica estratta per la stagione {prev_season} "
                      f"(controlla che l'URL/formato stagione sia corretto: {prev_url})", file=sys.stderr)
        # Non blocchiamo l'intero run per un fallimento sul backfill storico:
        # è un'informazione "nice to have", non i dati live dell'asta.

    print("Scarico indisponibili...")
    html = fetch_page(URLS["indisponibili"])
    if html:
        save_debug_html(html, "data/debug_indisponibili.html")
        injured = parse_indisponibili(html)
        if injured:
            headers = ['nome', 'squadra', 'categoria', 'dettaglio']
            if save_csv(injured, "data/indisponibili.csv", headers):
                print(f"OK: data/indisponibili.csv ({len(injured)} giocatori)")
            else:
                print("ERRORE: impossibile salvare indisponibili.csv", file=sys.stderr)

    if not ok:
        print("ERRORE: alcuni file non sono stati scaricati/estratti correttamente", file=sys.stderr)
        sys.exit(1)
    else:
        print("Tutti i dati scaricati con successo.")


if __name__ == "__main__":
    main()
