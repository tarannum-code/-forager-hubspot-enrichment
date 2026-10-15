from __future__ import annotations

import pytest

from app.enrichment.title_filter import TitleFilter

F = TitleFilter.parse("Chief,VP,Vice President,Head of,Director", "Intern,Assistant,Former")


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("VP of Engineering", True),
        ("SVP, Sales", False),  # "VP" is a whole word only
        ("MVP Program Lead", False),
        ("V.P. Marketing", True),
        ("Head of Product", True),
        ("Header Designer", False),
        ("Chief Technology Officer", True),
        ("Director, Research", True),
        ("Assistant Director", False),  # exclude wins
        ("Engineering Intern", False),
        ("Vice-President, Partnerships", True),
        ("Software Engineer", False),
        ("", False),
        (None, False),
    ],
)
def test_matches(title: str | None, expected: bool) -> None:
    assert F.matches(title) is expected


def test_forager_query_quotes_phrases() -> None:
    assert TitleFilter.parse("VP, Head of").to_forager_query() == '"VP" OR "Head of"'
    assert TitleFilter.parse("").to_forager_query() is None


def test_parse_dedupes_and_trims() -> None:
    f = TitleFilter.parse(" VP ,VP,, Head of ")
    assert f.include == ("VP", "Head of")


def test_rank_follows_user_priority() -> None:
    assert F.rank("Chief Executive Officer") < F.rank("VP Sales") < F.rank("Director of Ops")
    assert F.rank("Software Engineer") == len(F.include) + 1


def test_empty_include_accepts_anything_not_excluded() -> None:
    f = TitleFilter.parse("", "Intern")
    assert f.matches("Engineer") and not f.matches("Intern")
