""" Scarica i dati di quotazioni e statistiche da fantacalcio.it usando il parsing della tabella HTML reale del sito. """
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


def fetch_page(url):
    """Scarica una pagina HTML."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as e:
        print(f"ERRORE scaricando {url}: {e}", file=sys.stderr)
        return None


def clean_number(val):
    """Pulisce un valore numerico da stringa (es. '1.234,5' -> 1234.5)."""
    if not val:
        return 0
    clean = re.sub(r'[^\d,.\-]', '', str(val))
    # Se ci sono sia punto che virgola, il punto è separatore delle migliaia
    if ',' in clean and '.' in clean:
        clean = clean.replace('.', '').replace(',', '.')
    else:
        clean = clean.replace(',', '.')
    try:
        return float(clean)
    except ValueError:
        return 0


def extract_from_table(html, required_keywords):
    """Trova la tabella giusta cercando keyword negli header."""
    soup = BeautifulSoup(html, 'html.parser')
    tables = soup.find_all('table')

    for table in tables:
        header_row = table.find('tr')
        if not header_row:
            continue
        headers = [c.get_text(strip=True) for c in header_row.find_all(['th', 'td'])]
        header_text = ' '.join(headers).upper()
        if any(k in header_text for k in required_keywords):
            return table, headers

    return None, None


def extract_ruolo(row):
    """ Estrae il ruolo (P/D/C/A) da una riga della tabella quotazioni. Il ruolo su fantacalcio.it è quasi sempre un'icona, non testo semplice, quindi si cerca in ordine: classe CSS -> alt/title immagine -> testo cella. Se questa funzione restituisce sempre 'C' (fallback), apri data/debug_quotazioni.html, individua la riga di un giocatore e guarda come è marcato il ruolo (classe tipo "ruolo-p", un'icona <img alt="Portiere">, o uno <svg><use href="#icon-p">), poi adatta i pattern qui sotto di conseguenza. """
    valid = {'P', 'D', 'C', 'A'}

    # 1. Classi CSS tipo "ruolo-p", "role-p", "r-p"
    for tag in row.find_all(attrs={"class": True}):
        classes = ' '.join(tag.get('class', [])).lower()
        m = re.search(r'(?:ruolo|role|^r)[-_]?([pdca])\b', classes)
        if m:
            letter = m.group(1).upper()
            if letter in valid:
                return letter

    # 2. Attributi alt/title di immagini o span con testo del ruolo
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

    # 3. Testo semplice in una delle prime celle (fallback raro)
    cells = row.find_all(['td', 'th'])[:3]
    for cell in cells:
        text = cell.get_text(strip=True).upper()
        if text in valid:
            return text

    # Nessun ruolo trovato: fallback esplicito, NON nascondere l'errore
    return '?'


def parse_quotazioni(html):
    """Estrae le quotazioni dalla tabella reale di fantacalcio.it."""
    table, headers = extract_from_table(html, ['CALCIATORE', 'FVM', 'QUOTAZ'])
    if not table:
        return []

    # Header reali: [.., .., ..], Calciatore, Sq, QI, QA, FVM/1000 (Classic),
    # QI, QA, FVM/1000 (Mantra), ..
    # QI/QA/FVM sono duplicati (Classic + Mantra): prendiamo solo la prima
    # occorrenza di ciascuno, che corrisponde alla modalità Classic.
    col_map = {}
    seen = {'qi': False, 'qta': False, 'fvm': False}
    for i, h in enumerate(headers):
        h_clean = h.upper().replace(' ', '').replace('.', '').replace('/', '')
        if h_clean in ('CALCIATORE', 'NOME', 'GIOCATORE') and 'nome' not in col_map:
            col_map['nome'] = i
        elif h_clean in ('SQ', 'SQUADRA', 'TEAM') and 'squadra' not in col_map:
            col_map['squadra'] = i
        elif h_clean == 'QI' and not seen['qi']:
            col_map['qi'] = i
            seen['qi'] = True
        elif h_clean == 'QA' and not seen['qta']:
            col_map['qta'] = i
            seen['qta'] = True
        elif 'FVM' in h_clean and not seen['fvm']:
            col_map['fvm'] = i
            seen['fvm'] = True

    if 'nome' not in col_map:
        print("ATTENZIONE: header 'Calciatore' non trovato, struttura pagina cambiata", file=sys.stderr)
        return []

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

        squadra_idx = col_map.get('squadra')
        squadra = row_data[squadra_idx] if squadra_idx is not None and squadra_idx < len(row_data) else ''

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
        return []

    col_map = {}
    for i, h in enumerate(headers):
        h_clean = h.upper().replace(' ', '').replace('.', '')
        if ('NOME' in h_clean or 'CALCIATORE' in h_clean) and 'nome' not in col_map:
            col_map['nome'] = i
        elif ('SQUADRA' in h_clean or h_clean == 'SQ') and 'squadra' not in col_map:
            col_map['squadra'] = i
        elif h_clean == 'PV' and 'pv' not in col_map:
            col_map['pv'] = i
        elif h_clean == 'MV' and 'mv' not in col_map:
            col_map['mv'] = i
        elif h_clean == 'FM' and 'fm' not in col_map:
            col_map['fm'] = i

    if 'nome' not in col_map:
        print("ATTENZIONE: header 'Nome' non trovato nella tabella statistiche", file=sys.stderr)
        return []

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
            'squadra': row_data[col_map['squadra']] if col_map.get('squadra') is not None and col_map['squadra'] < len(row_data) else '',
        }
        for field in ('pv', 'mv', 'fm'):
            idx = col_map.get(field)
            entry[field] = clean_number(row_data[idx]) if idx is not None and idx < len(row_data) else 0

        stats.append(entry)

    return stats


def parse_indisponibili(html):
    """Estrae i nomi degli indisponibili dalla pagina."""
    soup = BeautifulSoup(html, 'html.parser')
    injured = []

    table, headers = extract_from_table(html, ['CALCIATORE', 'MOTIVO', 'INFORTUNIO', 'RIENTRO'])
    if table:
        nome_idx = 0
        for i, h in enumerate(headers):
            if 'CALCIATORE' in h.upper() or 'NOME' in h.upper():
                nome_idx = i
                break
        rows = table.find_all('tr')[1:]
        for row in rows:
            cols = row.find_all(['td', 'th'])
            row_data = [c.get_text(strip=True) for c in cols]
            if nome_idx < len(row_data) and len(row_data[nome_idx]) > 2:
                injured.append(row_data[nome_idx])
        return sorted(set(injured))

    # Fallback: se la struttura cambia, segnala invece di indovinare col regex
    print("ATTENZIONE: tabella indisponibili non trovata, controlla data/debug_indisponibili.html", file=sys.stderr)
    save_debug_html(html, "data/debug_indisponibili.html")
    return []


def save_csv(data, path, headers):
    """Salva i dati in CSV."""
    if not data:
        return False
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(data)
    return True


def save_debug_html(html, path):
    """Salva HTML per debug."""
    with open(path, 'w', encoding='utf-8') as f:
        f.write(html)


def main():
    os.makedirs("data", exist_ok=True)
    ok = True

    # 1. Quotazioni
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
                    print(f" ATTENZIONE: ruolo non riconosciuto per {n_unknown} giocatori "
                          f"(vedi commento in extract_ruolo per sistemare il parsing)", file=sys.stderr)
            else:
                print("ERRORE: impossibile salvare quotazioni.csv", file=sys.stderr)
                ok = False
        else:
            print("ERRORE: nessun giocatore estratto dalle quotazioni", file=sys.stderr)
            print(" HTML salvato in data/debug_quotazioni.html per analisi", file=sys.stderr)
            ok = False
    else:
        ok = False

    # 2. Statistiche
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
            print(" HTML salvato in data/debug_statistiche.html per analisi", file=sys.stderr)
            ok = False
    else:
        ok = False

    # 3. Indisponibili
    print("Scarico indisponibili...")
    html = fetch_page(URLS["indisponibili"])
    if html:
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