from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import adis_search


class _FakeElement:
    def __init__(self, text: str = "", children: list["_FakeElement"] | None = None) -> None:
        self.text = text
        self.children = children or []

    def inner_text(self) -> str:
        return self.text

    def locator(self, _selector: str) -> "_FakeLocator":
        return _FakeLocator(self.children)


class _FakeLocator:
    def __init__(self, elements: list[_FakeElement]) -> None:
        self.elements = elements

    def count(self) -> int:
        return len(self.elements)

    def nth(self, index: int) -> _FakeElement:
        return self.elements[index]


class _FakePage:
    def __init__(self, selectors: dict[str, list[_FakeElement]]) -> None:
        self.selectors = selectors

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(self.selectors.get(selector, []))


class SearchMatchingTests(unittest.TestCase):
    def test_related_recommendation_does_not_match_circe(self) -> None:
        related = 'Klytämnestra : Roman - Für alle Leser*innen von Madeline Millers "Ich bin Circe" / Costanza Casati'
        self.assertEqual(adis_search._title_candidate_score(related, "Madeline Miller - Circe"), 0)
        self.assertEqual(adis_search._title_candidate_score(related, "Circe"), 0)

    def test_exact_title_and_author_match(self) -> None:
        self.assertGreater(
            adis_search._title_candidate_score("Circe / Madeline Miller", "Madeline Miller - Circe"),
            0,
        )
        self.assertGreater(
            adis_search._title_candidate_score("The Outsider / Stephen King", "Stephen King - The Outsider"),
            0,
        )

    def test_single_word_title_does_not_match_late_in_another_title(self) -> None:
        self.assertEqual(adis_search._title_candidate_score("Ich bin Circe", "Circe"), 0)
        self.assertGreater(adis_search._title_candidate_score("Circe : Roman", "Circe"), 0)

    def test_detail_title_prefers_catalog_field_over_generic_heading(self) -> None:
        page = _FakePage(
            {
                "table.gi tr": [
                    _FakeElement(children=[_FakeElement("Medienart"), _FakeElement("[Buch]")]),
                    _FakeElement(
                        children=[
                            _FakeElement("Titel"),
                            _FakeElement("Mexican Gothic / Silvia Moreno-Garcia"),
                        ]
                    ),
                ],
                "h2": [
                    _FakeElement("Bevorstehendes Sitzungsende!"),
                    _FakeElement("Aktuelle Seite:\nMexican Gothic / Silvia Moreno-Garcia"),
                ],
            }
        )
        self.assertEqual(
            adis_search._extract_detail_title(page),
            "Mexican Gothic / Silvia Moreno-Garcia",
        )

    def test_unverified_search_is_explicit_in_cli(self) -> None:
        stdout = io.StringIO()
        argv = ["adis_search.py", "Circe"]
        with (
            patch.object(adis_search, "search_duesseldorf", return_value=[]),
            patch.object(adis_search, "get_last_search_note", return_value="Titel nicht verifiziert: Kein bestätigter Haupttitel."),
            patch.object(sys, "argv", argv),
            redirect_stdout(stdout),
        ):
            adis_search.main()

        self.assertIn("Titel nicht verifiziert", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
