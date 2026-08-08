# Düsseldorf OPAC Search

Robuste Python-Suche im Online-Katalog der [Stadtbüchereien Düsseldorf](https://opac-duesseldorf.itk-rheinland.de/) (aDIS)  
incl. **Smart Watchlist** mit Change-Detection und optionalem **Telegram-Alert**.

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

Titel merken und nur bei echten Verfügbarkeits-Änderungen benachrichtigt werden.

```bash
# Titel beobachten
python adis_search.py --watch "Harry Potter und der Stein der Weisen"

# Alle beobachteten Titel prüfen
python adis_search.py --check-watchlist

# Watchlist anzeigen / Titel entfernen
python adis_search.py --list-watch
python adis_search.py --unwatch "Harry Potter"
```

### Telegram-Alerts einrichten

1. Bot bei [@BotFather](https://t.me/BotFather) erstellen → Token kopieren  
2. Dem Bot eine Nachricht schicken, dann Chat-ID herausfinden (z. B. über `@userinfobot` oder die getUpdates-API)  
3. Umgebungsvariablen setzen:

```bash
export TELEGRAM_BOT_TOKEN="123456:ABC-DEF..."
export TELEGRAM_CHAT_ID="987654321"
```

Beim nächsten `--check-watchlist` werden Änderungen direkt an Telegram geschickt.

Tipp für regelmäßige Prüfung (cron / launchd):

```bash
*/30 * * * * cd /path/to/duesseldorf-opac-search && . venv/bin/activate && python adis_search.py --check-watchlist
```

## Python-API

```python
from adis_search import search_duesseldorf, summarize

results = search_duesseldorf("Sapiens")
print(summarize(results))
# → "✅ 1 verfügbar · 🔴 2 entliehen"
```

## CLI-Optionen

| Flag | Beschreibung |
|------|--------------|
| `--json` | Ausgabe als JSON (inkl. `summary`) |
| `--max N` | Nur die ersten N Exemplare |
| `--watch TITEL` | Zur Watchlist hinzufügen |
| `--check-watchlist` | Alle Titel prüfen + ggf. Telegram |
| `--list-watch` | Watchlist anzeigen |
| `--unwatch TITEL` | Aus Watchlist entfernen |
| `--no-notify` | Beim Check keine Telegram-Nachricht |
| `-v` | Verbose-Logs |

## Lizenz

MIT
