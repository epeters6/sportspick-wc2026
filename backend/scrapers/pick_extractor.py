"""
Pick extractor — parses free-text social media posts into structured picks.

Uses rule-based patterns first (fast, no GPU), then falls back to a
lightweight spaCy model for entity extraction if available.

Returns a dict with keys: predicted_winner, predicted_score, confidence
"""
from __future__ import annotations

import re
from typing import Any

# ─── World Cup 2026 team names + common aliases ──────────────────────────────

TEAM_ALIASES: dict[str, str] = {
    # USA — DB uses "USA" (openfootball format)
    "usa": "USA", "usmnt": "USA", "united states": "USA",
    "us": "USA", "america": "USA", "u.s.": "USA",
    # South America
    "brazil": "Brazil", "brasil": "Brazil",
    "argentina": "Argentina", "messi": "Argentina",
    "colombia": "Colombia",
    "chile": "Chile",
    "uruguay": "Uruguay",
    "ecuador": "Ecuador",
    "paraguay": "Paraguay",
    "venezuela": "Venezuela",
    "peru": "Peru",
    "bolivia": "Bolivia",
    # Europe
    "france": "France", "les bleus": "France",
    "england": "England",
    "germany": "Germany", "deutschland": "Germany",
    "spain": "Spain", "espana": "Spain",
    "portugal": "Portugal", "ronaldo": "Portugal",
    "netherlands": "Netherlands", "holland": "Netherlands",
    "italy": "Italy",
    "croatia": "Croatia",
    "switzerland": "Switzerland",
    "belgium": "Belgium",
    "denmark": "Denmark",
    "sweden": "Sweden",
    "poland": "Poland",
    "serbia": "Serbia",
    "ukraine": "Ukraine",
    "austria": "Austria",
    "norway": "Norway",
    "scotland": "Scotland",
    "turkey": "Turkey", "turkiye": "Turkey",
    "czech republic": "Czech Republic", "czechia": "Czech Republic",
    "bosnia": "Bosnia & Herzegovina",
    "bosnia and herzegovina": "Bosnia & Herzegovina",
    "bosnia & herzegovina": "Bosnia & Herzegovina",
    # North/Central America & Caribbean
    "mexico": "Mexico",
    "canada": "Canada",
    "panama": "Panama",
    "haiti": "Haiti",
    "curacao": "Curaçao", "curaçao": "Curaçao",
    # Asia/Oceania
    "japan": "Japan",
    "south korea": "South Korea", "korea": "South Korea",
    "australia": "Australia", "socceroos": "Australia",
    "new zealand": "New Zealand",
    "iran": "Iran", "ir iran": "Iran",
    "saudi arabia": "Saudi Arabia",
    "qatar": "Qatar",
    "iraq": "Iraq",
    "jordan": "Jordan",
    "uzbekistan": "Uzbekistan",
    # Africa
    "morocco": "Morocco",
    "senegal": "Senegal",
    "nigeria": "Nigeria",
    "ghana": "Ghana",
    "south africa": "South Africa",
    "ivory coast": "Ivory Coast",
    "côte d'ivoire": "Ivory Coast",
    "dr congo": "DR Congo", "congo": "DR Congo",
    "cape verde": "Cape Verde",
    "egypt": "Egypt",
    "tunisia": "Tunisia",
    "algeria": "Algeria",
}

# Patterns that indicate a winner prediction
WIN_PATTERNS = [
    r"(?:i (?:think|believe|predict)|my pick(?:s)?(?:\s+(?:is|are))?)[:\s]+([a-z\s]+?)(?:\s+(?:to win|wins?|over|beats?|defeats?))",
    r"([a-z\s]+?)\s+(?:to win|wins?|will win|going to win|gonna win|beats?\s+(?:them|it))",
    r"([a-z\s]+?)\s+over\s+(?:[a-z\s]+)",
    r"backing\s+([a-z\s]+?)(?:\s+(?:here|to win|tonight|today)|\.|$)",
    r"final:\s*([a-z\s]+?)\s+\d",
    r"winner:\s*([a-z\s]+?)(?:\s|$|\.|,)",
    r"prediction:\s*([a-z\s]+?)(?:\s+\d|\.|$)",
]

# Score patterns like "3-1", "2-0", "1 - 0"
SCORE_PATTERN = re.compile(r"\b(\d{1,2})\s*[-–]\s*(\d{1,2})\b")

# Confidence keywords
HIGH_CONFIDENCE = ["lock", "sure thing", "guaranteed", "100%", "easy", "🔒", "💯", "easy money"]
MEDIUM_CONFIDENCE = ["think", "believe", "predict", "feeling", "leaning", "probably"]
LOW_CONFIDENCE = ["maybe", "might", "could", "possibly", "not sure", "50/50"]


def extract_pick(text: str) -> dict[str, Any]:
    """
    Parse a social media post and return:
      predicted_winner: str | None  — canonical team name
      predicted_score:  str | None  — e.g. "3-1"
      confidence:       float | None — 0.0 to 1.0
    """
    if not text:
        return {}

    text_lower = text.lower()

    predicted_winner = _extract_winner(text_lower)
    predicted_score = _extract_score(text_lower)
    confidence = _extract_confidence(text_lower)

    return {
        "predicted_winner": predicted_winner,
        "predicted_score": predicted_score,
        "confidence": confidence,
    }


def _extract_winner(text_lower: str) -> str | None:
    for pattern in WIN_PATTERNS:
        m = re.search(pattern, text_lower)
        if m:
            raw = m.group(1).strip()
            canonical = _canonicalise_team(raw)
            if canonical:
                return canonical

    # Fallback: find any team mention preceded by directional verbs
    for alias, canonical in TEAM_ALIASES.items():
        if alias in text_lower:
            # Look for surrounding win-signal words
            idx = text_lower.find(alias)
            window = text_lower[max(0, idx - 40): idx + 40]
            if any(sig in window for sig in [
                "win", "beat", "pick", "predict", "back", "going", "final", "winner"
            ]):
                return canonical
    return None


def _extract_score(text_lower: str) -> str | None:
    m = SCORE_PATTERN.search(text_lower)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return None


def _extract_confidence(text_lower: str) -> float | None:
    if any(kw in text_lower for kw in HIGH_CONFIDENCE):
        return 0.90
    if any(kw in text_lower for kw in MEDIUM_CONFIDENCE):
        return 0.65
    if any(kw in text_lower for kw in LOW_CONFIDENCE):
        return 0.40
    return 0.55  # default neutral


def _canonicalise_team(raw: str) -> str | None:
    raw = raw.strip().lower()
    # Direct alias lookup
    if raw in TEAM_ALIASES:
        return TEAM_ALIASES[raw]
    # Partial match
    for alias, canonical in TEAM_ALIASES.items():
        if alias in raw or raw in alias:
            return canonical
    return None
