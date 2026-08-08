# Düsseldorf OPAC Search

Robuste Python-Suche im Online-Katalog der [Stadtbüchereien Düsseldorf](https://opac-duesseldorf.itk-rheinland.de/) (aDIS).

Gibt für einen Titel alle Exemplare mit **Status**, **Bibliothek**, **Standort** und **Signatur** zurück.

## Installation

```bash
pip install playwright
playwright install chromium
```

## Verwendung

```python
from adis_search import search_duesseldorf, summarize

results = search_duesseldorf("Harry Potter und der Stein der Weisen")

print(summarize(results))
# → "✅ 2 verfügbar · 🔴 3 entliehen"

for r in results:
    print(f"  {r['bibliothek']} – {r['standort']} → {r['status']}")
```

### CLI

```bash
# Menschlich lesbar (mit Zusammenfassung)
python adis_search.py "Harry Potter und der Stein der Weisen"

# Valides JSON (inkl. summary)
python adis_search.py "Harry Potter" --json

# Nur die ersten 3 Exemplare
python adis_search.py "Sapiens" --json --max 3
```

## Rückgabe

### `search_duesseldorf()`
Liste von Dictionaries (nach Validierung):

| Schlüssel              | Beschreibung                          |
|------------------------|---------------------------------------|
| `titel`                | Vollständiger Titel                   |
| `status`               | verfügbar / entliehen (+ Fälligkeit)  |
| `bibliothek`           | z. B. Zentralbibliothek, Bücherei …   |
| `standort`             | Regal / Bereich                       |
| `signatur`             | Signatur                              |
| `bestellmoeglichkeit`  | z. B. Standardleihfrist (28 Tage)     |

### `summarize(results)`
Kurzer Übersichtstext, z. B.:
```
✅ 2 verfügbar · 🔴 3 entliehen · ⏳ 1 vorbestellt
```

## Hinweise

- Nutzt Playwright (headless Chromium)
- Funktioniert mit der aktuellen aDIS-Oberfläche der Stadtbüchereien Düsseldorf
- Bei vielen Treffern wird der erste passende Titel geöffnet

## Lizenz

MIT
