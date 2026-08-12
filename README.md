# Düsseldorf OPAC Search

Robuste Python-Suche im Online-Katalog der [Stadtbüchereien Düsseldorf](https://opac-duesseldorf.itk-rheinland.de/) (aDIS)  
incl. **Smart Watchlist**, **Telegram-Alerts** und **konfigurierbarem Cron-Scheduler**.

Die Suche ist bewusst auf **gedruckte Bücher und E-Books** beschränkt.
Hörbücher, Hörspiele, CDs, DVDs, Blu-rays, Filme und sonstige Medien werden
bereits bei der Datensatz-Verifikation ausgeschlossen und fließen weder in
Verfügbarkeitszahlen noch in Abholpläne ein.

## Installation

```bash
pip install playwright
playwright install chromium
```

## Schnellstart

```bash
python adis_search.py "Harry Potter und der Stein der Weisen"
```

## „Kann ich das heute abholen?“ – Abholplan (MVP)

Der Abholplan verwandelt die einzelnen OPAC-Exemplare in eine handlungsfähige
Empfehlung: Er berücksichtigt **Verfügbarkeit**, die regulären
**Öffnungszeiten** der Filiale und deine explizit angegebene Filial-Reihenfolge.

```bash
# Beste aktuell erreichbare Kopie finden
python adis_search.py --plan-pickup "Sapiens"

# Eigene Reihenfolge vorgeben (kann mehrfach angegeben werden)
python adis_search.py --plan-pickup \
  --prefer-branch "Bücherei Bilk" \
  --prefer-branch "Zentralbibliothek" \
  "Sapiens"

# Für einen bestimmten Zeitpunkt testen oder planen
python adis_search.py --plan-pickup --at "2026-08-10T16:30" "Sapiens"

# Maschinenlesbare Antwort inklusive Entscheidung und Alternativen
python adis_search.py --plan-pickup --json "Sapiens"
```

Die Suche akzeptiert keinen nur thematisch verwandten Treffer als Treffer für
den gewünschten Titel. Wenn der Haupttitel nach dem Öffnen des OPAC-Eintrags
nicht verifiziert werden kann, liefert die CLI ausdrücklich
`Titel nicht verifiziert` (im JSON-Feld `search_note`) und keine erfundenen
Exemplare.

Verschiedene Ausgaben desselben Werks liegen im OPAC oft als getrennte
Datensätze vor. Die Suche prüft deshalb alle titel- und autorverifizierten
Datensätze und führt ihre Exemplare zusammen, bevor sie die Verfügbarkeit
bewertet. Lektürehilfen wie `Fragen zu Corpus Delicti` werden nicht allein wegen
enthaltener Titelwörter als der gesuchte Roman akzeptiert.

### Deutsche Übersetzung als sichere Alternative

Die Originalausgabe bleibt die erste Wahl. Findet die OPAC-Suche stattdessen
eine deutsche Ausgabe mit einem anderen Titel, wird sie nur dann akzeptiert,
wenn die strukturierten Katalogdaten alle drei Aussagen bestätigen:

1. `Bevorzugter Titel` oder `Originaltitel` entspricht dem gesuchten Werk.
2. `Verfasser` entspricht dem angegebenen Autor (Namensreihenfolge und
   Satzzeichen spielen keine Rolle).
3. `Sprache` ist Deutsch.

So wird zum Beispiel die Suche

```bash
python3 adis_search.py --json "Beyond Redemption - Michael R. Fletcher"
```

als deutsche Übersetzung kenntlich gemacht und kann `Chroniken des Wahns -
Blutwerk` liefern. Das JSON enthält dafür zusätzlich `trefferart`,
`originaltitel`, `sprache` und `originalsprache`; `search_note` erklärt die
Substitution auch für Agenten, die nur die Zusammenfassung auswerten.

Beispielausgabe:

```text
🧭 Abholplan: Sapiens

📗 Heute abholen: Zentralbibliothek
   jetzt geöffnet bis 21:00 Uhr (regulär geöffnet)
   Gesellschaft · Gcl 1 Huber
   Konrad-Adenauer-Platz 1, 40210 Düsseldorf
```

Der Plan behauptet **nicht**, dass eine nur ausgeliehene Kopie verfügbar wird:
Fälligkeitstermine erscheinen ausdrücklich als unverbindliche Rückgabe-Hinweise.
Bei Selbstbedienungszeiten wird das ebenfalls sichtbar gemacht.

### Grenzen des MVP

- „Nähe“ ist zunächst deine transparente `--prefer-branch`-Reihenfolge, keine
  erfundene Routenzeit. Ein späterer Routing-Adapter kann echte ÖPNV-/Fußwege
  ergänzen, ohne den Planer umzubauen.
- `branches.json` enthält reguläre Öffnungszeiten, zuletzt geprüft am
  **2026-08-09**, nach den offiziellen Seiten der
  [Stadtbüchereien Düsseldorf](https://www.duesseldorf.de/stadtbuechereien/wer-wir-sind/kontakt).
  Feiertage und kurzfristige Schließungen sind bewusst als Unsicherheit markiert.
- Das Feature plant nur den Besuch; es reserviert oder verlängert nichts.

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
| `--plan-pickup` | Beste Abholung anhand von Verfügbarkeit und Öffnungszeiten planen |
| `--prefer-branch NAME` | Filiale für den Abholplan bevorzugen; mehrfach für Reihenfolge |
| `--at ISO-DATETIME` | Planungszeitpunkt für den Abholplan setzen |
| `--pickup-buffer-minutes N` | Mindestzeit bis zur Schließung für eine sichere Abholung (default 20) |
| `-v` | Verbose-Logs |

## Lizenz

MIT
