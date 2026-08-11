"""
Robuste OPAC-Suche für Stadtbücherei Düsseldorf (aDIS / ITK Rheinland)
incl. Smart Watchlist, Telegram-Alerts und konfigurierbarem Cron-Scheduler.

Abholplan:
    python adis_search.py --plan-pickup --prefer-branch "Bücherei Bilk" "Sapiens"

Verwendung:
    from adis_search import search_duesseldorf, summarize

    results = search_duesseldorf("Harry Potter und der Stein der Weisen")
    print(summarize(results))

Watchlist:
    python adis_search.py --watch "Harry Potter"
    python adis_search.py --check-watchlist

Scheduler (automatische Checks):
    python adis_search.py --install-cron --from 8 --to 20 --every 3
    python adis_search.py --show-cron
    python adis_search.py --uninstall-cron

Telegram:
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

from pickup_planner import build_pickup_plan, parse_planning_time, render_pickup_plan

logger = logging.getLogger(__name__)

OPAC_START = "https://opac-duesseldorf.itk-rheinland.de/"

REQUIRED_KEYS = {
    "titel",
    "status",
    "bibliothek",
    "standort",
    "signatur",
    "bestellmoeglichkeit",
}

LAST_SEARCH_NOTE: str | None = None


def get_last_search_note() -> str | None:
    """Return a non-fatal diagnostic from the most recent live search."""

    return LAST_SEARCH_NOTE


def _normalise_search_text(value: str) -> str:
    text = (value or "").casefold().replace("_", " ")
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def _query_title_hints(query: str) -> list[str]:
    """Return possible title sides, prioritising the right side of ``author - title``."""

    parts = [part.strip() for part in re.split(r"\s+[-–—]\s+", query.strip(), maxsplit=1) if part.strip()]
    if len(parts) == 2:
        return [parts[1], parts[0]]
    slash_parts = [part.strip() for part in query.split(" / ", 1) if part.strip()]
    if len(slash_parts) == 2:
        return [slash_parts[0], slash_parts[1]]
    by_match = re.match(r"(.+?)\s+by\s+.+$", query.strip(), flags=re.IGNORECASE)
    if by_match:
        return [by_match.group(1).strip(), query.strip()]
    return [query.strip()]


def _primary_catalog_title(value: str) -> str:
    """Remove the author/recommendation suffix from a catalog result label."""

    return re.split(r"\s+/\s+|\s+-\s+", value.strip(), maxsplit=1)[0].strip()


def _primary_title_match_score(candidate: str, hint: str) -> int:
    candidate_text = _normalise_search_text(_primary_catalog_title(candidate))
    hint_text = _normalise_search_text(hint)
    if not candidate_text or not hint_text:
        return 0
    hint_tokens = set(hint_text.split())
    if candidate_text == hint_text:
        return 100
    if hint_text in candidate_text:
        # A one-word query must identify the beginning of the title. This
        # rejects a different work such as "Ich bin Circe" for a query for
        # "Circe", while still accepting catalog labels like "Circe : Roman".
        if len(hint_tokens) == 1 and not candidate_text.startswith(hint_text):
            return 0
        return 90
    candidate_tokens = set(candidate_text.split())
    if hint_tokens and hint_tokens <= candidate_tokens:
        if len(hint_tokens) == 1 and not candidate_text.startswith(hint_text):
            return 0
        return 80
    return 0


def _title_candidate_score(candidate: str, query: str) -> int:
    """Score only the primary title, never a recommendation/subtitle suffix."""

    return max((_primary_title_match_score(candidate, hint) for hint in _query_title_hints(query)), default=0)


def _extract_detail_title(page: Any) -> str | None:
    # The detail page contains several h2 headings before the actual title
    # (session warnings, "Vollanzeige", navigation sections). Prefer the
    # structured bibliographic field and only use a title-like heading as a
    # fallback. This prevents a generic heading from failing verification.
    title_rows = page.locator("table.gi tr")
    for i in range(min(title_rows.count(), 50)):
        cells = title_rows.nth(i).locator("td, th")
        if cells.count() < 2:
            continue
        label = _normalise_search_text(cells.nth(0).inner_text())
        if label in ("titel", "haupttitel", "titel zusatztitel"):
            value = re.sub(r"\s+", " ", cells.nth(1).inner_text().strip())
            if value:
                return value[:250]

    generic_headings = {
        "vollanzeige",
        "exemplarangaben",
        "anleitungen",
        "weg zum medium",
        "merkliste befüllen leeren",
        "weitere infos",
    }
    preferred: list[str] = []
    fallback: list[str] = []
    for selector in ["h2", "h1", ".detail-title", ".rTitle"]:
        elements = page.locator(selector)
        for i in range(min(elements.count(), 8)):
            raw = elements.nth(i).inner_text().strip()
            cleaned = re.sub(r"^Aktuelle Seite:\s*", "", raw, flags=re.IGNORECASE).strip()
            cleaned = re.sub(r"\s+", " ", cleaned)
            normalised = _normalise_search_text(cleaned)
            if (
                not cleaned
                or len(cleaned) <= 2
                or "sitzungsende" in normalised
                or normalised in generic_headings
            ):
                continue
            if raw.casefold().startswith("aktuelle seite") or " / " in cleaned:
                preferred.append(cleaned)
            else:
                fallback.append(cleaned)
    for candidate in preferred + fallback:
        if candidate:
            return candidate[:250]
    return None


def _wait_for_verified_detail_title(page: Any, query: str, attempts: int = 8) -> str | None:
    """Allow the OPAC's dynamic detail fields to render before rejecting a hit."""

    for attempt in range(max(1, attempts)):
        detail_title = _extract_detail_title(page)
        if detail_title:
            return detail_title if _title_candidate_score(detail_title, query) else None
        if attempt < attempts - 1:
            page.wait_for_timeout(500)
    return None


def _normalize_status(raw: str) -> str:
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
    validated: List[Dict[str, Any]] = []
    for i, item in enumerate(results):
        if not isinstance(item, dict):
            logger.warning("Ergebnis #%d ist kein Dict – übersprungen", i)
            continue
        missing = REQUIRED_KEYS - set(item.keys())
        if missing:
            logger.warning("Ergebnis #%d fehlt Schlüssel %s – übersprungen", i, sorted(missing))
            continue
        if not isinstance(item["titel"], str) or not item["titel"].strip():
            logger.warning("Ergebnis #%d: 'titel' ungültig – übersprungen", i)
            continue
        clean = {key: (str(item.get(key) or "").strip()) for key in REQUIRED_KEYS}
        validated.append(clean)
    return validated


def summarize(results: List[Dict[str, Any]]) -> str:
    if not results:
        return "Keine Exemplare gefunden."
    counts = {"verfügbar": 0, "entliehen": 0, "vorbestellt": 0, "sonstige": 0}
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
    available = []
    for r in results:
        status = (r.get("status") or "").lower()
        if status == "verfügbar":
            available.append(f"{r.get('bibliothek', '')}|{r.get('signatur', '')}")
    available.sort()
    return f"avail={len(available)}:" + ",".join(available)


def send_telegram(message: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        logger.info("Telegram nicht konfiguriert")
        print("ℹ️  Telegram-Hinweis: Setze TELEGRAM_BOT_TOKEN und TELEGRAM_CHAT_ID, um Alerts zu erhalten.")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
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
        available_count = sum(1 for r in results if (r.get("status") or "").lower() == "verfügbar")
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
                msg = f"📗 <b>Jetzt verfügbar!</b>\n{entry['display_title']}\n{summary}"
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


# ---------------------------------------------------------------------------
# Configurable scheduler (cron)
# ---------------------------------------------------------------------------

CRON_MARKER = "# duesseldorf-opac-search-watchlist"
SCHEDULER_CONFIG = Path(__file__).resolve().parent / "scheduler.json"
WRAPPER_SCRIPT = Path(__file__).resolve().parent / "check_watchlist.sh"


def build_cron_expression(from_hour: int, to_hour: int, every: int) -> str:
    if not (0 <= from_hour <= 23 and 0 <= to_hour <= 23):
        raise ValueError("Hours must be between 0 and 23")
    if every < 1 or every > 12:
        raise ValueError("--every must be between 1 and 12 hours")
    if from_hour > to_hour:
        raise ValueError("--from must be <= --to (same-day window only)")
    return f"0 {from_hour}-{to_hour}/{every} * * *"


def _describe_schedule(from_hour: int, to_hour: int, every: int) -> str:
    hours = list(range(from_hour, to_hour + 1, every))
    times = ", ".join(f"{h:02d}:00" for h in hours)
    return f"every {every}h between {from_hour:02d}:00–{to_hour:02d}:00 → {times}"


def _write_wrapper_script() -> Path:
    project_dir = Path(__file__).resolve().parent
    python = project_dir / "venv" / "bin" / "python"
    script = project_dir / "adis_search.py"
    if python.exists():
        runner = f'"{python}" "{script}" --check-watchlist'
    else:
        runner = f'python3 "{script}" --check-watchlist'
    content = f"""#!/bin/bash
# Auto-generated by adis_search.py — do not edit by hand
cd "{project_dir}" || exit 1

# Optional Telegram credentials (uncomment / set permanently via crontab env)
# export TELEGRAM_BOT_TOKEN="..."
# export TELEGRAM_CHAT_ID="..."

{runner}
"""
    WRAPPER_SCRIPT.write_text(content)
    WRAPPER_SCRIPT.chmod(0o755)
    return WRAPPER_SCRIPT


def _read_crontab() -> str:
    import subprocess
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        return result.stdout if result.returncode in (0, 1) else ""
    except FileNotFoundError:
        raise RuntimeError("crontab command not found. Install cron or use a different scheduler.")


def _write_crontab(content: str) -> None:
    import subprocess
    try:
        proc = subprocess.run(["crontab", "-"], input=content, text=True, capture_output=True)
    except FileNotFoundError:
        raise RuntimeError("crontab command not found. Install cron or use a different scheduler.")
    if proc.returncode != 0:
        raise RuntimeError(f"crontab update failed: {proc.stderr.strip()}")


def install_cron(from_hour: int = 8, to_hour: int = 20, every: int = 3) -> None:
    expr = build_cron_expression(from_hour, to_hour, every)
    wrapper = _write_wrapper_script()
    log_file = Path("/tmp/opac_watchlist.log")
    line = f'{expr} "{wrapper}" >> "{log_file}" 2>&1 {CRON_MARKER}'
    existing = _read_crontab()
    lines = [l for l in existing.splitlines() if CRON_MARKER not in l]
    lines.append(line)
    new_crontab = "\n".join(lines).rstrip() + "\n"
    _write_crontab(new_crontab)
    config = {
        "from_hour": from_hour,
        "to_hour": to_hour,
        "every": every,
        "cron": expr,
        "wrapper": str(wrapper),
        "installed_at": datetime.now(timezone.utc).isoformat(),
    }
    SCHEDULER_CONFIG.write_text(json.dumps(config, indent=2))
    print("✅ Cron job installed")
    print(f"   Schedule : {_describe_schedule(from_hour, to_hour, every)}")
    print(f"   Expression: {expr}")
    print(f"   Wrapper  : {wrapper}")
    print(f"   Log      : {log_file}")
    print()
    print("Tip: put TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in the wrapper or your shell profile.")


def uninstall_cron() -> None:
    existing = _read_crontab()
    lines = [l for l in existing.splitlines() if CRON_MARKER not in l]
    new_crontab = "\n".join(lines).rstrip()
    if new_crontab:
        new_crontab += "\n"
    _write_crontab(new_crontab)
    if SCHEDULER_CONFIG.exists():
        SCHEDULER_CONFIG.unlink()
    if WRAPPER_SCRIPT.exists():
        WRAPPER_SCRIPT.unlink()
    print("🗑️  Cron job removed.")


def show_cron() -> None:
    if SCHEDULER_CONFIG.exists():
        try:
            cfg = json.loads(SCHEDULER_CONFIG.read_text())
            print("📋 Saved schedule config:")
            print(f"   {_describe_schedule(cfg['from_hour'], cfg['to_hour'], cfg['every'])}")
            print(f"   Cron: {cfg.get('cron')}")
            print(f"   Installed: {cfg.get('installed_at', '?')[:19]}")
            print()
        except Exception:
            pass
    try:
        existing = _read_crontab()
        found = [l for l in existing.splitlines() if CRON_MARKER in l]
    except RuntimeError as e:
        print(f"⚠️  {e}")
        found = []
    if found:
        print("Current crontab entry:")
        for l in found:
            print(f"  {l}")
    else:
        print("No cron job installed for this project.")
        print("Install one with e.g.:")
        print("  python adis_search.py --install-cron --from 8 --to 20 --every 3")


def search_duesseldorf(titel: str, max_results: int = 5) -> List[Dict[str, Any]]:
    global LAST_SEARCH_NOTE
    LAST_SEARCH_NOTE = None
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
                candidates = []
                for i in range(min(title_links.count(), 25)):
                    link = title_links.nth(i)
                    link_text = link.inner_text().strip()
                    score = _title_candidate_score(link_text, titel)
                    if score:
                        candidates.append((score, i, link_text))
                candidates.sort(key=lambda candidate: (-candidate[0], candidate[1]))
                clicked = False
                for score, index, link_text in candidates:
                    logger.debug("Prüfe Treffer (score=%d): %s", score, link_text[:80])
                    title_links.nth(index).click()
                    page.wait_for_load_state("networkidle")
                    page.wait_for_timeout(2000)
                    detail_title = _wait_for_verified_detail_title(page, titel)
                    if detail_title:
                        clicked = True
                        break
                    logger.warning("Titel nicht verifiziert nach Treffer: %s", link_text[:120])
                    try:
                        page.go_back(wait_until="networkidle")
                        page.wait_for_timeout(500)
                    except Exception:
                        break
                if not clicked:
                    LAST_SEARCH_NOTE = f"Titel nicht verifiziert: Kein OPAC-Treffer bestätigt den Haupttitel für {titel!r}."
                    return ergebnisse

            titel_voll = _wait_for_verified_detail_title(page, titel)
            if not titel_voll:
                LAST_SEARCH_NOTE = f"Titel nicht verifiziert: Kein OPAC-Treffer bestätigt den Haupttitel für {titel!r}."
                return ergebnisse
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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Suche im OPAC der Stadtbüchereien Düsseldorf + Smart Watchlist"
    )
    parser.add_argument("titel", nargs="*", help="Suchbegriff / Titel")
    parser.add_argument("--json", action="store_true", help="Ergebnis als valides JSON ausgeben")
    parser.add_argument("--max", type=int, default=0, help="Maximale Anzahl Exemplare (0 = alle)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Ausführliche Logs")
    parser.add_argument("--watch", metavar="TITEL", help="Titel zur Watchlist hinzufügen")
    parser.add_argument("--unwatch", metavar="TITEL", help="Titel von der Watchlist entfernen")
    parser.add_argument("--list-watch", action="store_true", help="Aktuelle Watchlist anzeigen")
    parser.add_argument("--check-watchlist", action="store_true", help="Watchlist prüfen + ggf. Telegram")
    parser.add_argument("--no-notify", action="store_true", help="Keine Telegram-Nachricht senden")
    parser.add_argument("--install-cron", action="store_true", help="Cron-Job installieren")
    parser.add_argument("--uninstall-cron", action="store_true", help="Cron-Job entfernen")
    parser.add_argument("--show-cron", action="store_true", help="Aktuellen Cron-Schedule anzeigen")
    parser.add_argument("--from", dest="from_hour", type=int, default=8, metavar="HOUR", help="Startstunde (0-23)")
    parser.add_argument("--to", dest="to_hour", type=int, default=20, metavar="HOUR", help="Endstunde (0-23)")
    parser.add_argument("--every", type=int, default=3, metavar="HOURS", help="Intervall in Stunden")
    parser.add_argument(
        "--plan-pickup",
        action="store_true",
        help="Plant die beste Abholung anhand von Verfügbarkeit, Öffnungszeit und Filial-Präferenz",
    )
    parser.add_argument(
        "--prefer-branch",
        action="append",
        default=[],
        metavar="NAME",
        help="Bevorzugte Filiale; mehrfach angeben für eine Reihenfolge",
    )
    parser.add_argument(
        "--at",
        metavar="ISO-DATETIME",
        help="Planungszeitpunkt für --plan-pickup, z. B. 2026-08-10T16:30",
    )
    parser.add_argument(
        "--pickup-buffer-minutes",
        type=int,
        default=20,
        metavar="MINUTES",
        help="Mindestzeit bis zur Schließung für eine sichere Abholung (default 20)",
    )
    args = parser.parse_args()

    if args.at and not args.plan_pickup:
        parser.error("--at kann nur zusammen mit --plan-pickup verwendet werden")
    if args.prefer_branch and not args.plan_pickup:
        parser.error("--prefer-branch kann nur zusammen mit --plan-pickup verwendet werden")
    if args.pickup_buffer_minutes < 0:
        parser.error("--pickup-buffer-minutes darf nicht negativ sein")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )

    if args.install_cron:
        try:
            install_cron(args.from_hour, args.to_hour, args.every)
        except (ValueError, RuntimeError) as e:
            print(f"❌ {e}")
            sys.exit(1)
        return
    if args.uninstall_cron:
        try:
            uninstall_cron()
        except RuntimeError as e:
            print(f"❌ {e}")
            sys.exit(1)
        return
    if args.show_cron:
        show_cron()
        return
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
    search_note = get_last_search_note()
    if args.max > 0:
        results = results[: args.max]
    summary = summarize(results)

    if args.plan_pickup:
        try:
            planning_time = parse_planning_time(args.at) if args.at else None
        except ValueError as e:
            parser.error(str(e))
        plan = build_pickup_plan(
            query,
            results,
            now=planning_time,
            preferred_branches=args.prefer_branch,
            minimum_pickup_minutes=args.pickup_buffer_minutes,
        )
        if args.json:
            payload = {
                "query": query,
                "summary": summary,
                "count": len(results),
                "exemplare": results,
                "pickup_plan": plan.as_dict(),
                "search_note": search_note,
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            if search_note:
                print(f"⚠️ {search_note}\n")
            print(render_pickup_plan(plan))
        return

    if args.json:
        payload = {"query": query, "summary": summary, "count": len(results), "exemplare": results, "search_note": search_note}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    if not results:
        if search_note:
            print(f"⚠️ {search_note}")
            return
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
