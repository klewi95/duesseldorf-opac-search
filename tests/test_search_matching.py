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

    def test_related_corpus_delicti_titles_do_not_match_the_novel(self) -> None:
        self.assertEqual(adis_search._title_candidate_score("Fragen zu Corpus Delicti", "Corpus Delicti"), 0)
        self.assertEqual(adis_search._title_candidate_score("Juli Zeh, Corpus Delicti", "Corpus Delicti"), 0)
        self.assertGreater(adis_search._title_candidate_score("Corpus Delicti : ein Prozess", "Corpus Delicti"), 0)

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

    def test_german_translation_is_verified_by_original_title_and_author(self) -> None:
        page = _FakePage(
            {
                "table.gi tr": [
                    _FakeElement(children=[_FakeElement("Medienart"), _FakeElement("[Band]")]),
                    _FakeElement(children=[_FakeElement("Titel"), _FakeElement("Chroniken des Wahns - Blutwerk : Roman / Michael R. Fletcher")]),
                    _FakeElement(children=[_FakeElement("Verfasser"), _FakeElement("Fletcher, Michael R.")]),
                    _FakeElement(children=[_FakeElement("Bevorzugter Titel"), _FakeElement("Beyond redemption")]),
                    _FakeElement(children=[_FakeElement("Sprache"), _FakeElement("Deutsch")]),
                    _FakeElement(children=[_FakeElement("Sprache Original"), _FakeElement("Englisch")]),
                ]
            }
        )

        match = adis_search._classify_detail_match(page, "Beyond Redemption - Michael R. Fletcher")

        self.assertIsNotNone(match)
        self.assertEqual(match["trefferart"], "deutsche_uebersetzung")
        self.assertEqual(match["originaltitel"], "Beyond redemption")
        self.assertEqual(match["sprache"], "Deutsch")

    def test_translation_with_wrong_author_is_rejected(self) -> None:
        page = _FakePage(
            {
                "table.gi tr": [
                    _FakeElement(children=[_FakeElement("Medienart"), _FakeElement("[Band]")]),
                    _FakeElement(children=[_FakeElement("Titel"), _FakeElement("Blutwerk")]),
                    _FakeElement(children=[_FakeElement("Verfasser"), _FakeElement("Fletcher, Michael R.")]),
                    _FakeElement(children=[_FakeElement("Bevorzugter Titel"), _FakeElement("Beyond redemption")]),
                    _FakeElement(children=[_FakeElement("Sprache"), _FakeElement("Deutsch")]),
                ]
            }
        )

        self.assertIsNone(adis_search._classify_detail_match(page, "Beyond Redemption - Someone Else"))

    def test_missing_verfasser_uses_primary_title_responsibility(self) -> None:
        page = _FakePage(
            {
                "table.gi tr": [
                    _FakeElement(children=[_FakeElement("Medienart"), _FakeElement("[Buch]")]),
                    _FakeElement(
                        children=[
                            _FakeElement("Titel"),
                            _FakeElement("Am kürzeren Ende der Sonnenallee / Thomas Brussig"),
                        ]
                    ),
                    _FakeElement(children=[_FakeElement("Sprache"), _FakeElement("Deutsch")]),
                ]
            }
        )

        match = adis_search._classify_detail_match(
            page,
            "Am kürzeren Ende der Sonnenallee - Thomas Brussig",
        )

        self.assertIsNotNone(match)
        self.assertEqual(match["autor_match"], "title_responsibility")
        self.assertEqual(match["verifizierter_autor"], "Thomas Brussig")

    def test_conflicting_structured_author_blocks_title_fallback(self) -> None:
        page = _FakePage(
            {
                "table.gi tr": [
                    _FakeElement(children=[_FakeElement("Medienart"), _FakeElement("[Buch]")]),
                    _FakeElement(children=[_FakeElement("Titel"), _FakeElement("Corpus Delicti / Juli Zeh")]),
                    _FakeElement(children=[_FakeElement("Verfasser"), _FakeElement("Leis, Mario")]),
                ]
            }
        )

        self.assertIsNone(adis_search._classify_detail_match(page, "Corpus Delicti - Juli Zeh"))

    def test_translator_responsibility_is_not_treated_as_author(self) -> None:
        self.assertIsNone(
            adis_search._responsibility_author("Von Mäusen und Menschen / Deutsch von Mirjam Pressler")
        )

    def test_person_roles_keep_author_separate_from_translator(self) -> None:
        metadata = {
            "person": [
                "Steinbeck, John [Verfasser/in] Pressler, Mirjam [Übersetzer/in]"
            ]
        }

        self.assertEqual(
            adis_search._verify_author(metadata, "Von Mäusen und Menschen", "John Steinbeck"),
            ("person_role", "Steinbeck, John"),
        )
        self.assertIsNone(
            adis_search._verify_author(metadata, "Von Mäusen und Menschen", "Mirjam Pressler")
        )

    def test_verified_alias_connects_english_title_to_german_translation(self) -> None:
        page = _FakePage(
            {
                "table.gi tr": [
                    _FakeElement(children=[_FakeElement("Medienart"), _FakeElement("[Buch]")]),
                    _FakeElement(
                        children=[
                            _FakeElement("Titel"),
                            _FakeElement("Von Mäusen und Menschen : Roman / John Steinbeck ; Deutsch von Mirjam Pressler"),
                        ]
                    ),
                    _FakeElement(children=[_FakeElement("Sprache"), _FakeElement("Deutsch")]),
                    _FakeElement(children=[_FakeElement("Sprache Original"), _FakeElement("Englisch")]),
                ]
            }
        )

        match = adis_search._classify_detail_match(page, "Of Mice and Men - John Steinbeck")

        self.assertIsNotNone(match)
        self.assertEqual(match["trefferart"], "deutsche_uebersetzung")
        self.assertEqual(match["originaltitel"], "Of Mice and Men")
        self.assertEqual(match["uebersetzungs_match"], "verified_alias")

    def test_alias_does_not_accept_an_unrelated_book_by_same_author(self) -> None:
        page = _FakePage(
            {
                "table.gi tr": [
                    _FakeElement(children=[_FakeElement("Medienart"), _FakeElement("[Buch]")]),
                    _FakeElement(children=[_FakeElement("Titel"), _FakeElement("Früchte des Zorns / John Steinbeck")]),
                    _FakeElement(children=[_FakeElement("Sprache"), _FakeElement("Deutsch")]),
                ]
            }
        )

        self.assertIsNone(adis_search._classify_detail_match(page, "Of Mice and Men - John Steinbeck"))

    def test_print_book_and_ebook_are_accepted_media(self) -> None:
        print_metadata = {"medienart": ["[Band]"]}
        ebook_metadata = {"medienart": ["E-Book"]}

        self.assertEqual(
            adis_search._classify_book_media(print_metadata, "22 Bahnen : Roman"),
            ("buch", "[Band]"),
        )
        self.assertEqual(
            adis_search._classify_book_media(ebook_metadata, "22 Bahnen : Roman"),
            ("e_book", "E-Book"),
        )

    def test_audio_video_and_unknown_media_are_rejected(self) -> None:
        self.assertIsNone(
            adis_search._classify_book_media({"medienart": ["Tonträger"]}, "Unter der Drachenwand")
        )
        self.assertIsNone(
            adis_search._classify_book_media({"medienart": ["Film"]}, "22 Bahnen <2025>")
        )
        self.assertIsNone(adis_search._classify_book_media({}, "Of Mice and Men"))

    def test_dvd_record_cannot_be_verified_as_the_novel(self) -> None:
        page = _FakePage(
            {
                "table.gi tr": [
                    _FakeElement(children=[_FakeElement("Medienart"), _FakeElement("[Film]")]),
                    _FakeElement(children=[_FakeElement("Titel"), _FakeElement("22 Bahnen <2025> / Regie: Mia Maariel Meyer")]),
                    _FakeElement(children=[_FakeElement("Verfasser"), _FakeElement("Wahl, Caroline")]),
                ]
            }
        )

        self.assertIsNone(adis_search._classify_detail_match(page, "22 Bahnen - Caroline Wahl"))

    def test_validation_preserves_translation_metadata(self) -> None:
        result = {
            "titel": "Blutwerk",
            "status": "verfügbar",
            "bibliothek": "Zentralbibliothek",
            "standort": "Roman",
            "signatur": "Flet",
            "bestellmoeglichkeit": "",
            "trefferart": "deutsche_uebersetzung",
            "originaltitel": "Beyond redemption",
            "sprache": "Deutsch",
            "originalsprache": "Englisch",
        }

        validated = adis_search.validate_results([result])

        self.assertEqual(validated[0]["trefferart"], "deutsche_uebersetzung")
        self.assertEqual(validated[0]["originaltitel"], "Beyond redemption")

    def test_available_original_wins_across_multiple_editions(self) -> None:
        borrowed = {
            "titel": "Corpus Delicti : ein Prozess / Juli Zeh",
            "ausgabe": "Taschenbuch-Sonderausgabe",
            "status": "Ausgeliehen - Fällig am: 27.8.2026",
        }
        available = {
            "titel": "Corpus Delicti : ein Prozess / Juli Zeh",
            "ausgabe": "1. Auflage",
            "status": "verfügbar",
        }
        translated = {
            "titel": "The Method / Juli Zeh",
            "status": "verfügbar",
        }

        selected = adis_search._select_edition_results([borrowed, available], [translated])

        self.assertEqual(selected, [borrowed, available])

    def test_available_translation_wins_when_all_original_editions_are_borrowed(self) -> None:
        borrowed = [{"titel": "Original", "status": "entliehen"}]
        translated = [{"titel": "Übersetzung", "status": "verfügbar"}]

        self.assertEqual(adis_search._select_edition_results(borrowed, translated), translated)

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
