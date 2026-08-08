"""
Robuste OPAC-Suche für Stadtbücherei Düsseldorf (aDIS / ITK Rheinland)
incl. Smart Watchlist mit Change-Detection und optionalem Telegram-Alert.

Verwendung:
    from adis_search import search_duesseldorf, summarize

    results = search_duesseldorf("Harry Potter und der Stein der Weisen")
    print(summarize(results))
    # → "✅ 2 verfügbar · 🔴 3 entliehen"

Watchlist (CLI):
    python adis_search.py --watch "Harry Potter"
    python adis_search.py --check-watchlist
    python adis_search.py --list-watch

Telegram-Alerts:
    export TELEGRAM_BOT_TOKEN="..."
    export TELEGRAM_CHAT_ID="..."
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)

# Freundliche Start-URL (redirectet intern auf den korrekten DirectLink mit SOPAC07)
OPAC_START = "https://opac-duesseldorf.itk-rheinland.de/"

# Erwartete Schlüssel eines Ergebnis-Dicts (für Validierung)
REQUIRED_KEYS = {
    "titel",
    "status",
    "bibliothek",
    "standort",
    "signatur",
    "bestellmoeglichkeit",
}


def _normalize_status(raw: str) -> str:
    """Vereinheitlicht Status-Strings aus dem OPAC."""
    t = (raw or "").strip().lower()
    if not t:
        return "unbekannt"
    if "verfügbar" in t and "ausgeliehen" not in t:
        return "verfügbar"
    if "ausgeliehen" in t or "entliehen" in t:
        return raw.strip() if "fällig" in t else "entliehen"
    if "vorbestellt" in t or "reserviert" in t:
        return "vorbestellt"
    if "nicht ausleihbar" in t or "präsenz" in t:
        return "nicht ausleihbar"
    return raw.strip() or "unbekannt"


def validate_results(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Prüft, dass jedes Ergebnis-Dict die erwarteten Schlüssel besitzt und
    die Werte sinnvolle Typen haben. Ungültige Einträge werden entfernt
    und eine Warnung geloggt.
    """
    validated: List[Dict[str, Any]] = []

    for i, item in enumerate(results):
        if not isinstance(item, dict):
            logger.warning("Ergebnis #%d ist kein Dict – übersprungen", i)
            continue

        missing = REQUIRED_KEYS - set(item.keys())
        if missing:
            logger.warning(
                "Ergebnis #%d fehlt Schlüssel %s – übersprungen", i, sorted(missing)
            )
            continue

        if not isinstance(item["titel"], str) or not item["titel"].strip():
            logger.warning("Ergebnis #%d: 'titel' ungültig – übersprungen", i)
            continue

        clean = {
            key: (str(item.get(key) or "").strip())
            for key in REQUIRED_KEYS
        }
        validated.append(clean)

    return validated


def summarize(results: List[Dict[str, Any]]) -> str:
    """
    Erstellt eine kurze Verfügbarkeits-Zusammenfassung.

    Beispiel:
        "✅ 2 verfügbar · 🔴 3 entliehen · ⏳ 1 vorbestellt"
    """
    if not results:
        return "Keine Exemplare gefunden."

    counts = {
        "verfügbar": 0,
        "entliehen": 0,
        "vorbestellt": 0,
        "sonstige": 0,
    }

    for r in results:
        status = (r.get("status") or "").lower()
        if status == "verfügbar":
            counts["verfügbar"] += 1
        elif "entliehen" in status or "ausgeliehen" in status:
            counts["entliehen"] += 1
        elif "vorbestellt" in status or "reserviert" in status:
            counts["vorbestellt"] += 1
        else:
            counts["sonstige"] += 1

    parts = []
    if counts["verfügbar"]:
        parts.append(f"✅ {counts['verfügbar']} verfügbar")
    if counts["entliehen"]:
        parts.append(f"🔴 {counts['entliehen']} entliehen")
    if counts["vorbestellt"]:
        parts.append(f"⏳ {counts['vorbestellt']} vorbestellt")
    if counts["sonstige"]:
        parts.append(f"⚪ {counts['sonstige']} sonstige")

    return " · ".join(parts) if parts else "Keine Exemplare gefunden."


# ---------------------------------------------------------------------------
# Smart Watchlist
# ---------------------------------------------------------------------------

WATCHLIST_FILE = Path(__file__).resolve().parent / "watchlist.json"


def _load_watchlist() -> Dict[str, Any]:
    if not WATCHLIST_FILE.exists():
        return {"titles": {}}
    try:
        with open(WATCHLIST_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if "titles" not in data:
            data = {"titles": {}}
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Konnte Watchlist nicht lesen: %s – starte neu", e)
        return {"titles": {}}


def _save_watchlist(data: Dict[str, Any]) -> None:
    with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _availability_fingerprint(results: List[Dict[str, Any]]) -> str:
    """Kompakte Signatur der aktuellen Verfügbarkeit (für Change-Detection)."""
    available = []
    for r in results:
        status = (r.get("status") or "").lower()
        if status == "verfügbar":
            available.append(f"{r.get('bibliothek', '')}|{r.get('signatur', '')}")
    available.sort()
    return f"avail={len(available)}:" + ",".join(available)


def send_telegram(message: str) -> bool:
    """
    Sendet eine Nachricht per Telegram Bot API.
    Benötigt die Umgebungsvariablen:
        TELEGRAM_BOT_TOKEN
        TELEGRAM_CHAT_ID
    Gibt True zurück wenn erfolgreich gesendet.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    if not token or not chat_id:
        logger.info("Telegram nicht konfiguriert (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID fehlen)")
        print("ℹ️  Telegram-Hinweis: Setze TELEGRAM_BOT_TOKEN und TELEGRAM_CHAT_ID, um Alerts zu erhalten.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
            if result.get("ok"):
                print("📲 Telegram-Nachricht gesendet.")
                return True
            logger.warning("Telegram API Fehler: %s", result)
            return False
    except Exception as e:
        logger.error("Telegram senden fehlgeschlagen: %s", e)
        print(f"❌ Telegram-Fehler: {e}")
        return False


def add_to_watchlist(titel: str) -> None:
    """Titel zur Watchlist hinzufügen und initial prüfen."""
    titel = titel.strip()
    if not titel:
        print("Bitte einen Titel angeben.")
        return

    print(f"🔍 Prüfe initial: {titel!r} …")
    results = search_duesseldorf(titel)
    summary = summarize(results)
    fingerprint = _availability_fingerprint(results)
    display_title = results[0]["titel"] if results else titel

    data = _load_watchlist()
    data["titles"][titel] = {
        "display_title": display_title,
        "added": datetime.now(timezone.utc).isoformat(),
        "last_check": datetime.now(timezone.utc).isoformat(),
        "last_summary": summary,
        "fingerprint": fingerprint,
        "available_count": sum(1 for r in results if (r.get("status") or "").lower() == "verfügbar"),
    }
    _save_watchlist(data)

    print(f"✅ Zur Watchlist hinzugefügt: {display_title}")
    print(f"   {summary}")
    print(f"   (gespeichert in {WATCHLIST_FILE.name})")


def remove_from_watchlist(titel: str) -> None:
    data = _load_watchlist()
    key = titel.strip()
    if key not in data["titles"]:
        matches = [k for k in data["titles"] if key.lower() in k.lower()]
        if len(matches) == 1:
            key = matches[0]
        elif not matches:
            print(f"Titel nicht in der Watchlist: {titel!r}")
            return
        else:
            print("Mehrere Treffer – bitte genauer angeben:")
            for m in matches:
                print(f"  • {m}")
            return

    del data["titles"][key]
    _save_watchlist(data)
    print(f"🗑️  Entfernt: {key}")


def list_watchlist() -> None:
    data = _load_watchlist()
    titles = data.get("titles", {})
    if not titles:
        print("Watchlist ist leer.")
        return
    print(f"📋 Watchlist ({len(titles)} Titel):\n")
    for key, entry in titles.items():
        print(f"• {entry.get('display_title', key)}")
        print(f"  Letzter Stand: {entry.get('last_summary', '?')}")
        print(f"  Geprüft:       {entry.get('last_check', '?')[:19]}")
        print()


def check_watchlist(notify: bool = True) -> List[str]:
    """
    Prüft alle Watchlist-Einträge und meldet Änderungen.
    Gibt eine Liste der Änderungs-Nachrichten zurück.
    """
    data = _load_watchlist()
    titles = data.get("titles", {})
    if not titles:
        print("Watchlist ist leer. Füge Titel mit --watch hinzu.")
        return []

    changes: List[str] = []
    print(f"🔄 Prüfe {len(titles)} Titel …\n")

    for key, entry in list(titles.items()):
        display = entry.get("display_title", key)
        print(f"  → {display[:60]} …", end=" ", flush=True)

        try:
            results = search_duesseldorf(key)
        except Exception as e:
            print(f"Fehler: {e}")
            continue

        summary = summarize(results)
        fingerprint = _availability_fingerprint(results)
        available_count = sum(
            1 for r in results if (r.get("status") or "").lower() == "verfügbar"
        )
        old_fp = entry.get("fingerprint", "")
        old_count = entry.get("available_count", -1)

        entry["last_check"] = datetime.now(timezone.utc).isoformat()
        entry["last_summary"] = summary
        entry["fingerprint"] = fingerprint
        entry["available_count"] = available_count
        if results:
            entry["display_title"] = results[0]["titel"]

        if fingerprint != old_fp:
            if available_count > 0 and (old_count == 0 or available_count > old_count):
                human = f"📗 Jetzt verfügbar: {entry['display_title']}\n   {summary}"
                msg = (
                    f"📗 <b>Jetzt verfügbar!</b>\n"
                    f"{entry['display_title']}\n"
                    f"{summary}"
                )
            elif available_count == 0 and old_count > 0:
                human = f"📕 Nicht mehr verfügbar: {entry['display_title']}\n   {summary}"
                msg = f"📕 <b>Nicht mehr verfügbar</b>\n{entry['display_title']}\n{summary}"
            else:
                human = f"🔄 Änderung: {entry['display_title']}\n   {summary}"
                msg = f"🔄 <b>Änderung</b>\n{entry['display_title']}\n{summary}"

            print("ÄNDERUNG")
            print(f"     {human}")
            changes.append(human)
            if notify:
                send_telegram(msg)
        else:
            print(f"unverändert ({summary})")

    _save_watchlist(data)

    if not changes:
        print("\nKeine Änderungen festgestellt.")
    else:
        print(f"\n{len(changes)} Änderung(en) gefunden.")
    return changes


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

            page.goto(OPAC_START, wait_until="domcontentloaded")
            page.wait_for_selector("#Autosuggest", state="visible", timeout=15000)

            search_input = page.locator("#Autosuggest")
            search_input.fill(titel)
            page.locator("input.suche-starten").click()

            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1500)

            holdings_table = page.locator("table#resptable-1, table.rTable_table")
            on_detail = holdings_table.count() > 0 and holdings_table.first.locator("tr").count() > 1

            if not on_detail:
                title_links = page.locator(".rList_col.rList_titel a")
                clicked = False
                search_words = [w.lower() for w in titel.split() if len(w) > 2][:3]

                for i in range(min(title_links.count(), 25)):
                    link = title_links.nth(i)
                    link_text = link.inner_text().strip()
                    link_lower = link_text.lower()
                    if any(w in link_lower for w in search_words) or titel.lower() in link_lower:
                        logger.debug("Klicke Treffer: %s", link_text[:80])
                        link.click()
                        page.wait_for_load_state("networkidle")
                        page.wait_for_timeout(2000)
                        clicked = True
                        break

                if not clicked and title_links.count() > 0:
                    title_links.first.click()
                    page.wait_for_load_state("networkidle")
                    page.wait_for_timeout(2000)

            titel_voll = titel
            for sel in ["h2", "h1", ".detail-title", ".rTitle"]:
                els = page.locator(sel)
                for i in range(min(els.count(), 6)):
                    raw = els.nth(i).inner_text().strip()
                    if not raw:
                        continue
                    cleaned = re.sub(r"^Aktuelle Seite:\s*", "", raw, flags=re.I).strip()
                    if cleaned and "sitzungsende" not in cleaned.lower() and "anleitung" not in cleaned.lower():
                        if len(cleaned) > 10:
                            titel_voll = cleaned[:250]
                            break
                if titel_voll != titel:
                    break
            if titel_voll == titel:
                for row in page.locator("table.gi tr").all()[:25]:
                    cells = row.locator("td, th")
                    if cells.count() >= 2:
                        label = cells.nth(0).inner_text().strip().lower()
                        if label in ("titel", "haupttitel", "titel / zusatztitel"):
                            titel_voll = cells.nth(1).inner_text().strip()[:250]
                            break

            table = page.locator("table#resptable-1, table.rTable_table").first
            if table.count() == 0:
                logger.warning("Keine Exemplar-Tabelle gefunden für: %s", titel)
                return ergebnisse

            rows = table.locator("tr")
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

            col_map.setdefault("bibliothek", 0)
            col_map.setdefault("standort", 1)
            col_map.setdefault("signatur", 2)
            col_map.setdefault("bestell", 3)
            col_map.setdefault("status", 4)

            for i in range(1, rows.count()):
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

    return validate_results(ergebnisse)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Suche im OPAC der Stadtbüchereien Düsseldorf + Smart Watchlist"
    )
    parser.add_argument(
        "titel",
        nargs="*",
        help="Suchbegriff / Titel",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Ergebnis als valides JSON ausgeben",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=0,
        help="Maximale Anzahl Exemplare (0 = alle)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Ausführliche Logs",
    )
    parser.add_argument(
        "--watch",
        metavar="TITEL",
        help="Titel zur Watchlist hinzufügen und initial prüfen",
    )
    parser.add_argument(
        "--unwatch",
        metavar="TITEL",
        help="Titel von der Watchlist entfernen",
    )
    parser.add_argument(
        "--list-watch",
        action="store_true",
        help="Aktuelle Watchlist anzeigen",
    )
    parser.add_argument(
        "--check-watchlist",
        action="store_true",
        help="Alle Watchlist-Titel prüfen und bei Änderungen Telegram-Alert senden",
    )
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="Bei --check-watchlist keine Telegram-Nachricht senden",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )

    if args.watch:
        add_to_watchlist(args.watch)
        return
    if args.unwatch:
        remove_from_watchlist(args.unwatch)
        return
    if args.list_watch:
        list_watchlist()
        return
    if args.check_watchlist:
        check_watchlist(notify=not args.no_notify)
        return

    query = " ".join(args.titel).strip() or "Harry Potter und der Stein der Weisen"
    results = search_duesseldorf(query)

    if args.max > 0:
        results = results[: args.max]

    summary = summarize(results)

    if args.json:
        payload = {
            "query": query,
            "summary": summary,
            "count": len(results),
            "exemplare": results,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    if not results:
        print("Keine Exemplare gefunden.")
        return

    titel = results[0]["titel"]
    print(f"📖 {titel}")
    print(f"   {summary}\n")

    for r in results:
        print(f"   • {r['bibliothek']} – {r['standort']}")
        print(f"     {r['signatur']}  →  {r['status']}")
        print()


if __name__ == "__main__":
    main()
