# Düsseldorf OPAC Search

Robuste Python-Suche im Online-Katalog der [Stadtbüchereien Düsseldorf](https://opac-duesseldorf.itk-rheinland.de/) (aDIS)  
incl. **Smart Watchlist**, **Telegram-Alerts** und **konfigurierbarem Cron-Scheduler**.

## Installation

```bash
pip install playwright
playwright install chromium
```

## Schnellstart

```bash
python adis_search.py "Harry Potter und der Stein der Weisen"
```

## Smart Watchlist

```bash
# Titel beobachten
python adis_search.py --watch "Harry Potter und der Stein der Weisen"

# Alle beobachteten Titel prüfen
python adis_search.py --check-watchlist

# Watchlist anzeigen / Titel entfernen
python adis_search.py --list-watch
python adis_search.py --unwatch "Harry Potter"
```

### Telegram-Alerts

```bash
export TELEGRAM_BOT_TOKEN="123456:ABC-DEF..."
export TELEGRAM_CHAT_ID="987654321"
```

## Automatische Checks (Cron-Scheduler)

Du kannst den Prüf-Intervall selbst festlegen:

```bash
# Alle 3 Stunden zwischen 08:00 und 20:00
python adis_search.py --install-cron --from 8 --to 20 --every 3

# Andere Beispiele
python adis_search.py --install-cron --from 9 --to 18 --every 2   # alle 2h, 9–18 Uhr
python adis_search.py --install-cron --from 10 --to 22 --every 4  # alle 4h, 10–22 Uhr

# Aktuellen Schedule anzeigen
python adis_search.py --show-cron

# Wieder entfernen
python adis_search.py --uninstall-cron
```

Das erzeugt:
- ein Wrapper-Skript `check_watchlist.sh`
- einen Crontab-Eintrag (nur in dem gewählten Zeitfenster)
- eine kleine Config `scheduler.json`

Log-Ausgabe: `/tmp/opac_watchlist.log`

> **Hinweis:** Die Uhrzeiten beziehen sich auf die Systemzeitzone deines Macs  
> (sollte `Europe/Berlin` sein). Prüfen mit `date`.

## Python-API

```python
from adis_search import search_duesseldorf, summarize

results = search_duesseldorf("Sapiens")
print(summarize(results))
# → "✅ 1 verfügbar · 🔴 2 entliehen"
```

## CLI-Übersicht

| Flag | Beschreibung |
|------|--------------|
| `--json` | Ausgabe als JSON |
| `--max N` | Nur die ersten N Exemplare |
| `--watch TITEL` | Zur Watchlist hinzufügen |
| `--check-watchlist` | Alle Titel prüfen + ggf. Telegram |
| `--list-watch` | Watchlist anzeigen |
| `--unwatch TITEL` | Aus Watchlist entfernen |
| `--install-cron` | Cron-Job installieren |
| `--from HOUR` | Startstunde (default 8) |
| `--to HOUR` | Endstunde (default 20) |
| `--every HOURS` | Intervall in Stunden (default 3) |
| `--show-cron` | Aktuellen Schedule zeigen |
| `--uninstall-cron` | Cron-Job entfernen |
| `-v` | Verbose-Logs |

## Lizenz

MIT
