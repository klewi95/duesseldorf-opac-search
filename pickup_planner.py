"""Turn Düsseldorf OPAC holdings into an actionable pickup recommendation.

The OPAC tells us *which* copies exist.  This module adds the smallest useful
decision layer on top: regular branch opening hours, a user supplied branch
preference order, and an honest distinction between "available" and "safe to
pick up today".

It deliberately does not geocode an address or scrape opening hours at run
time.  Both would make a simple catalog query slower and less reliable.  A
future routing provider can add travel-time scores without changing the public
planner API.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo


BERLIN_TZ = ZoneInfo("Europe/Berlin")
BRANCH_DIRECTORY_FILE = Path(__file__).with_name("branches.json")

DAY_KEYS = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}
GERMAN_DAYS = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")
ACCESS_LABELS = {
    "open": "regulär geöffnet",
    "self_service": "Selbstbedienung",
}


def _normalise_name(value: str) -> str:
    """Create a forgiving lookup key without a third-party transliteration lib."""

    text = (value or "").casefold().strip()
    text = text.translate(str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}))
    return re.sub(r"[^a-z0-9]+", "", text)


def _parse_time(value: str) -> time:
    try:
        return time.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Ungültige Uhrzeit in branches.json: {value!r}") from exc


def _as_berlin(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(BERLIN_TZ)
    if value.tzinfo is None:
        return value.replace(tzinfo=BERLIN_TZ)
    return value.astimezone(BERLIN_TZ)


def parse_planning_time(value: str) -> datetime:
    """Parse the CLI's ISO timestamp and return a Europe/Berlin instant."""

    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Erwartet ISO-Zeit, z. B. 2026-08-10T16:30") from exc
    return _as_berlin(parsed)


@dataclass(frozen=True)
class OpeningWindow:
    start: time
    end: time
    access: str = "open"


@dataclass(frozen=True)
class Branch:
    name: str
    aliases: tuple[str, ...]
    address: str
    url: str
    hours: Mapping[int, tuple[OpeningWindow, ...]]


@dataclass(frozen=True)
class BranchDirectory:
    branches: tuple[Branch, ...]
    source_url: str
    verified_on: str

    def find(self, name: str) -> Branch | None:
        wanted = _normalise_name(name)
        for branch in self.branches:
            if wanted in {_normalise_name(branch.name), *(_normalise_name(alias) for alias in branch.aliases)}:
                return branch
        return None


@dataclass(frozen=True)
class HoursStatus:
    state: str
    access: str | None = None
    closes_at: datetime | None = None
    opens_at: datetime | None = None
    next_closes_at: datetime | None = None


@dataclass(frozen=True)
class PickupOption:
    holding: Mapping[str, str]
    branch: Branch | None
    available: bool
    can_pick_up_today: bool
    hours: HoursStatus
    preference_rank: int | None
    score: int
    source_index: int

    @property
    def branch_name(self) -> str:
        return self.branch.name if self.branch else (self.holding.get("bibliothek") or "Unbekannte Bibliothek")

    def as_dict(self) -> dict[str, Any]:
        return {
            "bibliothek": self.branch_name,
            "address": self.branch.address if self.branch else None,
            "branch_url": self.branch.url if self.branch else None,
            "available": self.available,
            "can_pick_up_today": self.can_pick_up_today,
            "hours": {
                "state": self.hours.state,
                "access": self.hours.access,
                "closes_at": self.hours.closes_at.isoformat() if self.hours.closes_at else None,
                "opens_at": self.hours.opens_at.isoformat() if self.hours.opens_at else None,
                "next_closes_at": self.hours.next_closes_at.isoformat() if self.hours.next_closes_at else None,
            },
            "preference_rank": self.preference_rank,
            "score": self.score,
            "exemplar": dict(self.holding),
        }


@dataclass(frozen=True)
class PickupPlan:
    query: str
    created_at: datetime
    options: tuple[PickupOption, ...]
    directory: BranchDirectory
    minimum_pickup_minutes: int
    preferred_branches: tuple[str, ...]

    @property
    def today_options(self) -> tuple[PickupOption, ...]:
        return tuple(option for option in self.options if option.can_pick_up_today)

    @property
    def available_options(self) -> tuple[PickupOption, ...]:
        return tuple(option for option in self.options if option.available)

    @property
    def recommendation(self) -> PickupOption | None:
        return self.today_options[0] if self.today_options else (self.available_options[0] if self.available_options else None)

    def as_dict(self) -> dict[str, Any]:
        recommendation = self.recommendation
        return {
            "query": self.query,
            "planned_at": self.created_at.isoformat(),
            "can_pick_up_today": bool(self.today_options),
            "minimum_pickup_minutes": self.minimum_pickup_minutes,
            "preferred_branches": list(self.preferred_branches),
            "recommendation": recommendation.as_dict() if recommendation else None,
            "today_options": [option.as_dict() for option in self.today_options],
            "available_later": [
                option.as_dict()
                for option in self.available_options
                if option not in self.today_options
            ],
            "unavailable_options": [option.as_dict() for option in self.options if not option.available],
            "hours_source": self.directory.source_url,
            "hours_verified_on": self.directory.verified_on,
            "limitations": [
                "Only regular opening hours are considered; public holidays and short-notice closures are not included.",
                "A listed available copy can still be unavailable by the time of arrival.",
            ],
        }


def load_branch_directory(path: Path | None = None) -> BranchDirectory:
    """Load the deliberately human-editable regular-hours directory."""

    data_path = path or BRANCH_DIRECTORY_FILE
    try:
        raw = json.loads(data_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Branch-Verzeichnis konnte nicht gelesen werden: {data_path}") from exc

    branches: list[Branch] = []
    try:
        for item in raw["branches"]:
            hours: dict[int, tuple[OpeningWindow, ...]] = {}
            for day, windows in item["hours"].items():
                if day not in DAY_KEYS:
                    raise ValueError(f"Unbekannter Wochentag: {day}")
                parsed_windows = tuple(
                    OpeningWindow(
                        start=_parse_time(window["from"]),
                        end=_parse_time(window["to"]),
                        access=window.get("access", "open"),
                    )
                    for window in windows
                )
                if any(window.end <= window.start for window in parsed_windows):
                    raise ValueError(f"Endzeit muss nach Startzeit liegen: {item['name']} / {day}")
                hours[DAY_KEYS[day]] = parsed_windows
            branches.append(
                Branch(
                    name=item["name"],
                    aliases=tuple(item.get("aliases", ())),
                    address=item["address"],
                    url=item.get("url", raw["source_url"]),
                    hours=hours,
                )
            )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"Ungültiges Branch-Verzeichnis: {data_path}") from exc

    return BranchDirectory(
        branches=tuple(branches),
        source_url=raw["source_url"],
        verified_on=raw["verified_on"],
    )


def _access_priority(access: str) -> int:
    # An overlapping self-service window is more useful to communicate than the
    # broader public opening window that contains it.
    return 1 if access == "self_service" else 0


def _next_opening(branch: Branch, now: datetime) -> tuple[datetime, datetime, str] | None:
    for offset in range(8):
        target_date = (now + timedelta(days=offset)).date()
        for window in branch.hours.get(target_date.weekday(), ()):
            opens_at = datetime.combine(target_date, window.start, tzinfo=now.tzinfo)
            closes_at = datetime.combine(target_date, window.end, tzinfo=now.tzinfo)
            if opens_at > now:
                return opens_at, closes_at, window.access
    return None


def opening_status(branch: Branch | None, now: datetime, closing_soon_minutes: int = 30) -> HoursStatus:
    """Return open/closed truth without pretending that holiday closures are known."""

    if branch is None:
        return HoursStatus(state="unknown")

    today_windows = branch.hours.get(now.weekday(), ())
    active = [window for window in today_windows if window.start <= now.timetz().replace(tzinfo=None) < window.end]
    if active:
        chosen = max(active, key=lambda window: _access_priority(window.access))
        closes_at = datetime.combine(now.date(), max(window.end for window in active), tzinfo=now.tzinfo)
        minutes_left = (closes_at - now).total_seconds() / 60
        state = "closing_soon" if minutes_left < closing_soon_minutes else "open"
        return HoursStatus(state=state, access=chosen.access, closes_at=closes_at)

    next_window = _next_opening(branch, now)
    if next_window is None:
        return HoursStatus(state="closed")
    opens_at, closes_at, access = next_window
    return HoursStatus(state="closed", access=access, opens_at=opens_at, next_closes_at=closes_at)


def _is_available(holding: Mapping[str, Any]) -> bool:
    status = str(holding.get("status") or "").casefold()
    return "verfügbar" in status and "nicht verfügbar" not in status and "zurzeit nicht verfügbar" not in status


def _preference_rank(branch_name: str, preferred_branches: Sequence[str]) -> int | None:
    wanted = _normalise_name(branch_name)
    for index, preferred in enumerate(preferred_branches):
        if wanted == _normalise_name(preferred):
            return index
    return None


def _can_pick_up_today(
    available: bool,
    hours: HoursStatus,
    now: datetime,
    minimum_pickup_minutes: int,
) -> bool:
    if not available or hours.state == "unknown":
        return False
    if hours.state in {"open", "closing_soon"} and hours.closes_at:
        return (hours.closes_at - now).total_seconds() >= minimum_pickup_minutes * 60
    if hours.opens_at and hours.next_closes_at and hours.opens_at.date() == now.date():
        return (hours.next_closes_at - hours.opens_at).total_seconds() >= minimum_pickup_minutes * 60
    return False


def _score_option(
    available: bool,
    can_pick_up_today: bool,
    hours: HoursStatus,
    preference_rank: int | None,
    known_branch: bool,
) -> int:
    score = 1_000 if available else 0
    if can_pick_up_today:
        score += 500
    if hours.state == "open":
        score += 100
    elif hours.state == "closing_soon":
        score -= 75
    elif hours.state == "unknown":
        score -= 50
    if preference_rank is not None:
        score += max(0, 300 - preference_rank * 25)
    if known_branch:
        score += 10
    return score


def build_pickup_plan(
    query: str,
    holdings: Sequence[Mapping[str, Any]],
    *,
    now: datetime | None = None,
    preferred_branches: Iterable[str] = (),
    minimum_pickup_minutes: int = 20,
    directory: BranchDirectory | None = None,
) -> PickupPlan:
    """Rank copies by availability, regular hours, then user branch preference.

    ``preferred_branches`` is intentionally an ordered list rather than a fake
    distance estimate.  It provides a useful, transparent MVP for "near me"
    until a consented routing provider is added.
    """

    if minimum_pickup_minutes < 0:
        raise ValueError("minimum_pickup_minutes darf nicht negativ sein")
    planning_time = _as_berlin(now)
    branch_directory = directory or load_branch_directory()
    preferences = tuple(value.strip() for value in preferred_branches if value and value.strip())
    options: list[PickupOption] = []

    for source_index, source_holding in enumerate(holdings):
        holding = {key: str(value or "") for key, value in source_holding.items()}
        branch_name = holding.get("bibliothek") or ""
        branch = branch_directory.find(branch_name)
        available = _is_available(holding)
        hours = opening_status(branch, planning_time)
        preference_rank = _preference_rank(branch.name if branch else branch_name, preferences)
        can_pick_up_today = _can_pick_up_today(available, hours, planning_time, minimum_pickup_minutes)
        score = _score_option(available, can_pick_up_today, hours, preference_rank, branch is not None)
        options.append(
            PickupOption(
                holding=holding,
                branch=branch,
                available=available,
                can_pick_up_today=can_pick_up_today,
                hours=hours,
                preference_rank=preference_rank,
                score=score,
                source_index=source_index,
            )
        )

    options.sort(key=lambda option: (-option.score, option.source_index))
    return PickupPlan(
        query=query,
        created_at=planning_time,
        options=tuple(options),
        directory=branch_directory,
        minimum_pickup_minutes=minimum_pickup_minutes,
        preferred_branches=preferences,
    )


def _format_timestamp(value: datetime) -> str:
    return f"{GERMAN_DAYS[value.weekday()]}, {value:%d.%m.} um {value:%H:%M} Uhr"


def _describe_hours(option: PickupOption, planning_date: datetime | None = None) -> str:
    hours = option.hours
    if hours.state == "unknown":
        return "Öffnungszeit für diese Filiale noch nicht im Verzeichnis"
    access_suffix = f" ({ACCESS_LABELS.get(hours.access or '', hours.access or '')})" if hours.access else ""
    if hours.state == "open" and hours.closes_at:
        return f"jetzt geöffnet bis {hours.closes_at:%H:%M} Uhr{access_suffix}"
    if hours.state == "closing_soon" and hours.closes_at:
        return f"schließt bald um {hours.closes_at:%H:%M} Uhr{access_suffix}"
    if hours.opens_at:
        prefix = "öffnet heute" if planning_date and hours.opens_at.date() == planning_date.date() else "öffnet wieder"
        return f"{prefix} {_format_timestamp(hours.opens_at)}{access_suffix}"
    return "heute regulär geschlossen"


def _due_date(status: str) -> str | None:
    match = re.search(r"fällig\s+am:\s*([0-9]{1,2}\.[0-9]{1,2}\.[0-9]{4})", status, flags=re.IGNORECASE)
    return match.group(1) if match else None


def _holding_details(option: PickupOption) -> str:
    location = option.holding.get("standort") or "Standort nicht angegeben"
    signature = option.holding.get("signatur") or "Signatur nicht angegeben"
    return f"{location} · {signature}"


def render_pickup_plan(plan: PickupPlan) -> str:
    """Render a concise, human-first answer for the existing CLI and skill."""

    lines = [f"🧭 Abholplan: {plan.query}"]
    if not plan.options:
        lines.append("\nKeine Exemplare im OPAC gefunden.")
        return "\n".join(lines)

    today = plan.today_options
    if today:
        best = today[0]
        lines.extend(
            [
                f"\n📗 Heute abholen: {best.branch_name}",
                f"   {_describe_hours(best, plan.created_at)}",
                f"   {_holding_details(best)}",
            ]
        )
        if best.branch and best.branch.address:
            lines.append(f"   {best.branch.address}")
        alternatives = [option for option in today[1:] if option.branch_name != best.branch_name][:2]
        if alternatives:
            lines.append("\nWeitere Optionen heute:")
            for option in alternatives:
                lines.append(f"   • {option.branch_name} — {_describe_hours(option, plan.created_at)}")
    else:
        available = plan.available_options
        if available:
            best = available[0]
            lines.extend(
                [
                    "\n📕 Heute keine sichere Abholung.",
                    f"   Verfügbar bei {best.branch_name}, aber {_describe_hours(best, plan.created_at)}.",
                    f"   {_holding_details(best)}",
                ]
            )
        else:
            lines.append("\n📕 Heute kein verfügbares Exemplar gefunden.")
            due_options = []
            for option in plan.options:
                due = _due_date(option.holding.get("status", ""))
                if due:
                    due_options.append((option, due))
            if due_options:
                lines.append("Mögliche Rückgaben (nicht garantiert):")
                for option, due in due_options[:3]:
                    lines.append(f"   • {option.branch_name} — fällig am {due}")

    lines.extend(
        [
            "\nHinweis: Der Plan nutzt reguläre Öffnungszeiten; Feiertage und kurzfristige Schließungen können abweichen.",
            f"Öffnungszeiten: {plan.directory.source_url}",
        ]
    )
    return "\n".join(lines)
