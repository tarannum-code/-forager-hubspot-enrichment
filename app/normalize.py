"""Pure data-cleaning helpers. Nothing here performs I/O, so everything is unit-testable.

The goal is HubSpot records that look hand-curated: proper name casing, consistent phone
format, canonical LinkedIn URLs, bare domains, and no placeholder junk like "N/A".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlparse

import phonenumbers

_JUNK = {"", "null", "none", "n/a", "na", "-", "--", "unknown", "undefined", "nan", "0"}
_WS = re.compile(r"\s+")
_EMAIL = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def clean_text(value: Any, *, max_length: int | None = None) -> str | None:
    """Trim/collapse whitespace and drop placeholder values. Returns None for junk."""
    if value is None:
        return None
    text = _WS.sub(" ", str(value)).strip()
    if text.lower() in _JUNK:
        return None
    if max_length and len(text) > max_length:
        text = text[: max_length - 1].rstrip() + "…"
    return text


# --- Names ------------------------------------------------------------------------

# Whitespace, hyphen, straight and typographic apostrophe (U+2019).
_NAME_SEPARATORS = re.compile(r"([\s\-'" + chr(0x2019) + "])")
_LOWER_PARTICLES = {"van", "von", "de", "da", "del", "della", "der", "di", "du", "la", "le", "bin"}
_UPPER_TOKENS = {"ii", "iii", "iv", "vi", "vii", "viii", "phd", "md", "mba", "cpa"}


def _case_token(token: str, *, first: bool) -> str:
    lower = token.lower()
    if lower in _UPPER_TOKENS:
        return token.upper()
    if not first and lower in _LOWER_PARTICLES:
        return lower
    if lower.startswith("mc") and len(lower) > 2:
        return "Mc" + lower[2].upper() + lower[3:]
    return lower[:1].upper() + lower[1:]


def smart_name_case(name: Any) -> str | None:
    """Fix ALL-CAPS / all-lowercase names; leave intentional mixed case (DeAndre, McKay) alone."""
    text = clean_text(name)
    if not text:
        return None
    parts = _NAME_SEPARATORS.split(text)
    out: list[str] = []
    word_index = 0
    for part in parts:
        if not part or _NAME_SEPARATORS.fullmatch(part):
            out.append(part)
            continue
        is_intentional_mixed = any(c.isupper() for c in part[1:]) and any(c.islower() for c in part)
        out.append(part if is_intentional_mixed else _case_token(part, first=word_index == 0))
        word_index += 1
    return "".join(out)


def split_full_name(full_name: Any) -> tuple[str | None, str | None]:
    text = clean_text(full_name)
    if not text:
        return None, None
    first, _, last = text.partition(" ")
    return smart_name_case(first), smart_name_case(last) if last else None


# --- Job titles ---------------------------------------------------------------------

_TITLE_ACRONYMS = {
    "ceo", "cto", "cfo", "coo", "cmo", "cro", "cpo", "ciso", "cio", "vp", "svp", "evp", "avp",
    "ai", "ml", "gtm", "hr", "it", "r&d", "ui", "ux", "api", "b2b", "saas", "pr", "qa", "emea",
    "apac", "amer", "us", "uk",
}


def clean_job_title(title: Any) -> str | None:
    text = clean_text(title, max_length=250)
    if not text:
        return None
    if not (text.isupper() or text.islower()):
        return text
    words = []
    for word in text.split(" "):
        lower = word.lower()
        words.append(word.upper() if lower.strip(",.") in _TITLE_ACRONYMS else lower.capitalize())
    return " ".join(words)


# --- Email ----------------------------------------------------------------------------


def normalize_email(value: Any) -> str | None:
    text = clean_text(value)
    if not text:
        return None
    match = _EMAIL.search(text)
    return match.group(0).lower() if match else None


def email_domain(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    return normalize_domain(email.rsplit("@", 1)[1])


FREE_EMAIL_DOMAINS = frozenset(
    {
        "gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com", "live.com",
        "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com", "gmx.com", "msn.com",
    }
)


# --- Domains & URLs -------------------------------------------------------------------


def normalize_domain(value: Any) -> str | None:
    """'https://www.OpenAI.com/about' -> 'openai.com'."""
    text = clean_text(value)
    if not text:
        return None
    if "://" not in text:
        text = "http://" + text
    host = (urlparse(text).hostname or "").lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    return host if "." in host else None


def website_url(domain: str | None) -> str | None:
    return f"https://{domain}" if domain else None


def linkedin_slug(value: Any, kind: str = "in") -> str | None:
    """Extract the public identifier from a LinkedIn URL ('in' = person, 'company')."""
    text = clean_text(value)
    if not text:
        return None
    if "linkedin.com" not in text.lower():
        # Already a bare slug?
        return text.strip("/") if re.fullmatch(r"[\w\-%.]+/?", text) else None
    path = urlparse(text if "://" in text else "https://" + text).path
    segments = [s for s in path.split("/") if s]
    for i, segment in enumerate(segments[:-1]):
        if segment.lower() == kind:
            return unquote(segments[i + 1])
    return None


def linkedin_person_url(slug: str | None) -> str | None:
    return f"https://www.linkedin.com/in/{slug}" if slug else None


def linkedin_company_url(slug: str | None) -> str | None:
    return f"https://www.linkedin.com/company/{slug}" if slug else None


# --- Location ---------------------------------------------------------------------------

_COUNTRY_ALIASES = {
    "us": "United States", "usa": "United States", "u.s.": "United States",
    "united states of america": "United States", "uk": "United Kingdom",
    "great britain": "United Kingdom", "uae": "United Arab Emirates",
}
_COUNTRY_TO_REGION = {
    "united states": "US", "canada": "CA", "united kingdom": "GB", "ireland": "IE",
    "india": "IN", "germany": "DE", "france": "FR", "spain": "ES", "italy": "IT",
    "netherlands": "NL", "australia": "AU", "singapore": "SG", "japan": "JP", "brazil": "BR",
    "mexico": "MX", "israel": "IL", "switzerland": "CH", "sweden": "SE", "pakistan": "PK",
    "united arab emirates": "AE", "south korea": "KR", "poland": "PL",
}
US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee",
    "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}
_US_STATE_NAMES = {name.lower() for name in US_STATES.values()}
# LinkedIn often reports metro areas instead of cities.
_METRO_AREAS = {
    "san francisco bay area": ("San Francisco", "California", "United States"),
    "greater seattle area": ("Seattle", "Washington", "United States"),
    "new york city metropolitan area": ("New York", "New York", "United States"),
    "greater boston": ("Boston", "Massachusetts", "United States"),
    "los angeles metropolitan area": ("Los Angeles", "California", "United States"),
    "greater chicago area": ("Chicago", "Illinois", "United States"),
    "washington dc-baltimore area": ("Washington", "District of Columbia", "United States"),
    "greater london": ("London", None, "United Kingdom"),
    "london area, united kingdom": ("London", None, "United Kingdom"),
}
_CITY_TYPES = {"city", "town", "village", "municipality", "hamlet", "locality", "borough"}
_STATE_TYPES = {"state", "region", "province", "county_state", "administrative_area"}


@dataclass(frozen=True)
class Location:
    city: str | None = None
    state: str | None = None
    country: str | None = None

    @property
    def is_empty(self) -> bool:
        return not (self.city or self.state or self.country)


def normalize_country(value: Any) -> str | None:
    text = clean_text(value)
    if not text:
        return None
    return _COUNTRY_ALIASES.get(text.lower(), text)


def _normalize_state(value: str | None, country: str | None) -> str | None:
    text = clean_text(value)
    if text and country == "United States" and text.upper() in US_STATES:
        return US_STATES[text.upper()]
    return text


def parse_location(location: Any) -> Location:
    """Turn a Forager `InnerLocation` ({name, osm_locations[{name, place_type}]}) into parts.

    Prefers the structured OSM hierarchy; falls back to the comma-separated display name.
    """
    if location is None:
        return Location()
    if hasattr(location, "model_dump"):
        location = location.model_dump()
    if isinstance(location, str):
        location = {"name": location}

    city = state = country = None
    for osm in location.get("osm_locations") or []:
        place_type = str(osm.get("place_type") or "").lower()
        name = clean_text(osm.get("name"))
        if not name:
            continue
        if place_type == "country":
            country = country or name
        elif place_type in _STATE_TYPES:
            state = state or name
        elif place_type in _CITY_TYPES:
            city = city or name

    display = clean_text(location.get("name"))
    if display and not (city and country):
        metro = _METRO_AREAS.get(display.lower())
        if metro:
            city, state, country = (city or metro[0]), (state or metro[1]), (country or metro[2])
        else:
            parts = [p.strip() for p in display.split(",") if p.strip()]
            if len(parts) >= 3:
                city, state, country = city or parts[0], state or parts[-2], country or parts[-1]
            elif len(parts) == 2:
                first, last = parts
                country = country or last
                if first.lower() in _US_STATE_NAMES or first.upper() in US_STATES:
                    state = state or first
                else:
                    metro = _METRO_AREAS.get(first.lower())
                    city = city or (metro[0] if metro else first)
            elif len(parts) == 1 and not country:
                country = parts[0]

    country = normalize_country(country)
    return Location(
        city=clean_text(city), state=_normalize_state(state, country), country=country
    )


# --- Phones -------------------------------------------------------------------------------


def normalize_phone(raw: Any, country: str | None = None) -> str | None:
    """Return a valid phone in international format (e.g. '+1 415-555-2671'), else None."""
    text = clean_text(raw)
    if not text:
        return None
    region = _COUNTRY_TO_REGION.get((country or "").lower(), "US")
    try:
        parsed = phonenumbers.parse(text, None if text.startswith("+") else region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
