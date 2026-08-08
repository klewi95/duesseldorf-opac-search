"""
Robuste OPAC-Suche für Stadtbücherei Düsseldorf (aDIS / ITK Rheinland).

Verwendung:
    from adis_search import search_duesseldorf
    results = search_duesseldorf("Harry Potter und der Stein der Weisen")

Jedes Element der Rückgabeliste ist ein Dict mit:
    titel, status, bibliothek, standort, signatur, bestellmoeglichkeit
"""

from __future__ import annotations

import re
import logging
from typing import List, Dict, Any

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)

# Freundliche Start-URL (redirectet intern auf den korrekten DirectLink mit SOPAC07)
OPAC_START = "https://opac-duesseldorf.itk-rheinland.de/"


def _normalize_status(raw: str) -> str:
    """Vereinheitlicht Status-Strings aus dem OPAC."""
    t = (raw or "").strip().lower()
    if not t:
        return "unbekannt"
    if "verfügbar" in t and "ausgeliehen" not in t:
        return "verfügbar"
    if "ausgeliehen" in t or "entliehen" in t:
        # behalte ggf. Fälligkeitsdatum
        return raw.strip() if "fällig" in t else "entliehen"
    if "vorbestellt" in t or "reserviert" in t:
        return "vorbestellt"
    if "nicht ausleihbar" in t or "präsenz" in t:
        return "nicht ausleihbar"
    return raw.strip() or "unbekannt"


def search_duesseldorf(titel: str, max_results: int = 5) -> List[Dict[str, Any]]:
    """
    Sucht im Düsseldorfer aDIS-OPAC nach einem Titel und gibt eine Liste
    mit allen gefundenen Exemplaren (Verfügbarkeit, Bibliothek, Standort, Signatur) zurück.

    Bei mehreren Treffern wird der erste passende Titel (der den Suchbegriff enthält)
    geöffnet. Es werden die Holdings der Detailseite ausgewertet.
    """
    if not titel or not titel.strip():
        return []

    ergebnisse: List[Dict[str, Any]] = []
    titel = titel.strip()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_default_timeout(25000)

            # ---- Startseite laden ----
            page.goto(OPAC_START, wait_until="domcontentloaded")
            page.wait_for_selector("#Autosuggest", state="visible", timeout=15000)

            # ---- Suche ausführen ----
            search_input = page.locator("#Autosuggest")
            search_input.fill(titel)
            page.locator("input.suche-starten").click()

            # Auf Trefferliste oder Direkt-Detailseite warten
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1500)

            # ---- Sind wir schon auf einer Detailseite? ----
            holdings_table = page.locator("table#resptable-1, table.rTable_table")
            on_detail = holdings_table.count() > 0 and holdings_table.first.locator("tr").count() > 1

            if not on_detail:
                # Trefferliste: ersten passenden Titel-Link anklicken
                title_links = page.locator(".rList_col.rList_titel a")
                clicked = False
                search_words = [w.lower() for w in titel.split() if len(w) > 2][:3]

                for i in range(min(title_links.count(), 25)):
                    link = title_links.nth(i)
                    link_text = link.inner_text().strip()
                    link_lower = link_text.lower()
                    # Bevorzuge Links, die den Suchbegriff (oder wesentliche Teile) enthalten
                    if any(w in link_lower for w in search_words) or titel.lower() in link_lower:
                        logger.debug("Klicke Treffer: %s", link_text[:80])
                        link.click()
                        page.wait_for_load_state("networkidle")
                        page.wait_for_timeout(2000)
                        clicked = True
                        break

                if not clicked and title_links.count() > 0:
                    # Fallback: ersten Treffer nehmen
                    title_links.first.click()
                    page.wait_for_load_state("networkidle")
                    page.wait_for_timeout(2000)

            # ---- Titel der Detailseite ----
            titel_voll = titel
            # aDIS zeigt den Titel oft in einem h2 mit Präfix "Aktuelle Seite:"
            for sel in ["h2", "h1", ".detail-title", ".rTitle"]:
                els = page.locator(sel)
                for i in range(min(els.count(), 6)):
                    raw = els.nth(i).inner_text().strip()
                    if not raw:
                        continue
                    # Präfix entfernen
                    cleaned = re.sub(r"^Aktuelle Seite:\s*", "", raw, flags=re.I).strip()
                    if cleaned and "sitzungsende" not in cleaned.lower() and "anleitung" not in cleaned.lower():
                        if len(cleaned) > 10:
                            titel_voll = cleaned[:250]
                            break
                if titel_voll != titel:
                    break
            # Fallback: Titel-Zeile aus den gi-Tabellen
            if titel_voll == titel:
                for row in page.locator("table.gi tr").all()[:25]:
                    cells = row.locator("td, th")
                    if cells.count() >= 2:
                        label = cells.nth(0).inner_text().strip().lower()
                        if label in ("titel", "haupttitel", "titel / zusatztitel"):
                            titel_voll = cells.nth(1).inner_text().strip()[:250]
                            break

            # ---- Exemplar-Tabelle parsen (aDIS rTable) ----
            table = page.locator("table#resptable-1, table.rTable_table").first
            if table.count() == 0:
                logger.warning("Keine Exemplar-Tabelle gefunden für: %s", titel)
                return ergebnisse

            rows = table.locator("tr")
            # Header-Zeile auslesen, um Spalten-Indizes robust zu bestimmen
            header_cells = rows.nth(0).locator("th, td")
            col_map = {}
            for j in range(header_cells.count()):
                h = header_cells.nth(j).inner_text().strip().lower()
                if "bibliothek" in h:
                    col_map["bibliothek"] = j
                elif "standort" in h:
                    col_map["standort"] = j
                elif "signatur" in h:
                    col_map["signatur"] = j
                elif "bestell" in h:
                    col_map["bestell"] = j
                elif "verfügbarkeit" in h or "status" in h:
                    col_map["status"] = j

            # Fallback-Indizes falls Header nicht erkannt (übliche Reihenfolge)
            col_map.setdefault("bibliothek", 0)
            col_map.setdefault("standort", 1)
            col_map.setdefault("signatur", 2)
            col_map.setdefault("bestell", 3)
            col_map.setdefault("status", 4)

            for i in range(1, rows.count()):  # ab 1 = Datenzeilen
                cells = rows.nth(i).locator("td")
                if cells.count() < 2:
                    continue

                def cell(idx: int) -> str:
                    if idx < cells.count():
                        return cells.nth(idx).inner_text().strip()
                    return ""

                bibliothek = cell(col_map["bibliothek"])
                standort = cell(col_map["standort"])
                signatur = cell(col_map["signatur"])
                bestell = cell(col_map.get("bestell", 3))
                status_raw = cell(col_map["status"])

                # Leere / Header-ähnliche Zeilen überspringen
                if not bibliothek and not signatur:
                    continue

                ergebnisse.append({
                    "titel": titel_voll,
                    "status": _normalize_status(status_raw),
                    "bibliothek": bibliothek,
                    "standort": standort,
                    "signatur": signatur,
                    "bestellmoeglichkeit": bestell,
                })

        except PlaywrightTimeout as e:
            logger.error("Timeout bei OPAC-Suche für '%s': %s", titel, e)
        except Exception as e:
            logger.exception("Fehler bei OPAC-Suche für '%s': %s", titel, e)
        finally:
            browser.close()

    return ergebnisse


# ---------------------------------------------------------------------------
# CLI-Test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    q = " ".join(sys.argv[1:]) or "Harry Potter und der Stein der Weisen"
    print(f"Suche nach: {q!r}\n")
    results = search_duesseldorf(q)
    if not results:
        print("Keine Exemplare gefunden.")
    else:
        for r in results:
            print(f"📖 {r['titel']}")
            print(f"   Bibliothek : {r['bibliothek']}")
            print(f"   Standort   : {r['standort']}")
            print(f"   Signatur   : {r['signatur']}")
            print(f"   Status     : {r['status']}")
            if r.get("bestellmoeglichkeit"):
                print(f"   Bestellung : {r['bestellmoeglichkeit']}")
            print()
