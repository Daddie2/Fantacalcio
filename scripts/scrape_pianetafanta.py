import os
import csv
import re
import sys
import json
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

def get_pianetafanta_data(player_name, player_team):
    # Trasforma "Dybala P." in "dybala-p"
    slug = re.sub(r'[^a-z0-9]+', '-', player_name.lower()).strip('-')
    url = f"https://www.pianetafanta.it/giocatori/{slug}"
    
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code != 200:
            return None
            
        # Estrae il JSON nascosto di Next.js
        match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', resp.text, re.DOTALL)
        if not match:
            return None
            
        data = json.loads(match.group(1))
        page_props = data.get('props', {}).get('pageProps', {})
        
        # Se i dati non corrispondono alla squadra (es. omonimi), scarta
        if page_props.get('squadra') and page_props.get('squadra').upper() != player_team.upper():
            return None

        statCasa = page_props.get('statCasa', {})
        statFuori = page_props.get('statFuori', {})
        
        return {
            'nome': player_name,
            'squadra': player_team,
            'gol_casa': statCasa.get('GoalReal', 0),
            'gol_fuori': statFuori.get('GoalReal', 0),
            'assist_casa': statCasa.get('Assist', 0),
            'assist_fuori': statFuori.get('Assist', 0),
            'amm_casa': statCasa.get('Amm', 0),
            'amm_fuori': statFuori.get('Amm', 0),
            'fm_casa': round(statCasa.get('MFG', 0), 2) if statCasa.get('MFG') else None,
            'fm_fuori': round(statFuori.get('MFG', 0), 2) if statFuori.get('MFG') else None,
        }
    except Exception as e:
        return None

def main():
    os.makedirs("data", exist_ok=True)
    quot_path = "data/quotazioni.csv"
    if not os.path.exists(quot_path):
        print(f"ERRORE: {quot_path} non trovato. Esegui prima scrape_fantacalcio.py", file=sys.stderr)
        sys.exit(1)
        
    players = []
    with open(quot_path, encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get('nome') and row.get('squadra'):
                players.append({'nome': row['nome'], 'squadra': row['squadra']})
                
    print(f"Trovati {len(players)} giocatori. Avvio download da PianetaFanta...")
    
    results = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(get_pianetafanta_data, p['nome'], p['squadra']): p for p in players}
        for i, future in enumerate(as_completed(futures)):
            res = future.result()
            if res:
                results.append(res)
            if (i+1) % 50 == 0:
                print(f"Scaricati {i+1}/{len(players)}...")
                
    if results:
        with open("data/stats_pianetafanta.csv", "w", newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=['nome', 'squadra', 'gol_casa', 'gol_fuori', 'assist_casa', 'assist_fuori', 'amm_casa', 'amm_fuori', 'fm_casa', 'fm_fuori'])
            writer.writeheader()
            writer.writerows(results)
        print(f"OK: data/stats_pianetafanta.csv salvato ({len(results)} giocatori).")
    else:
        print("ATTENZIONE: Nessun dato estratto da PianetaFanta.", file=sys.stderr)

if __name__ == "__main__":
    main()