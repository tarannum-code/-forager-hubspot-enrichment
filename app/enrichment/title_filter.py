"""Job-title filter used to control which people contact discovery surfaces.

Two layers, so the filter works even if Forager's text search is looser than expected:

1. Server side: the include keywords become a boolean `role_title` query
   (`"VP" OR "Head of"`), so we only pay for pages of relevant people.
2. Client side: every result is re-checked locally with whole-word matching, and the
   exclude keywords are applied. "VP" matches "VP of Sales" but not "MVP Lead";
   "Head of" never matches "Header". This makes the filter deterministic.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

_NON_ALNUM = re.compile(r"[^a-z0-9&]+")


def _normalize(text: str) -> str:
    """Lowercase, '&' kept, everything else collapsed to single spaces, padded."""
    lowered = text.lower().replace("v.p.", "vp").replace("vice-president", "vice president")
    return f" {_NON_ALNUM.sub(' ', lowered).strip()} "


def _dedupe(items: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item.strip() for item in items if item.strip()))


@dataclass(frozen=True)
class TitleFilter:
    include: tuple[str, ...]
    exclude: tuple[str, ...] = ()

    @classmethod
    def from_lists(cls, include: Iterable[str], exclude: Iterable[str] = ()) -> TitleFilter:
        return cls(include=_dedupe(include), exclude=_dedupe(exclude))

    @classmethod
    def parse(cls, include_csv: str | None, exclude_csv: str | None = None) -> TitleFilter:
        return cls.from_lists((include_csv or "").split(","), (exclude_csv or "").split(","))

    def to_forager_query(self) -> str | None:
        """Boolean query for Forager's `role_title` field. Quotes keep phrases intact."""
        if not self.include:
            return None
        terms = [f'"{term.replace(chr(34), "")}"' for term in self.include]
        return " OR ".join(terms)

    def matching_keyword(self, title: str | None) -> str | None:
        """Return the include keyword that matched, or None if the title is rejected."""
        if not title:
            return None
        normalized = _normalize(title)
        if any(_normalize(word) in normalized for word in self.exclude):
            return None
        if not self.include:
            return title
        for word in self.include:
            if _normalize(word) in normalized:
                return word
        return None

    def matches(self, title: str | None) -> bool:
        return self.matching_keyword(title) is not None

    def rank(self, title: str | None) -> int:
        """Lower is better: position of the first matching include keyword.

        Lets callers order candidates by the priority the user typed (e.g. Chief before VP).
        """
        keyword = self.matching_keyword(title)
        if keyword is None:
            return len(self.include) + 1
        return self.include.index(keyword) if keyword in self.include else 0
