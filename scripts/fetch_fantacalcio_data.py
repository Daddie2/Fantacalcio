"""
Scarica i dati di quotazioni e statistiche da fantacalcio.it
direttamente dalle tabelle HTML pubbliche usando solo requests + BeautifulSoup.
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


def fetch_page(url):
    """Scarica una pagina HTML."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as e:
        print(f"ERRORE scaricando {url}: {e}", file=sys.stderr)
        return None


def find_table(soup, keywords):
    """Cerca una tabella che contiene tutte le parole chiave."""
    tables = soup.find_all('table')
    for table in tables:
        text = table.get_text()
        if all(k.lower() in text.lower() for k in keywords):
            return table
    return None


def clean_number(val):
    """Pulisce un valore numerico da stringa."""
    if not val:
        return 0
    clean = re.sub(r'[^\d,.-]', '', str(val))
    clean = clean.replace(',', '.')
    try:
        return float(clean)
    except:
        return 0


def parse_quotazioni(html):
    """Estrae la tabella delle quotazioni dalla pagina."""
    soup = BeautifulSoup(html, 'html.parser')
    
    # Cerca la tabella delle quotazioni
    table = None
    
    # Metodo 1: Cerca per classi
    table_classes = ['table', 'table-striped', 'table-bordered', 'table-condensed', 'table-hover', 'table-responsive']
    for cls in table_classes:
        tables = soup.find_all('table', class_=re.compile(cls, re.I))
        for t in tables:
            text = t.get_text()
            if 'NOME' in text.upper() or 'CALCIATORE' in text.upper():
                table = t
                break
        if table:
            break
    
    # Metodo 2: Cerca per contenuto
    if not table:
        table = find_table(soup, ['NOME', 'SQUADRA'])
    
    # Metodo 3: Cerca qualsiasi tabella con molte righe
    if not table:
        for t in soup.find_all('table'):
            rows = t.find_all('tr')
            if len(rows) > 10:
                first_row = rows[1] if len(rows) > 1 else None
                if first_row and len(first_row.find_all(['td', 'th'])) >= 4:
                    table = t
                    break
    
    if not table:
        # Salva per debug
        with open('data/debug_quotazioni.html', 'w', encoding='utf-8') as f:
            f.write(html)
        print("ERRORE: Tabella quotazioni non trovata. HTML salvato in data/debug_quotazioni.html", file=sys.stderr)
        return None
    
    # Estrai header
    headers = []
    header_row = table.find('tr')
    if header_row:
        for th in header_row.find_all(['th', 'td']):
            headers.append(th.get_text(strip=True))
    
    if not headers:
        headers = ['Ruolo', 'Nome', 'Squadra', 'Qt.A', 'FVM']
    
    # Mappa colonne
    col_map = {}
    for i, h in enumerate(headers):
        h_clean = h.upper().replace(' ', '').replace('.', '').replace('\n', '')
        if h_clean in ['R', 'RUOLO']:
            col_map['ruolo'] = i
        elif h_clean in ['NOME', 'CALCIATORE', 'GIOCATORE']:
            col_map['nome'] = i
        elif h_clean in ['SQUADRA', 'SQ', 'SOCIETA']:
            col_map['squadra'] = i
        elif 'QTA' in h_clean:
            col_map['qta'] = i
        elif 'FVM' in h_clean:
            col_map['fvm'] = i
    
    # Fallback: struttura standard
    if 'nome' not in col_map:
        col_map = {'ruolo': 0, 'nome': 1, 'squadra': 2, 'qta': 3, 'fvm': 4}
    
    players = []
    rows = table.find_all('tr')[1:]
    
    for row in rows:
        cols = row.find_all(['td', 'th'])
        if not cols:
            continue
        
        row_data = [c.get_text(strip=True) for c in cols]
        if len(row_data) < 3:
            continue
        
        nome_idx = col_map.get('nome', 1)
        nome = row_data[nome_idx] if nome_idx < len(row_data) else ''
        if not nome or len(nome) < 2:
            continue
        
        ruolo_idx = col_map.get('ruolo', 0)
        ruolo_raw = row_data[ruolo_idx] if ruolo_idx < len(row_data) else 'C'
        ruolo = ruolo_raw.upper()[0] if ruolo_raw and ruolo_raw.upper()[0] in 'PDCA' else 'C'
        
        squadra_idx = col_map.get('squadra', 2)
        squadra = row_data[squadra_idx] if squadra_idx < len(row_data) else ''
        
        qta_idx = col_map.get('qta', 3)
        qta = clean_number(row_data[qta_idx]) if qta_idx < len(row_data) else 0
        
        fvm_idx = col_map.get('fvm', 4)
        fvm = clean_number(row_data[fvm_idx]) if fvm_idx < len(row_data) else 0
        
        player_id = f"p_{nome.lower().replace(' ', '_')}_{squadra.lower().replace(' ', '_')}"
        
        players.append({
            'id': player_id,
            'ruolo': ruolo,
            'nome': nome,
            'squadra': squadra,
            'qta': qta,
            'fvm': fvm
        })
    
    return players


def parse_statistiche(html):
    """Estrae le statistiche dalla pagina."""
    soup = BeautifulSoup(html, 'html.parser')
    
    # Cerca la tabella delle statistiche
    table = None
    
    # Metodo 1: Cerca per classi
    for cls in ['table', 'table-striped', 'table-bordered', 'table-sm']:
        tables = soup.find_all('table', class_=re.compile(cls, re.I))
        for t in tables:
            text = t.get_text()
            if any(k in text.upper() for k in ['PRESENZE', 'PV', 'MEDIA VOTO', 'FANTAMEDIA']):
                table = t
                break
        if table:
            break
    
    # Metodo 2: Cerca per contenuto
    if not table:
        table = find_table(soup, ['PRESENZE', 'FANTAMEDIA'])
    
    if not table:
        with open('data/debug_statistiche.html', 'w', encoding='utf-8') as f:
            f.write(html)
        print("ERRORE: Tabella statistiche non trovata. HTML salvato in data/debug_statistiche.html", file=sys.stderr)
        return None
    
    # Estrai header
    headers = []
    header_row = table.find('tr')
    if header_row:
        for th in header_row.find_all(['th', 'td']):
            headers.append(th.get_text(strip=True))
    
    if not headers:
        headers = ['Nome', 'Squadra', 'Pv', 'Mv', 'Fm']
    
    # Mappa colonne
    col_map = {}
    for i, h in enumerate(headers):
        h_clean = h.upper().replace(' ', '').replace('.', '')
        if 'NOME' in h_clean or 'CALCIATORE' in h_clean:
            col_map['nome'] = i
        elif 'SQUADRA' in h_clean or 'SQ' in h_clean:
            col_map['squadra'] = i
        elif 'PV' in h_clean or 'PRESENZE' in h_clean:
            col_map['pv'] = i
        elif 'MV' in h_clean or 'MEDIAVOTO' in h_clean:
            col_map['mv'] = i
        elif 'FM' in h_clean or 'FANTAMEDIA' in h_clean:
            col_map['fm'] = i
        elif 'GF' in h_clean or 'GOLFATTI' in h_clean:
            col_map['gf'] = i
        elif 'GS' in h_clean or 'GOLSUBITI' in h_clean:
            col_map['gs'] = i
        elif 'ASS' in h_clean or 'ASSIST' in h_clean:
            col_map['ass'] = i
        elif 'AMM' in h_clean or 'AMMONIZIONI' in h_clean:
            col_map['amm'] = i
        elif 'ESP' in h_clean or 'ESPULSIONI' in h_clean:
            col_map['esp'] = i
    
    if 'nome' not in col_map:
        col_map = {'nome': 0, 'squadra': 1, 'pv': 2, 'mv': 3, 'fm': 4}
    
    stats = []
    rows = table.find_all('tr')[1:]
    
    for row in rows:
        cols = row.find_all(['td', 'th'])
        if not cols:
            continue
        row_data = [c.get_text(strip=True) for c in cols]
        if len(row_data) < 3:
            continue
        
        nome = row_data[col_map['nome']] if col_map['nome'] < len(row_data) else ''
        if not nome or len(nome) < 2:
            continue
        
        entry = {
            'nome': nome,
            'squadra': row_data[col_map['squadra']] if col_map.get('squadra') is not None and col_map['squadra'] < len(row_data) else '',
        }
        
        for field in ['pv', 'mv', 'fm', 'gf', 'gs', 'ass', 'amm', 'esp']:
            if col_map.get(field) is not None and col_map[field] < len(row_data):
                entry[field] = clean_number(row_data[col_map[field]])
        
        stats.append(entry)
    
    return stats


def parse_indisponibili(html):
    """Estrae i nomi degli indisponibili dalla pagina."""
    soup = BeautifulSoup(html, 'html.parser')
    injured = []
    
    # Cerca pattern nel testo
    text = soup.get_text()
    
    # Pattern per nomi associati a infortuni
    patterns = [
        r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+[–\-]\s+(?:infortunato|stop|out)',
        r'(?:infortunato|stop|out)\s+[–\-]\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)',
        r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+[–\-]\s+[0-9]+/\s+[0-9]+',
        r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+(?:infortunato|si è fermato)',
    ]
    
    for pattern in patterns:
        matches = re.findall(pattern, text)
        injured.extend(matches)
    
    # Cerca anche in elementi specifici
    for cls in ['player-name', 'playerName', 'name', 'nome']:
        elements = soup.find_all(class_=re.compile(cls, re.I))
        for el in elements:
            text = el.get_text(strip=True)
            parent_text = el.parent.get_text() if el.parent else ''
            if any(k in parent_text.lower() for k in ['infortun', 'stop', 'out', 'indispon']):
                if len(text) > 2:
                    injured.append(text)
    
    # Rimuovi duplicati e parole comuni
    common_words = {'Dopo', 'Sarà', 'Senza', 'Dalla', 'Delle', 'Degli', 'Altre', 'Prima', 'Oggi',
                   'Martedi', 'Giovedi', 'Venerdi', 'Sabato', 'Domenica', 'Lunedì', 'Martedì',
                   'Mercoledì', 'Giovedì', 'Venerdì', 'Recupero', 'Tempo', 'Data', 'Nessuno'}
    
    injured = [n for n in set(injured) if n not in common_words and len(n) > 3]
    
    return injured


def save_csv(data, path, headers):
    """Salva i dati in CSV."""
    if not data:
        return False
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(data)
    return True


def main():
    os.makedirs("data", exist_ok=True)
    ok = True
    
    # 1. Quotazioni
    print("Scarico quotazioni...")
    html = fetch_page(URLS["quotazioni"])
    if html:
        players = parse_quotazioni(html)
        if players:
            headers = ['id', 'ruolo', 'nome', 'squadra', 'qta', 'fvm']
            if save_csv(players, "data/quotazioni.csv", headers):
                print(f"OK: data/quotazioni.csv ({len(players)} giocatori)")
            else:
                print("ERRORE: impossibile salvare quotazioni.csv", file=sys.stderr)
                ok = False
        else:
            print("ERRORE: nessun giocatore estratto dalle quotazioni", file=sys.stderr)
            ok = False
    else:
        ok = False
    
    # 2. Statistiche
    print("Scarico statistiche...")
    html = fetch_page(URLS["statistiche"])
    if html:
        stats = parse_statistiche(html)
        if stats:
            headers = ['nome', 'squadra', 'pv', 'mv', 'fm', 'gf', 'gs', 'ass', 'amm', 'esp']
            if save_csv(stats, "data/stats_curr.csv", headers):
                print(f"OK: data/stats_curr.csv ({len(stats)} giocatori)")
            else:
                print("ERRORE: impossibile salvare stats_curr.csv", file=sys.stderr)
                ok = False
        else:
            print("ERRORE: nessuna statistica estratta", file=sys.stderr)
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
                for name in sorted(injured):
                    f.write(f"{name}\n")
            print(f"OK: data/indisponibili.txt ({len(injured)} nomi)")
    
    if not ok:
        print("ERRORE: alcuni file non sono stati scaricati correttamente", file=sys.stderr)
        sys.exit(1)
    else:
        print("Tutti i dati scaricati con successo.")


if __name__ == "__main__":
    main()