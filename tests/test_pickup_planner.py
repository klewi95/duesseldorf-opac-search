from __future__ import annotations

import unittest
from datetime import datetime

from pickup_planner import BERLIN_TZ, build_pickup_plan, load_branch_directory, render_pickup_plan


def holding(branch: str, status: str = "verfügbar") -> dict[str, str]:
    return {
        "titel": "Beispieltitel",
        "status": status,
        "bibliothek": branch,
        "standort": "Romanregal",
        "signatur": "ABC 123",
        "bestellmoeglichkeit": "Standardleihfrist",
    }


def berlin_time(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=BERLIN_TZ)


class PickupPlannerTests(unittest.TestCase):
    def test_directory_recognises_opac_branch_names(self) -> None:
        directory = load_branch_directory()
        self.assertEqual(directory.find("Zentralbibliothek").address, "Konrad-Adenauer-Platz 1, 40210 Düsseldorf")
        self.assertEqual(directory.find("Stadtteilbücherei Benrath").name, "Bücherei Benrath")

    def test_preferred_open_branch_wins_for_today(self) -> None:
        plan = build_pickup_plan(
            "Beispieltitel",
            [holding("Zentralbibliothek"), holding("Bücherei Benrath")],
            now=berlin_time("2026-08-10T16:30"),  # Monday
            preferred_branches=["Bücherei Benrath", "Zentralbibliothek"],
        )

        self.assertTrue(plan.today_options)
        self.assertEqual(plan.recommendation.branch_name, "Bücherei Benrath")
        self.assertTrue(plan.recommendation.can_pick_up_today)
        self.assertIn("Heute abholen: Bücherei Benrath", render_pickup_plan(plan))

    def test_closed_branch_is_not_presented_as_a_today_pickup(self) -> None:
        plan = build_pickup_plan(
            "Beispieltitel",
            [holding("Bücherei Benrath")],
            now=berlin_time("2026-08-11T15:00"),  # Tuesday: Benrath is closed
        )

        self.assertFalse(plan.today_options)
        self.assertTrue(plan.recommendation.available)
        self.assertEqual(plan.recommendation.hours.state, "closed")
        rendered = render_pickup_plan(plan)
        self.assertIn("Heute keine sichere Abholung", rendered)
        self.assertIn("öffnet wieder", rendered)

    def test_self_service_is_communicated(self) -> None:
        plan = build_pickup_plan(
            "Beispieltitel",
            [holding("Bücherei Bilk")],
            now=berlin_time("2026-08-15T14:00"),  # Saturday self-service
        )

        self.assertTrue(plan.recommendation.can_pick_up_today)
        self.assertEqual(plan.recommendation.hours.access, "self_service")
        self.assertIn("Selbstbedienung", render_pickup_plan(plan))

    def test_due_date_is_a_fallback_not_a_promise(self) -> None:
        plan = build_pickup_plan(
            "Beispieltitel",
            [holding("Zentralbibliothek", "Ausgeliehen - Fällig am: 24.8.2026")],
            now=berlin_time("2026-08-10T16:30"),
        )

        self.assertFalse(plan.available_options)
        rendered = render_pickup_plan(plan)
        self.assertIn("nicht garantiert", rendered)
        self.assertIn("24.8.2026", rendered)

    def test_unknown_branch_never_claims_a_safe_pickup(self) -> None:
        plan = build_pickup_plan(
            "Beispieltitel",
            [holding("Mobile Bücherinsel")],
            now=berlin_time("2026-08-10T16:30"),
        )

        self.assertTrue(plan.recommendation.available)
        self.assertFalse(plan.recommendation.can_pick_up_today)
        self.assertEqual(plan.recommendation.hours.state, "unknown")


if __name__ == "__main__":
    unittest.main()
