"""
Pick extractor — parses free-text social media posts into structured picks.

Uses rule-based patterns first (fast, no GPU), then falls back to a
lightweight spaCy model for entity extraction if available.

Returns a dict with keys:
  predicted_winner  — canonical team/player name, "draw", "over", "under", "yes", "no"
  predicted_score   — e.g. "3-1"
  confidence        — 0.0 to 1.0
  bet_type          — "moneyline" | "draw" | "total_goals" | "team_total_runs" | ...
  bet_line          — numeric line e.g. "2.5"
  bet_subject       — player name, team name, "match", "1h", "f5", etc.
"""
from __future__ import annotations

import re
from typing import Any

from backend.sports_data.mlb_fetcher import MLB_TEAM_ALIASES

# ─── World Cup 2026 team names + common aliases ──────────────────────────────

TEAM_ALIASES: dict[str, str] = {
    # USA — DB uses "USA" (openfootball format)
    # NOTE: "us" and "america" intentionally omitted — too ambiguous in English prose
    "usa": "USA", "usmnt": "USA", "united states": "USA", "u.s.": "USA",
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

# Draw detection patterns
DRAW_PATTERNS = [
    r"\b(?:i (?:think|predict|expect|see)|my pick(?:\s+is)?)[:\s]+(?:a\s+)?draw\b",
    r"\b(?:predicting|backing|going with|calling)\s+(?:a\s+)?draw\b",
    r"\bpick[:\s]+draw\b",
    r"\bdraw prediction\b",
    r"\b(?:this ends?|finishing?)\s+(?:in\s+)?(?:a\s+)?draw\b",
    r"\b(?:1[-–]1|0[-–]0|2[-–]2)\s+draw\b",
    r"\bdraw\s+(?:here|bet|pick|value)\b",
    r"\bno (?:winner|result)\b",
    r"\bx\s+(?:value|bet|pick)\b",   # European odds notation: 1 = home, X = draw, 2 = away
]

# Over/under — game totals only (must mention goals/runs or explicit game total)
GAME_TOTAL_OVER_PATTERNS = [
    r"(?:game\s+)?(?:total\s+)?over\s+([\d.]+)\s*(?:goals?|runs?)\b",
    r"over\s+([\d.]+)\s+(?:game\s+)?(?:total\s+)?(?:goals?|runs?)\b",
]
GAME_TOTAL_UNDER_PATTERNS = [
    r"(?:game\s+)?(?:total\s+)?under\s+([\d.]+)\s*(?:goals?|runs?)\b",
    r"under\s+([\d.]+)\s+(?:game\s+)?(?:total\s+)?(?:goals?|runs?)\b",
]

# Legacy generic patterns — kept for extract_pick fallback only when no stat keyword follows
OVER_PATTERNS = [
    r"over\s+([\d.]+)(?:\s+(?:goals?|total))?\b",
    r"([\d.]+)\+\s+goals?\b",
    r"\btake\s+the\s+over\s+(?:on\s+)?([\d.]+)\b",
    r"\bbetting?\s+(?:the\s+)?over\s+([\d.]+)\b",
]
UNDER_PATTERNS = [
    r"under\s+([\d.]+)(?:\s+(?:goals?|total))?\b",
    r"\btake\s+the\s+under\s+(?:on\s+)?([\d.]+)\b",
    r"\bbetting?\s+(?:the\s+)?under\s+([\d.]+)\b",
]

# Structured prop patterns (order matters — run before generic O/U)
PLAYER_STAT_OU_RE = re.compile(
    r"(?P<name>[A-Z][\w'-]+(?:\s+[A-Z][\w'-]+){0,3})\s+"
    r"(?P<dir>over|under)\s+"
    r"(?P<line>[\d.]+)\s+"
    r"(?P<stat>shots?(?:\s+on\s+target)?|tackles?|goals?|assists?|"
    r"strikeouts?|ks?|hits?|rbis?|bases?|points?|rebounds?)\b",
    re.IGNORECASE,
)
TEAM_STAT_OU_RE = re.compile(
    r"(?P<team>[A-Za-z][\w\s'.-]{1,30}?)\s+"
    r"(?:(?:TT|TTO|team total)\s+)?"
    r"(?P<dir>over|under)\s+"
    r"(?P<line>[\d.]+)\s*"
    r"(?P<stat>shots?|tackles?|goals?|runs?|hits?|strikeouts?|ks?|bases?)?\b",
    re.IGNORECASE,
)
TEAM_TTO_RE = re.compile(
    r"(?P<team>[A-Z]{2,4}|[A-Za-z][\w\s'.-]{2,25}?)\s+TTO\s+(?P<line>[\d.]+)\b",
    re.IGNORECASE,
)
FIRST_HALF_OU_RE = re.compile(
    r"(?:(?P<home>[^/]+?)\s*/\s*(?P<away>[^/\n]+?)\s+)?"
    r"(?:1h|1st half|first half)\s+"
    r"(?P<dir>over|under)\s+"
    r"(?P<line>[\d.]+)\s*"
    r"(?:goals?|runs?)?",
    re.IGNORECASE,
)
MLB_F5_OU_RE = re.compile(
    r"(?:(?P<team1>[A-Z]{2,4}|[A-Za-z][\w\s'.-]+?)\s+(?P<team2>[A-Z]{2,4}|[A-Za-z][\w\s'.-]+?)\s+)?"
    r"F5\s+(?P<dir>over|under)\s+(?P<line>[\d.]+)\b",
    re.IGNORECASE,
)
PLAYER_TO_GO_RE = re.compile(
    r"(?P<name>[A-Z][\w'-]+(?:\s+[A-Z][\w'-]+)?)\s+to\s+go\s+"
    r"(?P<dir>over|under)\s+"
    r"(?P<line>[\d.]+)\s+"
    r"(?P<stat>hits?|strikeouts?|ks?|rbis?|runs?|bases?)\b",
    re.IGNORECASE,
)
PLAYER_K_RE = re.compile(
    r"(?P<name>[A-Z][\w'-]+(?:\s+[A-Z][\w'-]+)?)\s+"
    r"(?:OVER|Over|over)\s+"
    r"(?P<line>[\d.]+)\s*"
    r"(?:k|ks|strikeouts?)\b",
    re.IGNORECASE,
)

MLB_ABBREVS: dict[str, str] = {
    "ath": "Oakland Athletics", "laa": "Los Angeles Angels", "lad": "Los Angeles Dodgers",
    "nyy": "New York Yankees", "nym": "New York Mets", "bos": "Boston Red Sox",
    "chc": "Chicago Cubs", "cws": "Chicago White Sox", "chw": "Chicago White Sox",
    "hou": "Houston Astros", "atl": "Atlanta Braves", "phi": "Philadelphia Phillies",
    "sf": "San Francisco Giants", "sfg": "San Francisco Giants",
    "stl": "St. Louis Cardinals", "sd": "San Diego Padres", "sdp": "San Diego Padres",
    "tor": "Toronto Blue Jays", "tex": "Texas Rangers", "sea": "Seattle Mariners",
    "min": "Minnesota Twins", "tb": "Tampa Bay Rays", "tbr": "Tampa Bay Rays",
    "bal": "Baltimore Orioles", "det": "Detroit Tigers", "cle": "Cleveland Guardians",
    "kc": "Kansas City Royals", "kcr": "Kansas City Royals",
    "oak": "Oakland Athletics", "mia": "Miami Marlins", "was": "Washington Nationals",
    "wsh": "Washington Nationals", "pit": "Pittsburgh Pirates", "cin": "Cincinnati Reds",
    "mil": "Milwaukee Brewers", "col": "Colorado Rockies", "ari": "Arizona Diamondbacks",
    "az": "Arizona Diamondbacks",
}

# Corners total / team corners
CORNERS_OVER_PATTERNS = [
    r"over\s+([\d.]+)\s+corners?\b",
    r"([\d.]+)\+\s*corners?\b",
    r"corners?\s+over\s+([\d.]+)\b",
    r"\btake\s+(?:the\s+)?over\s+(?:on\s+)?(?:the\s+)?corners?\b",
]
CORNERS_UNDER_PATTERNS = [
    r"under\s+([\d.]+)\s+corners?\b",
    r"corners?\s+under\s+([\d.]+)\b",
]

# Cards patterns (yellow/red)
CARDS_OVER_PATTERNS = [
    r"over\s+([\d.]+)\s+(?:yellow\s+)?cards?\b",
    r"([\d.]+)\+\s*(?:yellow\s+)?cards?\b",
    r"\bcard[s]?\s+over\s+([\d.]+)\b",
]
CARDS_UNDER_PATTERNS = [
    r"under\s+([\d.]+)\s+(?:yellow\s+)?cards?\b",
    r"\bcards?\s+under\s+([\d.]+)\b",
]

# Team / match shots
SHOTS_OVER_PATTERNS = [
    r"over\s+([\d.]+)\s+shots?(?:\s+on\s+target)?\b",
    r"([\d.]+)\+\s*shots?(?:\s+on\s+target)?\b",
    r"\bshots?\s+over\s+([\d.]+)\b",
]
SHOTS_UNDER_PATTERNS = [
    r"under\s+([\d.]+)\s+shots?\b",
    r"\bshots?\s+under\s+([\d.]+)\b",
]

# Player to score (anytime scorer)
PLAYER_SCORER_PATTERNS = [
    r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+(?:to\s+)?(?:anytime\s+)?(?:score|bag(?:s\s+a)?|nets?|to\s+score)",
    r"anytime\s+scorer[:\s]+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)",
    r"(?:back|backing|love|taking)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+to\s+score",
    r"first\s+(?:goal)?scorer[:\s]+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)",
    r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+(?:gets?|grabs?|scores?)\s+(?:a\s+)?goal",
]

# Player shots on target
PLAYER_SHOTS_PATTERNS = [
    r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+(?:to have\s+)?(?:over\s+)?([\d.]+)\+?\s+shots?\s+on\s+target",
    r"over\s+([\d.]+)\s+shots?\s+on\s+target\s+(?:for\s+)?([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)",
]

# Player assists
PLAYER_ASSIST_PATTERNS = [
    r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+(?:to\s+get\s+)?(?:an?\s+)?assist",
    r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+(?:anytime\s+)?(?:assist|provider)",
    r"assist[:\s]+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)",
]

# Both-teams-to-score patterns
BTTS_YES_PATTERNS = [
    r"\bbtts\s*(?:yes|[✓✔])\b",
    r"\bboth\s+teams?\s+(?:to\s+)?score\b",
    r"\bboth\s+sides?\s+(?:to\s+)?score\b",
]
BTTS_NO_PATTERNS = [
    r"\bbtts\s*no\b",
    r"\bnot?\s+both\s+teams?\s+(?:to\s+)?score\b",
    r"\bclean\s+sheet\b",
]

# Score patterns like "3-1", "2-0", "1 - 0"
SCORE_PATTERN = re.compile(r"\b(\d{1,2})\s*[-–]\s*(\d{1,2})\b")

# Confidence keywords
HIGH_CONFIDENCE = ["lock", "sure thing", "guaranteed", "100%", "easy", "🔒", "💯", "easy money", "best bet", "top pick"]
MEDIUM_CONFIDENCE = ["think", "believe", "predict", "feeling", "leaning", "probably"]
LOW_CONFIDENCE = ["maybe", "might", "could", "possibly", "not sure", "50/50"]


def extract_all_picks(
    text: str,
    allowed_teams: set[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Like extract_pick but returns ALL explicitly-backed picks in the text,
    including draws and over/under bets.

    Uses only structured WIN_PATTERNS (requires clear betting language like
    "backing X", "X to win", "prediction: X") — deliberately avoids the broad
    fallback so match preview articles don't generate a pick for every team
    mentioned in passing.

    allowed_teams: if provided, moneyline picks for teams NOT in this set are
    filtered out. Pass the two teams from a video title to prevent false
    positives from match previews that describe both sides in detail.
    """
    if not text:
        return []

    text_lower = text.lower()
    score = _extract_score(text_lower)
    confidence = _extract_confidence(text_lower)
    picks: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    def _add(pick: dict) -> None:
        key = (
            pick.get("bet_type", "moneyline"),
            pick.get("predicted_winner", ""),
            pick.get("bet_line"),
            pick.get("bet_subject"),
        )
        if key not in seen_keys:
            seen_keys.add(key)
            picks.append(pick)

    # Parse line-by-line first (multi-pick parlay posts)
    for line in _split_pick_lines(text):
        line_lower = line.lower()
        for pick in _extract_line_picks(line, line_lower, confidence, score):
            _add(pick)

    # Whole-post patterns that may span context
    draw_pick = _extract_draw(text_lower, confidence, score)
    if draw_pick:
        _add(draw_pick)

    if score:
        parts = score.split("-")
        if len(parts) == 2 and parts[0] == parts[1]:
            _add({
                "predicted_winner": "draw",
                "predicted_score": score,
                "confidence": (confidence or 0.55) * 0.9,
                "bet_type": "draw",
                "bet_line": None,
                "bet_subject": None,
            })

    btts_pick = _extract_btts(text_lower, confidence)
    if btts_pick:
        _add(btts_pick)

    for pick in _extract_player_props(text, confidence):
        _add(pick)

    # Game-level totals only when not already captured per-line
    for ou_pick in _extract_game_totals(text_lower, confidence):
        _add(ou_pick)

    for pattern in WIN_PATTERNS:
        for m in re.finditer(pattern, text_lower):
            raw = m.group(1).strip()
            canonical = _canonicalise_team(raw)
            if not canonical:
                continue
            if allowed_teams and canonical not in allowed_teams:
                continue
            _add({
                "predicted_winner": canonical,
                "predicted_score": score,
                "confidence": confidence,
                "bet_type": "moneyline",
                "bet_line": None,
                "bet_subject": None,
            })

    # If nothing found, fall back to single-pick extraction
    if not picks:
        single = extract_pick(text)
        if single.get("predicted_winner"):
            winner = single["predicted_winner"]
            if not allowed_teams or winner in allowed_teams or single.get("bet_type") in ("draw", "total_goals", "btts"):
                return [single]
        return []

    return picks


def extract_pick(text: str) -> dict[str, Any]:
    """
    Parse a social media post and return the single most prominent pick.

    Returns a dict with:
      predicted_winner — canonical team name, "draw", "over", or "under"
      predicted_score  — e.g. "3-1"
      confidence       — 0.0 to 1.0
      bet_type         — "moneyline" | "draw" | "total_goals" | "btts" | "spread"
      bet_line         — e.g. "2.5" for over/under, None otherwise
    """
    if not text:
        return {}

    text_lower = text.lower()
    confidence = _extract_confidence(text_lower)
    score = _extract_score(text_lower)

    # Check specialised bet types first — they're more unambiguous
    draw_pick = _extract_draw(text_lower, confidence, score)
    if draw_pick:
        return draw_pick

    ou_picks = _extract_game_totals(text_lower, confidence)
    if ou_picks:
        return ou_picks[0]

    # Last-resort generic O/U (avoid stealing player/team lines)
    ou_picks = _extract_over_under(text_lower, confidence)
    if ou_picks:
        return ou_picks[0]

    btts_pick = _extract_btts(text_lower, confidence)
    if btts_pick:
        return btts_pick

    corner_picks = _extract_corners(text_lower, confidence)
    if corner_picks:
        return corner_picks[0]

    card_picks = _extract_cards(text_lower, confidence)
    if card_picks:
        return card_picks[0]

    shot_picks = _extract_shots(text_lower, confidence)
    if shot_picks:
        return shot_picks[0]

    player_props = _extract_player_props(text, confidence)
    if player_props:
        return player_props[0]

    predicted_winner = _extract_winner(text_lower)

    return {
        "predicted_winner": predicted_winner,
        "predicted_score": score,
        "confidence": confidence,
        "bet_type": "moneyline" if predicted_winner else None,
        "bet_line": None,
        "bet_subject": None,
    }


def _split_pick_lines(text: str) -> list[str]:
    """Split multi-pick posts into individual lines for structured parsing."""
    if not text:
        return []
    chunks = re.split(r"[\n\r]+|(?<=[•·])\s*|(?<=\d[.)])\s+", text)
    lines: list[str] = []
    for chunk in chunks:
        chunk = chunk.strip(" •·-\t")
        if len(chunk) >= 8:
            lines.append(chunk)
    return lines


def _resolve_team_token(token: str) -> str | None:
    raw = (token or "").strip()
    if not raw:
        return None
    low = raw.lower()
    if low in MLB_ABBREVS:
        return MLB_ABBREVS[low]
    return _canonicalise_team(low)


def _stat_to_bet_type(stat: str, *, subject_kind: str) -> str:
    s = (stat or "").lower().strip()
    if s in ("k", "ks", "strikeout", "strikeouts"):
        s = "strikeout"
    elif s.endswith("s") and s not in ("assists",):
        s = s.rstrip("s")
    if subject_kind == "player":
        return {
            "shot": "player_shots",
            "tackle": "player_tackles",
            "goal": "player_goals",
            "assist": "player_assists",
            "strikeout": "player_strikeouts",
            "hit": "player_hits",
            "rbi": "player_rbis",
        }.get(s, f"player_{s}")
    if subject_kind == "team":
        return {
            "shot": "team_shots",
            "tackle": "team_tackles",
            "goal": "team_total_goals",
            "run": "team_total_runs",
            "hit": "team_hits",
            "strikeout": "team_strikeouts",
        }.get(s, f"team_{s}")
    if subject_kind == "1h":
        return "first_half_goals" if s in ("goal", "") else f"first_half_{s}s"
    if subject_kind == "f5":
        return "first_five_runs" if s in ("run", "") else f"first_five_{s}s"
    return {
        "goal": "total_goals",
        "run": "total_runs",
    }.get(s, "total_goals")


def _ou_pick(
    direction: str,
    line: str,
    bet_type: str,
    subject: str | None,
    confidence: float | None,
) -> dict[str, Any]:
    return {
        "predicted_winner": direction.lower(),
        "predicted_score": None,
        "confidence": confidence,
        "bet_type": bet_type,
        "bet_line": line,
        "bet_subject": subject,
    }


def _strip_line_prefix(line: str) -> str:
    """Remove emoji / checkmark prefixes from parlay lines."""
    return re.sub(r"^[\s✅❌👎⚾️🏀🔐📋]+", "", line or "").strip()


def _extract_line_picks(
    line: str,
    line_lower: str,
    confidence: float | None,
    score: str | None,
) -> list[dict[str, Any]]:
    """Extract structured props from a single line (player/team/period O/U)."""
    line = _strip_line_prefix(line)
    line_lower = line.lower()
    picks: list[dict[str, Any]] = []

    m = PLAYER_TO_GO_RE.search(line)
    if m:
        bt = _stat_to_bet_type(m.group("stat"), subject_kind="player")
        picks.append(_ou_pick(
            m.group("dir"), m.group("line"), bt, m.group("name").strip(), confidence,
        ))
        return picks

    m = FIRST_HALF_OU_RE.search(line)
    if m:
        stat = "goal" if "goal" in line_lower or "run" not in line_lower else "run"
        bt = _stat_to_bet_type(stat, subject_kind="1h")
        subj = "1h"
        if m.group("home") and m.group("away"):
            subj = f"{m.group('home').strip()} vs {m.group('away').strip()} (1H)"
        picks.append(_ou_pick(m.group("dir"), m.group("line"), bt, subj, confidence))
        return picks

    m = MLB_F5_OU_RE.search(line)
    if m:
        subj = "f5"
        if m.group("team1") and m.group("team2"):
            subj = f"{m.group('team1').strip()} vs {m.group('team2').strip()} (F5)"
        picks.append(_ou_pick(
            m.group("dir"), m.group("line"), "first_five_runs", subj, confidence,
        ))
        return picks

    m = PLAYER_K_RE.search(line)
    if m:
        picks.append(_ou_pick(
            "over", m.group("line"), "player_strikeouts", m.group("name").strip(), confidence,
        ))
        return picks

    m = PLAYER_STAT_OU_RE.search(line)
    if m:
        name = m.group("name").strip()
        if _resolve_team_token(name):
            pass  # fall through to team handler
        else:
            stat = m.group("stat")
            bt = _stat_to_bet_type(stat, subject_kind="player")
            picks.append(_ou_pick(
                m.group("dir"), m.group("line"), bt, name, confidence,
            ))
            return picks

    m = TEAM_TTO_RE.search(line)
    if m:
        team = _resolve_team_token(m.group("team")) or m.group("team").strip()
        picks.append(_ou_pick(
            "over", m.group("line"), "team_total_runs", team, confidence,
        ))
        return picks

    m = TEAM_STAT_OU_RE.search(line)
    if m:
        team_raw = m.group("team").strip()
        team = _resolve_team_token(team_raw) or team_raw
        stat = (m.group("stat") or "").lower()
        if not stat:
            stat = "run" if re.search(r"\b(?:mlb|baseball|runs?|f5|tto|tt)\b", line_lower) else "goal"
            if "shot" in line_lower:
                stat = "shot"
            elif "tackle" in line_lower:
                stat = "tackle"
        bt = _stat_to_bet_type(stat, subject_kind="team")
        picks.append(_ou_pick(
            m.group("dir"), m.group("line"), bt, team, confidence,
        ))
        return picks

    for pick in _extract_corners(line_lower, confidence):
        pick["bet_subject"] = "match"
        picks.append(pick)
    for pick in _extract_cards(line_lower, confidence):
        pick["bet_subject"] = "match"
        picks.append(pick)
    for pick in _extract_shots(line_lower, confidence):
        if not pick.get("bet_subject"):
            pick["bet_subject"] = "match"
        picks.append(pick)

    return picks


def _extract_game_totals(text_lower: str, confidence: float | None) -> list[dict[str, Any]]:
    """Match-level O/U only when goals/runs are explicit."""
    if re.search(r"(?:1h|1st half|first half)\s+(?:over|under)", text_lower):
        return []
    picks: list[dict[str, Any]] = []
    for pattern in GAME_TOTAL_OVER_PATTERNS:
        m = re.search(pattern, text_lower)
        if m:
            stat = "run" if "run" in m.group(0) else "goal"
            bt = _stat_to_bet_type(stat, subject_kind="match")
            picks.append(_ou_pick("over", m.group(1), bt, "match", confidence))
            break
    for pattern in GAME_TOTAL_UNDER_PATTERNS:
        m = re.search(pattern, text_lower)
        if m:
            stat = "run" if "run" in m.group(0) else "goal"
            bt = _stat_to_bet_type(stat, subject_kind="match")
            picks.append(_ou_pick("under", m.group(1), bt, "match", confidence))
            break
    return picks


def _extract_draw(text_lower: str, confidence: float | None, score: str | None) -> dict[str, Any] | None:
    for pattern in DRAW_PATTERNS:
        if re.search(pattern, text_lower):
            return {
                "predicted_winner": "draw",
                "predicted_score": score,
                "confidence": confidence,
                "bet_type": "draw",
                "bet_line": None,
                "bet_subject": None,
            }
    return None


def _extract_over_under(text_lower: str, confidence: float | None) -> list[dict[str, Any]]:
    """Generic O/U fallback — skips lines that look like player/team/period props."""
    if re.search(
        r"[a-z]+\s+over\s+[\d.]+\s+(?:shots?|tackles?|ks?|strikeouts?|assists?)\b",
        text_lower,
    ):
        return []
    if re.search(r"\b(?:tto|tt|1h|f5|1st half)\b", text_lower):
        return []
    picks = []
    for pattern in OVER_PATTERNS:
        m = re.search(pattern, text_lower)
        if m:
            line = next((g for g in m.groups() if g), None)
            stat = "run" if "run" in text_lower else "goal"
            bt = _stat_to_bet_type(stat, subject_kind="match")
            picks.append(_ou_pick("over", line, bt, "match", confidence))
            break
    for pattern in UNDER_PATTERNS:
        m = re.search(pattern, text_lower)
        if m:
            line = next((g for g in m.groups() if g), None)
            stat = "run" if "run" in text_lower else "goal"
            bt = _stat_to_bet_type(stat, subject_kind="match")
            picks.append(_ou_pick("under", line, bt, "match", confidence))
            break
    return picks


def _extract_btts(text_lower: str, confidence: float | None) -> dict[str, Any] | None:
    for pattern in BTTS_YES_PATTERNS:
        if re.search(pattern, text_lower):
            return {
                "predicted_winner": "yes",
                "predicted_score": None,
                "confidence": confidence,
                "bet_type": "btts",
                "bet_line": None,
                "bet_subject": "match",
            }
    for pattern in BTTS_NO_PATTERNS:
        if re.search(pattern, text_lower):
            return {
                "predicted_winner": "no",
                "predicted_score": None,
                "confidence": confidence,
                "bet_type": "btts",
                "bet_line": None,
                "bet_subject": "match",
            }
    return None


def _extract_winner(text_lower: str) -> str | None:
    for pattern in WIN_PATTERNS:
        m = re.search(pattern, text_lower)
        if m:
            raw = m.group(1).strip()
            canonical = _canonicalise_team(raw)
            if canonical:
                return canonical

    # Fallback: find any team mention preceded by directional verbs
    for alias, canonical in {**TEAM_ALIASES, **MLB_TEAM_ALIASES}.items():
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
        return 0.72
    if any(kw in text_lower for kw in MEDIUM_CONFIDENCE):
        return 0.58
    if any(kw in text_lower for kw in LOW_CONFIDENCE):
        return 0.42
    return 0.52  # default — slightly above coin flip, not a strong lean


def _extract_corners(text_lower: str, confidence: float | None) -> list[dict]:
    picks = []
    for pat in CORNERS_OVER_PATTERNS:
        m = re.search(pat, text_lower)
        if m:
            line = next((g for g in m.groups() if g), None)
            picks.append({"predicted_winner": "over", "predicted_score": None,
                          "confidence": confidence, "bet_type": "corners", "bet_line": line,
                          "bet_subject": "match"})
            break
    for pat in CORNERS_UNDER_PATTERNS:
        m = re.search(pat, text_lower)
        if m:
            line = next((g for g in m.groups() if g), None)
            picks.append({"predicted_winner": "under", "predicted_score": None,
                          "confidence": confidence, "bet_type": "corners", "bet_line": line,
                          "bet_subject": "match"})
            break
    return picks


def _extract_cards(text_lower: str, confidence: float | None) -> list[dict]:
    picks = []
    for pat in CARDS_OVER_PATTERNS:
        m = re.search(pat, text_lower)
        if m:
            line = next((g for g in m.groups() if g), None)
            picks.append({"predicted_winner": "over", "predicted_score": None,
                          "confidence": confidence, "bet_type": "cards", "bet_line": line,
                          "bet_subject": "match"})
            break
    for pat in CARDS_UNDER_PATTERNS:
        m = re.search(pat, text_lower)
        if m:
            line = next((g for g in m.groups() if g), None)
            picks.append({"predicted_winner": "under", "predicted_score": None,
                          "confidence": confidence, "bet_type": "cards", "bet_line": line,
                          "bet_subject": "match"})
            break
    return picks


def _extract_shots(text_lower: str, confidence: float | None) -> list[dict]:
    picks = []
    for pat in SHOTS_OVER_PATTERNS:
        m = re.search(pat, text_lower)
        if m:
            line = next((g for g in m.groups() if g), None)
            picks.append({"predicted_winner": "over", "predicted_score": None,
                          "confidence": confidence, "bet_type": "shots", "bet_line": line,
                          "bet_subject": "match"})
            break
    for pat in SHOTS_UNDER_PATTERNS:
        m = re.search(pat, text_lower)
        if m:
            line = next((g for g in m.groups() if g), None)
            picks.append({"predicted_winner": "under", "predicted_score": None,
                          "confidence": confidence, "bet_type": "shots", "bet_line": line,
                          "bet_subject": "match"})
            break
    return picks


def _extract_player_props(text: str, confidence: float | None) -> list[dict]:
    """Extract player prop picks from original-case text (names are title-case)."""
    picks: list[dict] = []
    seen: set[str] = set()

    # Anytime scorer
    for pat in PLAYER_SCORER_PATTERNS:
        for m in re.finditer(pat, text):
            name = m.group(1).strip()
            if len(name) < 3 or name in seen:
                continue
            seen.add(name)
            picks.append({"predicted_winner": name, "predicted_score": None,
                          "confidence": confidence, "bet_type": "player_scorer",
                          "bet_line": None, "bet_subject": name})

    # Player shots on target
    for pat in PLAYER_SHOTS_PATTERNS:
        m = re.search(pat, text)
        if m:
            groups = [g for g in m.groups() if g]
            # Detect which group is name vs line
            name = next((g for g in groups if re.match(r"[A-Z]", g)), None)
            line = next((g for g in groups if re.match(r"[\d.]", g)), None)
            if name and name not in seen:
                seen.add(name)
                picks.append({"predicted_winner": "over", "predicted_score": None,
                              "confidence": confidence, "bet_type": "player_shots",
                              "bet_line": line, "bet_subject": name})

    # Player assists
    for pat in PLAYER_ASSIST_PATTERNS:
        for m in re.finditer(pat, text):
            name = m.group(1).strip()
            if len(name) < 3 or name in seen:
                continue
            seen.add(name)
            picks.append({"predicted_winner": name, "predicted_score": None,
                          "confidence": confidence, "bet_type": "player_assists",
                          "bet_line": None, "bet_subject": name})

    return picks


def _canonicalise_team(raw: str) -> str | None:
    raw = raw.strip().lower()
    # Direct alias lookup (soccer + MLB)
    if raw in TEAM_ALIASES:
        return TEAM_ALIASES[raw]
    if raw in MLB_TEAM_ALIASES:
        return MLB_TEAM_ALIASES[raw]
    # Partial match
    for alias, canonical in TEAM_ALIASES.items():
        if alias in raw or raw in alias:
            return canonical
    for alias, canonical in MLB_TEAM_ALIASES.items():
        if alias in raw or raw in alias:
            return canonical
    return None
