"""
Cross-platform event matcher — v3 precision-focused.

Key design changes:
  - Uses token_sort_ratio (order-independent full comparison) instead of
    token_set_ratio (which gives 100% if one title is a subset of another)
  - Requires shared anchor entities AND penalises year/timeframe mismatches
  - Option-order inversion detection (e.g., "X or Y" vs "Y or X")
  - Gap-confidence gate: large price gaps need proportionally higher confidence
  - Higher review threshold (65%) eliminates most false matches
  - Detects and penalises "same subject, different question" patterns
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set, Tuple

try:
    from rapidfuzz import fuzz
except ImportError:  # small, deterministic fallback for minimal installs
    from difflib import SequenceMatcher

    class _FallbackFuzz:
        @staticmethod
        def ratio(left: str, right: str) -> float:
            return SequenceMatcher(None, left, right).ratio() * 100.0

        @staticmethod
        def token_sort_ratio(left: str, right: str) -> float:
            left_sorted = " ".join(sorted(left.split()))
            right_sorted = " ".join(sorted(right.split()))
            return SequenceMatcher(None, left_sorted, right_sorted).ratio() * 100.0

    fuzz = _FallbackFuzz()

from .models import MatchedPair, NormalizedMarket

log = logging.getLogger(__name__)

# ── Stop words ────────────────────────────────────────────
_STOP_WORDS = frozenset({
    "will", "the", "a", "an", "be", "in", "on", "at", "to", "of", "for",
    "by", "is", "it", "its", "this", "that", "or", "and", "not", "no",
    "yes", "if", "do", "does", "than", "more", "less", "above", "below",
    "before", "after", "between", "what", "which", "who", "whom", "how",
    "when", "where", "there", "here", "with", "from", "into", "have",
    "has", "had", "was", "were", "are", "been", "being", "market",
    "contract", "contracts", "event", "prediction", "bet",
})

# Generic words: common in market titles but don't identify a specific event
_GENERIC_WORDS = frozenset({
    "win", "won", "lose", "lost", "first", "last", "next", "new",
    "team", "game", "match", "season", "year", "price", "rate",
    "run", "running", "announce", "announced", "election",
    "presidential", "president", "championship", "champion",
    "party", "republican", "democrat", "democratic", "nominee",
    "nomination", "primary", "vote", "united", "states", "national",
    "league", "major", "tour", "cup", "super", "bowl", "world",
    "official", "officially", "report", "reported",
})

# ── Regex patterns ────────────────────────────────────────
_YEAR_PATTERN = re.compile(r'\b(20\d{2})\b')
_NUMBER_PATTERN = re.compile(r'\b\d+(?:\.\d+)?%?\b')
_PROPER_NOUN_PATTERN = re.compile(r'\b[A-Z][a-z]{2,}\b')
_OR_PATTERN = re.compile(r'\b([A-Za-z0-9]+)\s+or\s+([A-Za-z0-9]+)\b', re.I)
_MONTH_PATTERN = re.compile(
    r"\b(january|february|march|april|may|june|july|august|september|october|november|december|"
    r"jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\b",
    re.I,
)

_LOCATIONS = frozenset({
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
    "maine", "maryland", "massachusetts", "michigan", "minnesota",
    "mississippi", "missouri", "montana", "nebraska", "nevada",
    "hampshire", "jersey", "mexico", "york", "carolina", "dakota",
    "ohio", "oklahoma", "oregon", "pennsylvania", "rhode", "tennessee",
    "texas", "utah", "vermont", "virginia", "washington", "wisconsin",
    "wyoming", "israel", "ethiopia", "romania", "brazil", "france",
    "germany", "italy", "spain", "turkey", "hungary", "switzerland",
    "ukraine", "russia", "canada", "china", "taiwan", "greenland",
})

_EVENT_TYPES = [
    ("presidential_declaration", re.compile(r"\bdeclare for .*presidential|\bdeclare .*presidential", re.I)),
    ("presidential_election_win", re.compile(r"\bwin .*presidential election\b", re.I)),
    ("award_supporting_actress", re.compile(r"\bbest supporting actress\b", re.I)),
    ("award_supporting_actor", re.compile(r"\bbest supporting actor\b", re.I)),
    ("award_actress", re.compile(r"\bbest actress\b", re.I)),
    ("award_actor", re.compile(r"\bbest actor\b", re.I)),
    ("award_picture", re.compile(r"\bbest picture\b", re.I)),
    ("next_prime_minister", re.compile(r"\bnext prime minister\b", re.I)),
    ("next_cabinet_departure", re.compile(r"\bnext(?: person)? to leave .*cabinet\b|\bnext person to leave .*cabinet\b", re.I)),
    ("meeting_location", re.compile(r"\bmeet next\b|\bnext meet\b", re.I)),
    ("endorsement", re.compile(r"\bendorse\b", re.I)),
    ("runoff_qualification", re.compile(r"\bqualif\w* for (?:the )?runoff\b", re.I)),
    ("first_round_second", re.compile(r"\bsecond place in (?:the )?first round\b", re.I)),
    ("senate_margin", re.compile(r"\bsenate race.*within\b", re.I)),
    ("senate_winner", re.compile(r"\bwin .*senate race\b", re.I)),
    ("house_seat_count", re.compile(r"\bwin exactly \d+ seats\b", re.I)),
    ("map_change", re.compile(r"\bnew congressional map\b", re.I)),
    ("fed_rate_at_date", re.compile(r"\bfederal funds rate in effect\b|\bfed funds rate at\b", re.I)),
    ("fed_rate_reach", re.compile(r"\bfed.+\breach\b", re.I)),
    ("arrest", re.compile(r"\barrested?\b", re.I)),
    ("pardon", re.compile(r"\bpardon(?:ed)?\b", re.I)),
    ("ipo_first", re.compile(r"\bipo first\b", re.I)),
]

_SUBJECT_PATTERN = re.compile(
    r"^\s*will\s+(.+?)\s+(?:be|become|win|leave|meet|reach|finish|qualif\w*|"
    r"endorse|resign|retire|receive|get|have|announce|run)\b",
    re.I,
)
_ENDORSE_OBJECT_PATTERN = re.compile(
    r"\bendorse\s+(.+?)(?:\s+in\s+the|\s+before|\s+for\s+the|\?|$)",
    re.I,
)

# Phrase-level synonyms: applied during normalisation to canonicalise
# different phrasings of the same concept
_PHRASE_SYNONYMS = [
    (re.compile(r'\boscars\b', re.I), 'academy awards'),
    (re.compile(r'\bacademy awards\b', re.I), 'academy awards'),
    (re.compile(r'\b99th academy awards\b', re.I), 'academy awards'),
    (re.compile(r'\bnext person to leave\b', re.I), 'next to leave'),
    (re.compile(r'\bbe the nominee for the presidency for\b', re.I), 'win presidential nomination'),
    (re.compile(r'\bbe the democratic presidential nominee\b', re.I), 'win democratic presidential nomination'),
    (re.compile(r'\bbe the nominee for the vice presidency for\b', re.I), 'win vice presidential nomination'),
    (re.compile(r'\bwin the \d{4} \w+ presidential nomination\b', re.I), 'win presidential nomination'),
    (re.compile(r'\bannounce a presidential run\b', re.I), 'run for president'),
    (re.compile(r'\brun for president of the united states\b', re.I), 'run for president'),
    (re.compile(r'\bannounce a run for president\b', re.I), 'run for president'),
]

# Event-type discriminators: if one title has one of these and the other doesn't,
# they're almost certainly different events
_EVENT_DISCRIMINATORS = [
    re.compile(r'\bipo\b', re.I),
    re.compile(r'\bpardon\b', re.I),
    re.compile(r'\barrest\b', re.I),
    re.compile(r'\bresign\b', re.I),
    re.compile(r'\bretire\b', re.I),
    re.compile(r'\bdie\b|\bdeath\b|\bdead\b', re.I),
    re.compile(r'\breleas\w*\b', re.I),
    re.compile(r'\brunoff\b|\bfirst round\b', re.I),
    re.compile(r'\bqualif\w*\b', re.I),
    re.compile(r'\bnfl\b', re.I),
    re.compile(r'\bmlb\b', re.I),
    re.compile(r'\bnba\b', re.I),
    re.compile(r'\bcfp\b', re.I),
    re.compile(r'\bgolf\b|\bpga\b|\bmajor championship\b', re.I),
    re.compile(r'\b3m open\b|\bus open\b|\bmasters\b|\bopen championship\b', re.I),
    re.compile(r'\bafc\b|\bnfc\b', re.I),
    re.compile(r'\bsenate\b', re.I),
    re.compile(r'\bgovernor\b', re.I),
    re.compile(r'\bfed\w*\s+reserve\b|\bfed\s+(cut|hike|increase|decrease)\b', re.I),
    re.compile(r'\bticket\b|\b\w+\s+and\s+\w+\b', re.I),
    re.compile(r'\bvice presidency\b|\bvice-presidential\b|\bvp nominee\b', re.I),
    re.compile(r'\bpresidential nomination\b|\bpresidential election\b|\bpresidency\b', re.I),
]


def normalise_title(title: str) -> str:
    """Normalise a market title for fuzzy matching.

    Applies synonym substitution so that semantically identical
    but differently phrased titles produce closer fuzzy scores.
    """
    t = title.lower().strip()
    t = t.replace("**", "")
    # Apply phrase-level synonym substitution BEFORE tokenisation
    for pattern, replacement in _PHRASE_SYNONYMS:
        t = pattern.sub(replacement, t)
    t = re.sub(r"[^\w\s\-]", " ", t)
    tokens = [w for w in t.split() if w not in _STOP_WORDS]
    return " ".join(tokens)


def _tokenize(normalised: str) -> Set[str]:
    return set(normalised.split()) if normalised else set()


def _extract_years(title: str) -> Set[str]:
    """Extract all 4-digit years from a title."""
    return set(_YEAR_PATTERN.findall(title))


def _extract_anchor_tokens(title: str) -> Set[str]:
    """
    Extract "anchor" tokens — specific proper nouns and distinctive terms
    that uniquely identify an event subject.
    """
    anchors = set()
    proper_nouns = _PROPER_NOUN_PATTERN.findall(title)
    for pn in proper_nouns:
        lower = pn.lower()
        if lower not in _STOP_WORDS and lower not in _GENERIC_WORDS and len(lower) >= 3:
            anchors.add(lower)
    normalised = normalise_title(title)
    for token in normalised.split():
        if len(token) >= 5 and token not in _GENERIC_WORDS and token not in _STOP_WORDS:
            anchors.add(token)
    return anchors


def _check_discriminator_conflict(title_a: str, title_b: str) -> float:
    """
    Check if titles contain conflicting event-type discriminators.
    """
    penalty = 0.0
    for pattern in _EVENT_DISCRIMINATORS:
        a_has = bool(pattern.search(title_a))
        b_has = bool(pattern.search(title_b))
        if a_has != b_has:
            penalty -= 10.0  # each mismatch is a red flag
    return max(penalty, -30.0)  # cap penalty


def _check_option_inversion(title_a: str, title_b: str) -> bool:
    """
    Check if titles present two choices in reverse order.
    E.g., 'Will OpenAI or Anthropic IPO first?' vs 'Will Anthropic or OpenAI IPO first?'
    """
    m_a = _OR_PATTERN.search(title_a)
    m_b = _OR_PATTERN.search(title_b)
    if m_a and m_b:
        a1, a2 = m_a.group(1).lower(), m_a.group(2).lower()
        b1, b2 = m_b.group(1).lower(), m_b.group(2).lower()
        if a1 == b2 and a2 == b1 and a1 != a2:
            return True
    return False


def _date_proximity_score(dt1: Optional[datetime], dt2: Optional[datetime]) -> float:
    if dt1 is None or dt2 is None:
        return 40.0
    if dt1.tzinfo is None:
        dt1 = dt1.replace(tzinfo=timezone.utc)
    if dt2.tzinfo is None:
        dt2 = dt2.replace(tzinfo=timezone.utc)
    delta = abs((dt1 - dt2).total_seconds())
    hours = delta / 3600
    if hours < 6:
        return 100.0
    elif hours < 24:
        return 90.0
    elif hours < 48:
        return 75.0
    elif hours < 168:
        return 50.0
    elif hours < 720:
        return 25.0
    else:
        return 0.0


def _event_type(title: str) -> Optional[str]:
    for name, pattern in _EVENT_TYPES:
        if pattern.search(title):
            return name
    return None


def _subject(title: str) -> Optional[str]:
    match = _SUBJECT_PATTERN.search(title)
    if not match:
        return None
    value = normalise_title(match.group(1))
    return value or None


def _locations(title: str) -> Set[str]:
    tokens = set(re.findall(r"[a-z]+", title.lower()))
    return tokens & _LOCATIONS


def _non_year_numbers(title: str) -> Set[str]:
    canonical = normalise_title(title)
    return {value for value in _NUMBER_PATTERN.findall(canonical) if not _YEAR_PATTERN.fullmatch(value)}


def validate_settlement_identity(
    market_a: NormalizedMarket,
    market_b: NormalizedMarket,
) -> tuple[bool, list[str]]:
    """Reject pairs that cannot resolve as the same binary proposition."""
    reasons: list[str] = []
    title_a, title_b = market_a.title, market_b.title
    inverted = _check_option_inversion(title_a, title_b)

    type_a, type_b = _event_type(title_a), _event_type(title_b)
    if type_a and type_b and type_a != type_b:
        return False, [f"event_type_mismatch={type_a}:{type_b}"]

    years_a, years_b = _extract_years(title_a), _extract_years(title_b)
    if years_a and years_b and not (years_a & years_b):
        return False, [f"year_mismatch={sorted(years_a)}:{sorted(years_b)}"]

    locations_a, locations_b = _locations(title_a), _locations(title_b)
    if locations_a and locations_b and locations_a.isdisjoint(locations_b):
        return False, [f"location_mismatch={sorted(locations_a)}:{sorted(locations_b)}"]

    numbers_a, numbers_b = _non_year_numbers(title_a), _non_year_numbers(title_b)
    if numbers_a and numbers_b and numbers_a != numbers_b:
        return False, [f"threshold_mismatch={sorted(numbers_a)}:{sorted(numbers_b)}"]

    months_a = {month.lower()[:3] for month in _MONTH_PATTERN.findall(title_a)}
    months_b = {month.lower()[:3] for month in _MONTH_PATTERN.findall(title_b)}
    if months_a and months_b and months_a != months_b:
        return False, [f"deadline_month_mismatch={sorted(months_a)}:{sorted(months_b)}"]

    if market_a.end_date and market_b.end_date:
        left = market_a.end_date
        right = market_b.end_date
        if left.tzinfo is None:
            left = left.replace(tzinfo=timezone.utc)
        if right.tzinfo is None:
            right = right.replace(tzinfo=timezone.utc)
        if abs((left - right).total_seconds()) > 7 * 86400:
            return False, ["settlement_date_mismatch"]

    subject_a, subject_b = _subject(title_a), _subject(title_b)
    if not inverted and subject_a and subject_b and fuzz.ratio(subject_a, subject_b) < 92:
        return False, [f"subject_mismatch={subject_a}:{subject_b}"]

    endorse_a = _ENDORSE_OBJECT_PATTERN.search(title_a)
    endorse_b = _ENDORSE_OBJECT_PATTERN.search(title_b)
    if endorse_a and endorse_b:
        object_a = normalise_title(endorse_a.group(1))
        object_b = normalise_title(endorse_b.group(1))
        if fuzz.ratio(object_a, object_b) < 92:
            return False, [f"endorsement_object_mismatch={object_a}:{object_b}"]

    anchors_a = market_a._anchors or _extract_anchor_tokens(title_a)
    anchors_b = market_b._anchors or _extract_anchor_tokens(title_b)
    if len(anchors_a & anchors_b) < 2 and fuzz.token_sort_ratio(
        normalise_title(title_a), normalise_title(title_b)
    ) < 85:
        return False, ["insufficient_shared_identity"]

    reasons.append("hard_identity_passed")
    if type_a and type_b:
        reasons.append(f"event_type={type_a}")
    return True, reasons


def compute_match_confidence(
    market_a: NormalizedMarket,
    market_b: NormalizedMarket,
) -> Tuple[float, bool, list[str]]:
    """
    Compute confidence (0-100) that two markets are the SAME question.
    Returns (confidence, is_option_inverted, list_of_reasons).
    """
    reasons: list[str] = []

    identity_ok, identity_reasons = validate_settlement_identity(market_a, market_b)
    if not identity_ok:
        return 0.0, False, identity_reasons
    reasons.extend(identity_reasons)

    norm_a = market_a._normalised or normalise_title(market_a.title)
    norm_b = market_b._normalised or normalise_title(market_b.title)

    # Check for option order inversion (e.g. OpenAI or Anthropic vs Anthropic or OpenAI)
    is_inverted = _check_option_inversion(market_a.title, market_b.title)
    if is_inverted:
        reasons.append("option_order_inverted")

    # ── Primary: token_sort_ratio on normalised titles ──
    sort_score = fuzz.token_sort_ratio(norm_a, norm_b)
    raw_sort = fuzz.token_sort_ratio(market_a.title.lower(), market_b.title.lower())

    ev_a = normalise_title(market_a.event_title) if market_a.event_title else norm_a
    ev_b = normalise_title(market_b.event_title) if market_b.event_title else norm_b
    ev_sort = fuzz.token_sort_ratio(ev_a, ev_b)

    title_score = max(sort_score, raw_sort, ev_sort)
    reasons.append(f"title_sort={sort_score:.0f},raw={raw_sort:.0f},ev={ev_sort:.0f}")

    # ── Date proximity ──
    date_score = _date_proximity_score(market_a.end_date, market_b.end_date)
    reasons.append(f"date={date_score:.0f}")

    # ── Anchor overlap ──
    anchors_a = getattr(market_a, '_anchors', None) or _extract_anchor_tokens(market_a.title)
    anchors_b = getattr(market_b, '_anchors', None) or _extract_anchor_tokens(market_b.title)
    shared_anchors = anchors_a & anchors_b

    if shared_anchors:
        anchor_score = min(len(shared_anchors) * 20.0, 100.0)
        reasons.append(f"anchors={len(shared_anchors)}({','.join(list(shared_anchors)[:3])})")
    else:
        anchor_score = 0.0
        reasons.append("no_anchors")

    # ── Year check ──
    years_a = _extract_years(market_a.title)
    years_b = _extract_years(market_b.title)
    if years_a and years_b:
        if years_a == years_b:
            year_score = 100.0
        elif years_a & years_b:
            year_score = 60.0
        else:
            year_score = 0.0
            reasons.append(f"year_mismatch({years_a}&{years_b})")
    else:
        year_score = 50.0

    # ── Discriminator conflict ──
    disc_penalty = _check_discriminator_conflict(market_a.title, market_b.title)
    if disc_penalty < 0:
        reasons.append(f"disc_penalty={disc_penalty:.0f}")

    # ── Category match ──
    cat_bonus = 5.0 if (market_a.category == market_b.category and market_a.category != "other") else 0.0

    # ── Exact title match boost ──
    exact_boost = 0.0
    if sort_score >= 95 and raw_sort >= 95:
        exact_boost = 20.0
        reasons.append("exact_title_match=+20")

    # ── Strong-match boost (near-identical with rich anchors) ──
    # Titles with sort_score ≥82 AND ≥3 shared anchors AND matching event titles
    # are almost certainly the same question with minor phrasing differences
    strong_boost = 0.0
    if sort_score >= 82 and len(shared_anchors) >= 3 and ev_sort >= 85 and exact_boost == 0:
        strong_boost = 10.0
        reasons.append(f"strong_match_boost=+10")

    # ── Weighted combination ──
    confidence = (
        title_score * 0.40
        + raw_sort * 0.15
        + date_score * 0.15
        + anchor_score * 0.15
        + year_score * 0.10
        + cat_bonus
        + exact_boost
        + strong_boost
        + disc_penalty
    )

    return min(max(confidence, 0.0), 100.0), is_inverted, reasons


def _build_inverted_index(markets: List[NormalizedMarket]) -> Dict[str, List[int]]:
    index: Dict[str, List[int]] = defaultdict(list)
    for i, m in enumerate(markets):
        for token in _tokenize(m._normalised):
            if len(token) >= 3 and token not in _GENERIC_WORDS:
                index[token].append(i)
    return index


def find_matches(
    kalshi_markets: List[NormalizedMarket],
    poly_markets: List[NormalizedMarket],
    auto_threshold: float = 85.0,
    review_threshold: float = 70.0,
) -> List[MatchedPair]:
    """
    Find matching events across Kalshi and Polymarket.
    """
    if not kalshi_markets or not poly_markets:
        return []

    log.info(
        "Starting matching: %d Kalshi x %d Polymarket markets",
        len(kalshi_markets), len(poly_markets),
    )

    for m in kalshi_markets:
        m._normalised = normalise_title(m.title)
        m._anchors = _extract_anchor_tokens(m.title)
    for m in poly_markets:
        m._normalised = normalise_title(m.title)
        m._anchors = _extract_anchor_tokens(m.title)

    poly_index = _build_inverted_index(poly_markets)

    matches: List[MatchedPair] = []
    used_poly_ids: set[str] = set()
    candidate_counts = []

    for km in kalshi_markets:
        if not km._normalised:
            continue

        km_tokens = {t for t in _tokenize(km._normalised) if t not in _GENERIC_WORDS and len(t) >= 3}
        if not km_tokens:
            continue

        candidate_idx_counts: Dict[int, int] = defaultdict(int)
        for token in km_tokens:
            if token in poly_index:
                for idx in poly_index[token]:
                    candidate_idx_counts[idx] += 1

        candidate_indices = [idx for idx, count in candidate_idx_counts.items() if count >= 2]

        if not candidate_indices:
            continue

        candidate_counts.append(len(candidate_indices))

        quick_scores = [
            (idx, fuzz.token_sort_ratio(km._normalised, poly_markets[idx]._normalised))
            for idx in candidate_indices
        ]
        quick_scores.sort(key=lambda x: x[1], reverse=True)

        best_match: Optional[MatchedPair] = None
        best_confidence = 0.0

        for idx, quick_score in quick_scores[:10]:
            if quick_score < 65:
                break

            pm = poly_markets[idx]
            if pm.market_id in used_poly_ids:
                continue

            confidence, is_inverted, reasons = compute_match_confidence(km, pm)

            if confidence >= review_threshold and confidence > best_confidence:
                best_confidence = confidence
                best_match = MatchedPair(
                    kalshi=km,
                    polymarket=pm,
                    confidence=round(confidence, 1),
                    inverted_outcomes=is_inverted,
                    match_reasons=reasons,
                )

        if best_match:
            used_poly_ids.add(best_match.polymarket.market_id)
            matches.append(best_match)

    matches.sort(key=lambda m: m.confidence, reverse=True)

    auto = sum(1 for m in matches if m.confidence >= auto_threshold)
    review = sum(1 for m in matches if review_threshold <= m.confidence < auto_threshold)
    avg_candidates = (
        sum(candidate_counts) / len(candidate_counts) if candidate_counts else 0
    )
    log.info(
        "Matched %d pairs (%d auto, %d review) from %dx%d markets "
        "(avg %.1f candidates/market)",
        len(matches), auto, review, len(kalshi_markets), len(poly_markets),
        avg_candidates,
    )

    return matches
