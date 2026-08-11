from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import adis_search


SAMPLE_HOLDING = {
    "titel": "Sapiens",
    "status": "verfügbar",
    "bibliothek": "Zentralbibliothek",
    "standort": "Gesellschaft",
    "signatur": "Gcl 1 Huber",
    "bestellmoeglichkeit": "Standardleihfrist",
}


class PickupPlanCliTests(unittest.TestCase):
    def test_json_mode_embeds_the_pickup_decision(self) -> None:
        stdout = io.StringIO()
        argv = [
            "adis_search.py",
            "--plan-pickup",
            "--json",
            "--at",
            "2026-08-10T16:30",
            "Sapiens",
        ]
        with patch.object(adis_search, "search_duesseldorf", return_value=[SAMPLE_HOLDING]), patch.object(sys, "argv", argv), redirect_stdout(stdout):
            adis_search.main()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["query"], "Sapiens")
        self.assertTrue(payload["pickup_plan"]["can_pick_up_today"])
        self.assertEqual(payload["pickup_plan"]["recommendation"]["bibliothek"], "Zentralbibliothek")


if __name__ == "__main__":
    unittest.main()
