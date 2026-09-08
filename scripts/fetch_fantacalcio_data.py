"""
Scarica i dati di quotazioni e statistiche da fantacalcio.it
usando requests-html per eseguire JavaScript e caricare le tabelle.
"""
import os
import sys
import re
import csv
import json
import time
from requests_html import HTMLSession
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
}

# URL delle pagine pubbliche
URLS = {
    "quotazioni": "https://www.fantacalcio.it/quotazioni-fantacalcio",
    "statistiche": "https://www.fantacalcio.it/statistiche-serie-a",
    "indisponibili": "https://www.fantacalcio.it/indisponibili-serie-a",
}


def fetch_with_js(url):
    """Scarica una pagina eseguendo JavaScript."""
    session = HTMLSession()
    try:
        response = session.get(url, headers=HEADERS, timeout=30)
        # Aspetta che il JavaScript carichi le tabelle
        response.html.render(timeout=20, sleep=2)
        return response.html.html
    except Exception as e:
        print(f"ERRORE con requests-html per {url}: {e}", file=sys.stderr)
        # Fallback: prova senza JS
        try:
            response = session.get(url, headers=HEADERS, timeout=30)
            return response.text
        except Exception as e2:
            print(f"ERRORE anche senza JS: {e2}", file=sys.stderr)
            return None
    finally:
        session.close()


def parse_quotazioni(html):
    """Estrae la tabella delle quotazioni dalla pagina."""
    soup = BeautifulSoup(html, 'html.parser')
    players = []
    
    # Cerca la tabella delle quotazioni - vari selettori possibili
    table = None
    
    # Prova diversi selettori
    selectors = [
        'table.table-condensed',
        'table.table-striped',
        'table.table-bordered',
        'table.quotazioni-table',
        'table[class*="quota"]',
        'table[class*="price"]',
        'table[class*="table"]',
        'div.table-responsive table',
        '.listone table',
        '#listone table'
    ]
    
    for selector in selectors:
        try:
            found = soup.select(selector)
            if found:
                table = found[0]
                break
        except:
            continue
    
    # Se ancora non troviamo, cerca qualsiasi tabella con dati
    if not table:
        tables = soup.find_all('table')
        for t in tables:
            # Controlla se ha intestazioni di quotazioni
            header_text = ' '.join([th.get_text(strip=True) for th in t.find_all('th')])
            if any(k in header_text.upper() for k in ['NOME', 'CALCIATORE', 'GIOCATORE', 'QTA', 'FVM', 'QUOTAZ']):
                table = t
                break
    
    if not table:
        # Salva l'HTML per debug
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
    
    # Se non troviamo header, usiamo quelli standard
    if not headers or len(headers) < 3:
        headers = ['Ruolo', 'Nome', 'Squadra', 'Qt.A', 'FVM']
    
    # Mappa colonne per nome
    col_map = {}
    for i, h in enumerate(headers):
        h_clean = h.upper().replace(' ', '').replace('.', '')
        if 'RUOLO' in h_clean or 'R' == h_clean:
            col_map['ruolo'] = i
        elif 'NOME' in h_clean or 'CALCIATORE' in h_clean or 'GIOCATORE' in h_clean:
            col_map['nome'] = i
        elif 'SQUADRA' in h_clean or 'SQ' in h_clean:
            col_map['squadra'] = i
        elif 'QTA' in h_clean or 'QUOTAZIONE' in h_clean:
            col_map['qta'] = i
        elif 'FVM' in h_clean or 'VALORE' in h_clean:
            col_map['fvm'] = i
    
    # Se mancano colonne essenziali, usa indici standard
    if 'nome' not in col_map:
        for i, h in enumerate(headers):
            if any(k in h.upper() for k in ['NOME', 'CALCIATORE', 'GIOCATORE']):
                col_map['nome'] = i
            elif any(k in h.upper() for k in ['SQUADRA', 'SQ', 'SOCIETA']):
                col_map['squadra'] = i
            elif any(k in h.upper() for k in ['RUOLO', 'R', 'POS']):
                col_map['ruolo'] = i
    
    # Fallback estremo
    if 'nome' not in col_map:
        col_map = {'ruolo': 0, 'nome': 1, 'squadra': 2, 'qta': 3, 'fvm': 4}
    
    # Estrai righe
    rows = table.find_all('tr')[1:]
    for row in rows:
        cols = row.find_all(['td', 'th'])
        if not cols:
            continue
        
        row_data = [c.get_text(strip=True) for c in cols]
        
        # Salta righe vuote o di intestazione
        if not row_data or len(row_data) < 3:
            continue
        
        nome = row_data[col_map['nome']] if col_map['nome'] < len(row_data) else ''
        if not nome or len(nome) < 2:
            continue
        
        ruolo_raw = row_data[col_map['ruolo']] if col_map['ruolo'] < len(row_data) else 'C'
        ruolo = ruolo_raw.upper()[0] if ruolo_raw and ruolo_raw.upper()[0] in 'PDCA' else 'C'
        
        squadra = row_data[col_map['squadra']] if col_map['squadra'] < len(row_data) else ''
        
        def parse_num(val):
            if not val:
                return 0
            clean = re.sub(r'[^\d,.]', '', str(val))
            clean = clean.replace(',', '.')
            try:
                return float(clean)
            except:
                return 0
        
        qta = parse_num(row_data[col_map['qta']]) if col_map.get('qta') is not None and col_map['qta'] < len(row_data) else 0
        fvm = parse_num(row_data[col_map['fvm']]) if col_map.get('fvm') is not None and col_map['fvm'] < len(row_data) else 0
        
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
    stats = []
    
    # Cerca la tabella delle statistiche
    table = None
    
    # Prova diversi selettori
    selectors = [
        'table.table-striped',
        'table.table-bordered',
        'table[class*="stat"]',
        'div.table-responsive table',
        '.statistiche-table',
        '#statistiche table'
    ]
    
    for selector in selectors:
        try:
            found = soup.select(selector)
            if found:
                table = found[0]
                break
        except:
            continue
    
    if not table:
        tables = soup.find_all('table')
        for t in tables:
            header_text = ' '.join([th.get_text(strip=True) for th in t.find_all('th')])
            if any(k in header_text.upper() for k in ['PRESENZE', 'PV', 'MEDIA', 'FANTAMEDIA']):
                table = t
                break
    
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
        headers = ['Nome', 'Squadra', 'Pv', 'Mv', 'Fm', 'Gf', 'Gs', 'Ass', 'Amm', 'Esp']
    
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
        elif 'GF' in h_clean or 'GOLFATTI' in h_clean or 'GOL' in h_clean:
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
    
    def parse_num(val):
        if not val:
            return 0
        clean = re.sub(r'[^\d,.]', '', str(val))
        clean = clean.replace(',', '.')
        try:
            return float(clean)
        except:
            return 0
    
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
                entry[field] = parse_num(row_data[col_map[field]])
        
        stats.append(entry)
    
    return stats


def parse_indisponibili(html):
    """Estrae i nomi degli indisponibili dalla pagina."""
    soup = BeautifulSoup(html, 'html.parser')
    injured = []
    
    text = soup.get_text()
    
    # Cerca sezioni specifiche
    for section in soup.find_all(['div', 'section', 'article', 'table']):
        section_text = section.get_text()
        if any(k in section_text.lower() for k in ['infortun', 'stop', 'out', 'indispon', 'squalif']):
            names = re.findall(r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)', section_text)
            injured.extend(names)
    
    all_names = re.findall(r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)', text)
    common_words = {'Dopo', 'Sarà', 'Senza', 'Dalla', 'Delle', 'Degli', 'Altre', 'Prima', 'Oggi', 'Martedi', 'Giovedi', 'Venerdi', 'Sabato', 'Domenica', 'Lunedì', 'Martedì'}
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
    html = fetch_with_js(URLS["quotazioni"])
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
    html = fetch_with_js(URLS["statistiche"])
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
    html = fetch_with_js(URLS["indisponibili"])
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