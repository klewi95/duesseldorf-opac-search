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
from adis_search import search_duesseldorf

results = search_duesseldorf("Harry Potter und der Stein der Weisen")

for r in results:
    print(r["titel"])
    print(f"  {r['bibliothek']} – {r['standort']}")
    print(f"  {r['signatur']}  →  {r['status']}")
```

### CLI

```bash
python adis_search.py "Harry Potter und der Stein der Weisen"
```

## Rückgabe

Liste von Dictionaries:

| Schlüssel              | Beschreibung                          |
|------------------------|---------------------------------------|
| `titel`                | Vollständiger Titel                   |
| `status`               | verfügbar / entliehen (+ Fälligkeit)  |
| `bibliothek`           | z. B. Zentralbibliothek, Bücherei …   |
| `standort`             | Regal / Bereich                       |
| `signatur`             | Signatur                              |
| `bestellmoeglichkeit`  | z. B. Standardleihfrist (28 Tage)     |

## Hinweise

- Nutzt Playwright (headless Chromium)
- Funktioniert mit der aktuellen aDIS-Oberfläche der Stadtbüchereien Düsseldorf
- Bei vielen Treffern wird der erste passende Titel geöffnet

## Lizenz

MIT
