""" Scarica i file Excel ufficiali di fantacalcio.it (quotazioni e statistiche stagione corrente) e li salva in data/, cosi' l'app puo' leggerli da sola. Pensato per girare dentro un workflow di GitHub Actions programmato (vedi .github/workflows/update-data.yml), ma funziona anche lanciato a mano: pip install requests python scripts/fetch_fantacalcio_data.py """
import os
import sys
import requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://www.fantacalcio.it/quotazioni-fantacalcio",
    "Accept": "*/*",
}

# Endpoint diretti usati dal bottone "Scarica" delle pagine pubbliche del sito.
# Il primo numero e' l'id della stagione corrente, il secondo il tipo di
# visualizzazione (1 = Classic). Se in futuro cambia stagione e questi
# smettono di funzionare, vanno ripresi dal codice sorgente delle pagine:
# https://www.fantacalcio.it/quotazioni-fantacalcio (bottone "Scarica")
# https://www.fantacalcio.it/statistiche-serie-a (bottone "Scarica")
FILES = {
    "data/quotazioni.xlsx": "https://www.fantacalcio.it/api/v1/Excel/prices/21/1",
    "data/stats_curr.xlsx": "https://www.fantacalcio.it/api/v1/Excel/stats/21/1",
}


def main():
    os.makedirs("data", exist_ok=True)
    ok = True
    for path, url in FILES.items():
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"ERRORE scaricando {url}: {e}", file=sys.stderr)
            ok = False
            continue

        content = resp.content
        content_type = resp.headers.get("Content-Type", "")
        # controllo minimo di sanita': un vero .xlsx e' un file binario
        # (zip) di dimensione non banale, non una pagina di errore html.
        looks_like_xlsx = content[:2] == b"PK" and len(content) > 2000
        if not looks_like_xlsx:
            print(
                f"ATTENZIONE: risposta sospetta da {url} "
                f"(content-type={content_type}, {len(content)} byte). "
                "Il sito potrebbe aver cambiato l'endpoint o richiesto un login.",
                file=sys.stderr,
            )
            ok = False
            continue

        with open(path, "wb") as f:
            f.write(content)
        print(f"OK: {path} aggiornato ({len(content)} byte)")

    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()