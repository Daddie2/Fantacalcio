"""
Scarica quotazioni, statistiche e indisponibili da fantacalcio.it.
"""
import os
import sys
import re
import csv
import requests
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

BLOCK_MARKERS = [
    "captcha", "attention required", "access denied",
    "just a moment", "checking your browser", "are you human",
]


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
    Estrae il ruolo (P/D/C/A) da una riga della tabella quotazioni.
    Il ruolo è quasi certamente un'icona (non testo semplice): si cerca
    in ordine classe CSS -> alt/title immagine -> testo cella.

    Se ritorna sempre '?', apri data/debug_quotazioni.html, guarda come
    è marcato il ruolo in una riga giocatore (classe tipo "ruolo-p",
    <img alt="Portiere">, <svg><use href="#icon-p">...) e adatta i
    pattern sotto di conseguenza.
    """
    valid = {'P', 'D', 'C', 'A'}

    for tag in row.find_all(attrs={"class": True}):
        classes = ' '.join(tag.get('class', [])).lower()
        m = re.search(r'(?:ruolo|role|^r)[-_]?([pdca])\b', classes)
        if m:
            letter = m.group(1).upper()
            if letter in valid:
                return letter

    for tag in row.find_all(['img', 'span', 'div']):
        for attr in ('alt', 'title'):
            val = (tag.get(attr) or '').strip().upper()
            if val in valid:
                return val
            if val.startswith('PORTIERE'):
                return 'P'
            if val.startswith('DIFENSORE'):
                return 'D'
            if val.startswith('CENTROCAMPISTA'):
                return 'C'
            if val.startswith('ATTACCANTE'):
                return 'A'

    cells = row.find_all(['td', 'th'])[:4]
    for cell in cells:
        text = cell.get_text(strip=True).upper()
        if text in valid:
            return text

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
    debug_row_ok = None
    debug_row_unknown = None
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

        if ruolo != '?' and debug_row_ok is None:
            debug_row_ok = (nome, row)
        if ruolo == '?' and debug_row_unknown is None:
            debug_row_unknown = (nome, row)

        players.append({
            'id': f"p_{nome.lower().replace(' ', '_').replace('.', '')}",
            'ruolo': ruolo,
            'nome': nome,
            'squadra': squadra,
            'qta': qta,
            'fvm': fvm
        })

    if debug_row_ok and debug_row_unknown:
        print("\n--- DEBUG RUOLO: riga con ruolo RICONOSCIUTO "
              f"({debug_row_ok[0]}) ---", file=sys.stderr)
        print(debug_row_ok[1].prettify(), file=sys.stderr)
        print("\n--- DEBUG RUOLO: riga con ruolo NON RICONOSCIUTO "
              f"({debug_row_unknown[0]}) ---", file=sys.stderr)
        print(debug_row_unknown[1].prettify(), file=sys.stderr)
        print("--- FINE DEBUG RUOLO ---\n", file=sys.stderr)

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
    Estrae i nomi dei calciatori indisponibili (infortunati + squalificati).

    ATTENZIONE: questa pagina NON è una tabella. È organizzata per squadra:
    icona/nome squadra -> sezione "Infortunati" (nomi in grassetto con
    descrizione) -> sezione "Squalificati" -> sezione "Diffidati".
    I diffidati non vengono inclusi: sono ancora disponibili, solo a
    rischio squalifica alla prossima ammonizione.
    """
    soup = BeautifulSoup(html, 'html.parser')
    injured = []
    current_category = None
    category_labels = {'infortunati', 'squalificati', 'diffidati'}

    for tag in soup.find_all(['img', 'a', 'strong', 'b']):
        if tag.name == 'img':
            alt = (tag.get('alt') or '').strip().lower()
            if alt.startswith('stemma'):
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

        if tag.name in ('strong', 'b') and current_category in ('infortunati', 'squalificati'):
            if text and text != 'Nessuno' and len(text) > 1:
                injured.append(text)

    if not injured:
        print("ATTENZIONE: nessun indisponibile estratto, struttura pagina cambiata", file=sys.stderr)

    return sorted(set(injured))


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

    print("Scarico indisponibili...")
    html = fetch_page(URLS["indisponibili"])
    if html:
        save_debug_html(html, "data/debug_indisponibili.html")
        injured = parse_indisponibili(html)
        if injured:
            with open("data/indisponibili.txt", "w", encoding="utf-8") as f:
                for name in injured:
                    f.write(f"{name}\n")
            print(f"OK: data/indisponibili.txt ({len(injured)} nomi)")

    if not ok:
        print("ERRORE: alcuni file non sono stati scaricati/estratti correttamente", file=sys.stderr)
        sys.exit(1)
    else:
        print("Tutti i dati scaricati con successo.")


if __name__ == "__main__":
    main()