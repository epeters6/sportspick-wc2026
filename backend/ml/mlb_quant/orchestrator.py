import json
import os
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import pybaseball as pyb
import requests

pyb.cache.enable()

_QUANT_DIR = Path(__file__).resolve().parent
MANIFEST_PATH = _QUANT_DIR / "manifest.json"
TIER_OVERRIDES_PATH = _QUANT_DIR / "tier_overrides.json"
UMPIRE_OVERRIDES_PATH = _QUANT_DIR / "umpire_overrides.json"

DEFAULT_PROP_LINE = 17.5
BASELINE_LOOKBACK_START = "2026-03-01"
OFFENSE_LOOKBACK_DAYS = 45
REQUEST_TIMEOUT_SECONDS = 8

DEFAULT_SIGMA_BY_METRIC: dict[str, float] = {
    "release_speed": 1.5,
    "release_pos_z": 0.18,
    "release_pos_x": 0.22,
    "release_spin_rate": 120.0,
    "release_extension": 0.18,
    "pfx_x": 4.5,
    "pfx_z": 4.5,
}

PITCH_METRICS = [
    "release_speed",
    "release_pos_z",
    "release_pos_x",
    "release_spin_rate",
    "release_extension",
    "pfx_x",
    "pfx_z",
]

FASTBALL_TYPES = {
    "4-Seam Fastball",
    "Two-Seam Fastball",
    "Sinker",
    "Cutter",
}

SWING_EVENTS = {
    "swinging_strike",
    "swinging_strike_blocked",
    "missed_bunt",
    "foul",
    "foul_tip",
    "foul_bunt",
    "hit_into_play",
    "hit_into_play_no_out",
    "hit_into_play_score",
}
WHIFF_EVENTS = {
    "swinging_strike",
    "swinging_strike_blocked",
    "missed_bunt",
}
CALLED_STRIKE_EVENTS = {"called_strike"}
FIRST_STRIKE_EVENTS = CALLED_STRIKE_EVENTS | WHIFF_EVENTS | {
    "foul",
    "foul_tip",
    "foul_bunt",
    "hit_into_play",
    "hit_into_play_no_out",
    "hit_into_play_score",
}
CONTACT_EVENTS = {
    "foul",
    "foul_tip",
    "foul_bunt",
    "hit_into_play",
    "hit_into_play_no_out",
    "hit_into_play_score",
}

DEFAULT_TIER_OVERRIDES_BY_NAME = {
    "tarik skubal": 1,
}

TODAY_STARTERS = [
    ("Kyle Leahy", "STL"),
    ("Janson Junk", "MIA"),
    ("Peter Lambert", "HOU"),
    ("Tanner Bibee", "CLE"),
    ("Brandon Williamson", "CIN"),
    ("Nick Martinez", "TBR"),
    ("Chris Bassitt", "BAL"),
    ("Michael Wacha", "KCR"),
    ("Eric Lauer", "TOR"),
    ("Jose Soriano", "LAA"),
    ("Aaron Civale", "ATH"),
    ("Logan Gilbert", "SEA"),
    ("Chad Patrick", "MIL"),
    ("Casey Mize", "DET"),
    ("Martin Perez", "ATL"),
    ("Zack Littell", "WSN"),
    ("Max Fried", "NYY"),
    ("Ranger Suarez", "BOS"),
    ("Connor Prielipp", "MIN"),
    ("Clay Holmes", "NYM"),
    ("Kyle Backhus", "PHI"),
    ("Matthew Boyd", "CHC"),
    ("Braxton Ashcraft", "PIT"),
    ("Jack Leiter", "TEX"),
    ("Walker Buehler", "SDP"),
    ("Tomoyuki Sugano", "COL"),
    ("Anthony Kay", "CHW"),
    ("Eduardo Rodriguez", "ARI"),
    ("Shohei Ohtani", "LAD"),
    ("Tyler Mahle", "SFG"),
]

TEAM_CODE_FOR_PYB = {
    "ATH": "OAK",
}

TEAM_CODE_NORMALIZATION = {
    "KC": "KCR",
    "TB": "TBR",
    "SD": "SDP",
    "SF": "SFG",
    "WSH": "WSN",
    "CWS": "CHW",
    "AZ": "ARI",
}

STADIUMS = {
    "NYY": (40.8296, -73.9262, False),
    "BOS": (42.3467, -71.0972, False),
    "TBR": (27.7684, -82.6534, True),
    "CIN": (39.0975, -84.5071, False),
    "LAD": (34.0739, -118.2400, False),
    "STL": (38.6226, -90.1928, False),
    "SFG": (37.7786, -122.3893, False),
    "PHI": (39.9061, -75.1665, False),
    "ARI": (33.4453, -112.0667, True),
    "ATL": (33.8911, -84.4683, False),
    "BAL": (39.2842, -76.6216, False),
    "CHC": (41.9484, -87.6557, False),
    "CHW": (41.8299, -87.6339, False),
    "CLE": (41.4962, -81.6852, False),
    "COL": (39.7559, -104.9942, False),
    "DET": (42.3392, -83.0485, False),
    "HOU": (29.7573, -95.3559, True),
    "KCR": (39.0517, -94.4803, False),
    "LAA": (33.8003, -117.8827, False),
    "MIA": (25.7783, -80.2198, True),
    "MIL": (43.0284, -87.9712, True),
    "MIN": (44.9817, -93.2778, False),
    "NYM": (40.7571, -73.8458, False),
    "ATH": (38.5804, -121.5138, False),
    "PIT": (40.4473, -80.0060, False),
    "SDP": (32.7076, -117.1570, False),
    "SEA": (47.5914, -122.3323, True),
    "TEX": (32.7512, -97.0832, True),
    "TOR": (43.6414, -79.3894, True),
    "WSN": (38.8730, -77.0074, False),
}

PARK_FACTOR_RUNS = {
    "ARI": 1.00,
    "ATL": 1.02,
    "ATH": 0.97,
    "BAL": 0.95,
    "BOS": 1.03,
    "CHC": 1.02,
    "CHW": 1.01,
    "CIN": 1.07,
    "CLE": 0.98,
    "COL": 1.18,
    "DET": 0.96,
    "HOU": 0.99,
    "KCR": 0.99,
    "LAA": 0.99,
    "LAD": 0.97,
    "MIA": 0.95,
    "MIL": 1.00,
    "MIN": 1.01,
    "NYM": 0.98,
    "NYY": 1.03,
    "PHI": 1.02,
    "PIT": 0.95,
    "SDP": 0.94,
    "SEA": 0.96,
    "SFG": 0.94,
    "STL": 0.97,
    "TBR": 0.96,
    "TEX": 1.04,
    "TOR": 1.00,
    "WSN": 1.00,
}

PARK_FACTOR_HR = {
    "ARI": 1.00,
    "ATL": 1.08,
    "ATH": 0.95,
    "BAL": 0.93,
    "BOS": 1.05,
    "CHC": 1.04,
    "CHW": 1.02,
    "CIN": 1.18,
    "CLE": 0.99,
    "COL": 1.27,
    "DET": 0.90,
    "HOU": 0.98,
    "KCR": 0.92,
    "LAA": 0.98,
    "LAD": 0.94,
    "MIA": 0.88,
    "MIL": 1.01,
    "MIN": 1.05,
    "NYM": 0.96,
    "NYY": 1.18,
    "PHI": 1.11,
    "PIT": 0.89,
    "SDP": 0.86,
    "SEA": 0.92,
    "SFG": 0.82,
    "STL": 0.95,
    "TBR": 0.90,
    "TEX": 1.15,
    "TOR": 1.02,
    "WSN": 1.00,
}

STADIUM_ALTITUDE_M = {
    "COL": 1609.0,
    "ARI": 331.0,
    "ATL": 320.0,
    "ATH": 9.0,
    "BAL": 8.0,
    "BOS": 6.0,
    "CHC": 181.0,
    "CHW": 181.0,
    "CIN": 149.0,
    "CLE": 199.0,
    "DET": 183.0,
    "HOU": 13.0,
    "KCR": 274.0,
    "LAA": 47.0,
    "LAD": 105.0,
    "MIA": 2.0,
    "MIL": 194.0,
    "MIN": 264.0,
    "NYM": 8.0,
    "NYY": 8.0,
    "PHI": 12.0,
    "PIT": 225.0,
    "SDP": 20.0,
    "SEA": 18.0,
    "SFG": 3.0,
    "STL": 142.0,
    "TBR": 6.0,
    "TEX": 154.0,
    "TOR": 76.0,
    "WSN": 9.0,
}


def atomic_write_json(path: Path, payload: Any) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=4)
    os.replace(tmp_path, path)


def load_json_file(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def normalize_player_name(name: str) -> str:
    text = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode("ascii")
    text = text.lower().strip()
    chars = []
    for ch in text:
        chars.append(ch if (ch.isalnum() or ch == " ") else " ")
    return " ".join("".join(chars).split())


def normalize_team_code(team_code: str) -> str:
    t = str(team_code or "").strip().upper()
    return TEAM_CODE_NORMALIZATION.get(t, t)


def normalize_team_for_pyb(team_code: str) -> str:
    normalized = normalize_team_code(team_code)
    return TEAM_CODE_FOR_PYB.get(normalized, normalized)


def _resolve_col(df: pd.DataFrame, candidates: list[str]) -> Optional[str]:
    for col in candidates:
        if col in df.columns:
            return col
    return None


def _weighted_avg(values: pd.Series, weights: pd.Series) -> float:
    if values.empty:
        return 0.0
    w = pd.to_numeric(weights, errors="coerce").fillna(0.0)
    x = pd.to_numeric(values, errors="coerce").fillna(0.0)
    if float(w.sum()) <= 0:
        return float(x.mean())
    return float((x * w).sum() / w.sum())


def _safe_get_json(url: str) -> Any:
    response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def convert_ip_to_outs(ip: Any) -> int:
    try:
        ip_float = float(ip)
    except Exception:
        return 0
    innings = int(ip_float)
    partial = int(round((ip_float - innings) * 10))
    return int((innings * 3) + partial)


def build_pitch_baselines(data: pd.DataFrame) -> dict[str, dict[str, float]]:
    baselines: dict[str, dict[str, float]] = {}
    if data.empty or "pitch_name" not in data.columns:
        return baselines

    grouped = data.groupby("pitch_name")
    for pitch_name, group in grouped:
        pitch_entry: dict[str, float] = {}
        for metric in PITCH_METRICS:
            if metric not in group.columns:
                continue
            series = pd.to_numeric(group[metric], errors="coerce").dropna()
            if series.empty:
                continue

            mean_val = float(series.mean())
            std_val = float(series.std(ddof=0))
            sigma_floor = DEFAULT_SIGMA_BY_METRIC.get(metric, 1.0) * 0.55
            sigma_val = max(sigma_floor, std_val if pd.notna(std_val) else sigma_floor)

            pitch_entry[metric] = round(mean_val, 3)
            pitch_entry[f"{metric}_sigma"] = round(sigma_val, 3)

        if (
            "release_speed" in pitch_entry
            and "release_pos_z" in pitch_entry
            and "release_pos_x" in pitch_entry
        ):
            baselines[str(pitch_name)] = pitch_entry
    return baselines


def fetch_today_matchups(today_str: str) -> dict[str, dict[str, Any]]:
    url = f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={today_str}"
    try:
        payload = _safe_get_json(url)
    except Exception:
        return {}
    matchups: dict[str, dict[str, Any]] = {}

    for date_block in payload.get("dates", []):
        for game in date_block.get("games", []):
            game_pk = safe_int(game.get("gamePk"), 0)
            teams = game.get("teams", {})
            home = normalize_team_code(teams.get("home", {}).get("team", {}).get("abbreviation", ""))
            away = normalize_team_code(teams.get("away", {}).get("team", {}).get("abbreviation", ""))
            if not home or not away:
                continue

            matchups[home] = {
                "opponent_team": away,
                "home_away": "home",
                "venue_team": home,
                "game_pk": game_pk,
            }
            matchups[away] = {
                "opponent_team": home,
                "home_away": "away",
                "venue_team": home,
                "game_pk": game_pk,
            }
    return matchups


def fetch_home_plate_umpire(game_pk: int, umpire_cache: dict[int, str]) -> str:
    if game_pk in umpire_cache:
        return umpire_cache[game_pk]
    if game_pk <= 0:
        return "Unknown"

    url = f"https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live"
    try:
        payload = _safe_get_json(url)
        live_data = payload.get("liveData", {})
        officials = live_data.get("boxscore", {}).get("officials", [])
        if not officials:
            officials = payload.get("gameData", {}).get("officials", [])
        for official in officials:
            official_type = str(official.get("officialType", "")).lower()
            if "home plate" in official_type:
                name = official.get("official", {}).get("fullName", "Unknown")
                umpire_cache[game_pk] = str(name)
                return str(name)
    except Exception:
        pass
    umpire_cache[game_pk] = "Unknown"
    return "Unknown"


def load_umpire_overrides() -> dict[str, float]:
    try:
        from backend.scrapers.umpire_scraper import UmpireScraper
        scraper = UmpireScraper()
        return scraper.fetch_tendencies()
    except Exception as e:
        logger.warning(f"Failed to initialize UmpireScraper: {e}")
        # Fallback inline
        payload = load_json_file(UMPIRE_OVERRIDES_PATH, {})
        overrides: dict[str, float] = {}
        if isinstance(payload, dict):
            for k, v in payload.items():
                overrides[normalize_player_name(str(k))] = clamp(safe_float(v, 0.50), 0.0, 1.0)
        return overrides


def fetch_team_offense_context(start_date: str, end_date: str) -> dict[str, dict[str, float]]:
    try:
        df = pyb.batting_stats_range(start_date, end_date, qual=0)
    except Exception:
        return {}
    if df.empty:
        return {}

    team_col = _resolve_col(df, ["Team", "Tm"])
    if not team_col:
        return {}
    pa_col = _resolve_col(df, ["PA"])
    obp_col = _resolve_col(df, ["OBP"])
    k_col = _resolve_col(df, ["K%", "SO%"])
    bb_col = _resolve_col(df, ["BB%"])
    wrc_col = _resolve_col(df, ["wRC+"])
    ppa_col = _resolve_col(df, ["P/PA", "Pit/PA", "Pitches/PA"])

    if pa_col:
        weights = pd.to_numeric(df[pa_col], errors="coerce").fillna(0.0)
    else:
        weights = pd.Series([1.0] * len(df), index=df.index)

    output: dict[str, dict[str, float]] = {}
    for team_code, group in df.groupby(team_col):
        t = normalize_team_code(str(team_code))
        g_weights = weights.loc[group.index]

        obp = _weighted_avg(group[obp_col], g_weights) if obp_col else 0.320
        k_pct = _weighted_avg(group[k_col], g_weights) if k_col else 0.22
        bb_pct = _weighted_avg(group[bb_col], g_weights) if bb_col else 0.08
        wrc_plus = _weighted_avg(group[wrc_col], g_weights) if wrc_col else 100.0
        p_pa = _weighted_avg(group[ppa_col], g_weights) if ppa_col else 3.9

        if k_pct > 1.5:
            k_pct = k_pct / 100.0
        if bb_pct > 1.5:
            bb_pct = bb_pct / 100.0

        output[t] = {
            "obp": round(float(obp), 3),
            "k_pct": round(float(k_pct), 4),
            "bb_pct": round(float(bb_pct), 4),
            "wrc_plus": round(float(wrc_plus), 2),
            "pitches_per_pa": round(float(p_pa), 3),
            "lineup_handedness_balance": 0.50,
            "pinch_hit_risk": 0.10,
        }
    return output


def fetch_recent_bullpen_data() -> pd.DataFrame:
    today = datetime.now()
    try:
        return pyb.pitching_stats_range(
            (today - timedelta(days=3)).strftime("%Y-%m-%d"),
            (today - timedelta(days=1)).strftime("%Y-%m-%d"),
            qual=0,
        )
    except Exception:
        return pd.DataFrame()


def build_bullpen_context_by_team(recent_stats: pd.DataFrame) -> dict[str, dict[str, Any]]:
    if recent_stats.empty:
        return {}
    team_col = _resolve_col(recent_stats, ["Team", "Tm"])
    g_col = _resolve_col(recent_stats, ["G"])
    pitch_col = _resolve_col(recent_stats, ["Pitches", "Pit", "NP"])
    ip_col = _resolve_col(recent_stats, ["IP"])
    if not team_col:
        return {}

    output: dict[str, dict[str, Any]] = {}
    for team, group in recent_stats.groupby(team_col):
        t = normalize_team_code(str(team))
        heavy_use = 0
        if g_col:
            heavy_use = int((pd.to_numeric(group[g_col], errors="coerce").fillna(0) >= 2).sum())

        if pitch_col:
            pitch_proxy = pd.to_numeric(group[pitch_col], errors="coerce").fillna(0.0)
        elif ip_col:
            pitch_proxy = pd.to_numeric(group[ip_col], errors="coerce").fillna(0.0) * 15.0
        else:
            pitch_proxy = pd.Series([0.0] * len(group), index=group.index)

        top3 = float(pitch_proxy.sort_values(ascending=False).head(3).sum())
        fatigue_score = clamp((heavy_use / 6.0) + (top3 / 220.0), 0.0, 1.5)

        if fatigue_score >= 0.95:
            status = "Gassed"
        elif fatigue_score >= 0.55:
            status = "Taxed"
        else:
            status = "Fresh"

        output[t] = {
            "bullpen_status": status,
            "bullpen_fatigue_score": round(fatigue_score, 3),
            "top3_relief_pitch_proxy": round(top3, 1),
            "heavy_use_arms": heavy_use,
        }
    return output


def get_weather_context(venue_team: str) -> dict[str, Any]:
    lat_lon = STADIUMS.get(venue_team)
    if not lat_lon:
        return {
            "weather_impact": "Normal",
            "temperature_c": None,
            "humidity_pct": None,
            "wind_speed_kph": None,
            "wind_direction_deg": None,
            "weather_fatigue_risk": 0.50,
            "dynamic_park_factor": 1.0,
        }

    lat, lon, is_dome = lat_lon
    if is_dome:
        return {
            "weather_impact": "Neutral (Dome)",
            "temperature_c": None,
            "humidity_pct": None,
            "wind_speed_kph": None,
            "wind_direction_deg": None,
            "weather_fatigue_risk": 0.25,
            "dynamic_park_factor": 1.0,
        }

    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&current=temperature_2m,relative_humidity_2m,wind_speed_10m,wind_direction_10m"
    )
    try:
        payload = _safe_get_json(url)
        current = payload.get("current", {})
        temp_c = safe_float(current.get("temperature_2m"), 22.0)
        humidity = safe_float(current.get("relative_humidity_2m"), 55.0)
        wind_speed_kph = safe_float(current.get("wind_speed_10m"), 10.0)
        wind_dir = safe_float(current.get("wind_direction_10m"), 0.0)

        # Wind blowing from W/S (roughly 135 to 315 degrees) usually blows OUT in many stadiums
        # Wind blowing from E/N (0-135, 315-360) usually blows IN
        is_wind_out = 135.0 <= wind_dir <= 315.0
        w_out_kph = wind_speed_kph if is_wind_out else 0.0
        w_in_kph = wind_speed_kph if not is_wind_out else 0.0

        # Adjust temp/humidity components
        temp_component = clamp((temp_c - 22.0) / 12.0, 0.0, 1.0)
        humidity_component = clamp((humidity - 55.0) / 35.0, 0.0, 1.0)
        
        # Wind out = more HRs = pitcher fatigue quicker (leash gets shorter). 
        # Wind in = fewer HRs = pitcher can stay longer (fatigue risk goes down).
        if is_wind_out:
            wind_component = clamp((wind_speed_kph - 10.0) / 25.0, 0.0, 1.0)
        else:
            wind_component = -1 * clamp((wind_speed_kph - 10.0) / 25.0, 0.0, 1.0)

        fatigue_risk = clamp(
            (0.55 * temp_component) + (0.25 * humidity_component) + (0.20 * wind_component),
            0.0,
            1.5,
        )

        # Dynamic Park Factor Multiplier
        dpf = 1.0
        if w_out_kph > 24: # >15mph
            dpf = 1.10
        elif w_out_kph > 16: # >10mph
            dpf = 1.06
        elif w_in_kph > 16:
            dpf = 0.94
            
        if temp_c < 7.2: # < 45F
            dpf *= 0.93
        elif temp_c < 12.7: # < 55F
            dpf *= 0.97
        elif temp_c > 29.4: # > 85F
            dpf *= 1.03

        if fatigue_risk >= 0.90:
            impact = "High Fatigue Risk"
        elif temp_c <= 8:
            impact = "Low Carry / Cold"
        elif w_in_kph > 16:
            impact = "Wind Blowing In"
        elif w_out_kph > 16:
            impact = "Wind Blowing Out"
        else:
            impact = "Normal"

        return {
            "weather_impact": impact,
            "temperature_c": round(temp_c, 1),
            "humidity_pct": round(humidity, 1),
            "wind_speed_kph": round(wind_speed_kph, 1),
            "wind_direction_deg": round(wind_dir, 1),
            "weather_fatigue_risk": round(fatigue_risk, 3),
            "dynamic_park_factor": round(dpf, 3),
        }
    except Exception:
        return {
            "weather_impact": "Normal",
            "temperature_c": None,
            "humidity_pct": None,
            "wind_speed_kph": None,
            "wind_direction_deg": None,
            "weather_fatigue_risk": 0.50,
            "dynamic_park_factor": 1.0,
        }


def load_tier_overrides() -> tuple[dict[str, int], dict[str, int]]:
    payload = load_json_file(TIER_OVERRIDES_PATH, {})
    by_name = dict(DEFAULT_TIER_OVERRIDES_BY_NAME)
    by_id: dict[str, int] = {}

    if isinstance(payload, dict):
        if isinstance(payload.get("by_name"), dict):
            for k, v in payload["by_name"].items():
                by_name[normalize_player_name(str(k))] = safe_int(v, 3)
        if isinstance(payload.get("by_id"), dict):
            for k, v in payload["by_id"].items():
                by_id[str(k)] = safe_int(v, 3)
        if "by_name" not in payload and "by_id" not in payload:
            # Flat payload fallback.
            for k, v in payload.items():
                key = str(k)
                if key.isdigit():
                    by_id[key] = safe_int(v, 3)
                else:
                    by_name[normalize_player_name(key)] = safe_int(v, 3)
    return by_name, by_id


def _extract_pitching_row_metrics(season_row: Optional[pd.Series], starts_sampled: int) -> dict[str, float]:
    if season_row is None:
        return {
            "fip": 4.00,
            "hr_per_9": 1.10,
            "lob_pct": 72.0,
            "bb_pct": 8.0,
            "k_pct": 22.0,
            "avg_outs_per_start": 15.0,
        }

    fip = safe_float(season_row.get("FIP"), 4.00)
    hr9 = safe_float(season_row.get("HR/9"), 1.10)
    lob = safe_float(season_row.get("LOB%"), 72.0)
    bb_pct = safe_float(season_row.get("BB%"), 8.0)
    k_pct = safe_float(season_row.get("K%"), 22.0)
    if bb_pct <= 1.5:
        bb_pct *= 100.0
    if k_pct <= 1.5:
        k_pct *= 100.0

    ip = safe_float(season_row.get("IP"), 0.0)
    gs = max(safe_int(season_row.get("GS"), starts_sampled), 1)
    avg_outs_per_start = (convert_ip_to_outs(ip) / gs) if ip > 0 else 15.0

    return {
        "fip": round(fip, 3),
        "hr_per_9": round(hr9, 3),
        "lob_pct": round(lob, 2),
        "bb_pct": round(bb_pct, 2),
        "k_pct": round(k_pct, 2),
        "avg_outs_per_start": round(avg_outs_per_start, 2),
    }


def _build_statcast_advanced_context(
    data: pd.DataFrame,
    season_metrics: dict[str, float],
    pitch_hand: str,
) -> dict[str, float]:
    total_pitches = len(data)
    if total_pitches <= 0:
        return {
            "pitches_per_pa": 3.9,
            "pitches_per_ip": 16.0,
            "f_strike_pct": 58.0,
            "csw_pct": 28.0,
            "zone_pct": 48.0,
            "o_swing_pct": 30.0,
            "swstr_pct": 12.0,
            "z_contact_pct": 84.0,
            "behind_pct": 31.0,
            "gb_pct": 43.0,
            "ld_pct": 22.0,
            "fb_pct": 35.0,
            "babip": 0.300,
            "barrel_pct": 7.0,
            "hard_hit_pct": 38.0,
            "lob_pct": season_metrics.get("lob_pct", 72.0),
            "hr_per_9": season_metrics.get("hr_per_9", 1.10),
            "days_rest": 4.0,
            "rolling_pitch_count_3": 85.0,
            "rolling_pitch_count_5": 85.0,
            "rolling_pitch_count_10": 85.0,
            "season_max_pitch_count": 95.0,
            "velocity_decay_4_6": 0.0,
            "fip": season_metrics.get("fip", 4.0),
            "manager_hook_score": 0.50,
            "ttto_penalty": 0.50,
            "expected_outs_baseline": season_metrics.get("avg_outs_per_start", 15.0),
            "pitch_hand": pitch_hand,
        }

    work = data.copy()
    if "description" in work.columns:
        work["description"] = work["description"].astype(str).str.lower()
    else:
        work["description"] = ""

    if "game_date" in work.columns:
        work["game_date"] = pd.to_datetime(work["game_date"], errors="coerce")
    else:
        work["game_date"] = pd.NaT

    if "game_pk" in work.columns and "at_bat_number" in work.columns:
        work["ab_key"] = (
            work["game_pk"].astype(str)
            + "_"
            + pd.to_numeric(work["at_bat_number"], errors="coerce").fillna(-1).astype(int).astype(str)
        )
    elif "at_bat_number" in work.columns:
        work["ab_key"] = (
            work["game_date"].astype(str)
            + "_"
            + pd.to_numeric(work["at_bat_number"], errors="coerce").fillna(-1).astype(int).astype(str)
        )
    else:
        work["ab_key"] = work.index.astype(str)

    pitch_order_col = "pitch_number" if "pitch_number" in work.columns else None
    sort_cols = ["game_date", "ab_key"] + ([pitch_order_col] if pitch_order_col else [])
    first_pitches = work.sort_values(sort_cols).groupby("ab_key").head(1)

    total_pas = max(first_pitches.shape[0], 1)
    pitches_per_pa = total_pitches / total_pas

    first_desc = first_pitches["description"]
    f_strike_pct = 100.0 * float(first_desc.isin(FIRST_STRIKE_EVENTS).mean())

    desc = work["description"]
    called_strikes = desc.isin(CALLED_STRIKE_EVENTS)
    whiffs = desc.isin(WHIFF_EVENTS)
    swings = desc.isin(SWING_EVENTS)
    contacts = desc.isin(CONTACT_EVENTS)

    csw_pct = 100.0 * float((called_strikes | whiffs).mean())
    swstr_pct = 100.0 * float(whiffs.mean())

    zone = pd.to_numeric(work.get("zone"), errors="coerce")
    valid_zone = zone.notna()
    in_zone = valid_zone & zone.between(1, 9)
    out_zone = valid_zone & (~zone.between(1, 9))
    zone_pct = 100.0 * float(in_zone.mean()) if valid_zone.any() else 48.0

    out_zone_count = max(int(out_zone.sum()), 1)
    in_zone_swings = swings & in_zone
    z_swings_count = max(int(in_zone_swings.sum()), 1)

    o_swing_pct = 100.0 * float((swings & out_zone).sum() / out_zone_count)
    z_contact_pct = 100.0 * float((contacts & in_zone).sum() / z_swings_count)

    balls = pd.to_numeric(work.get("balls"), errors="coerce")
    strikes = pd.to_numeric(work.get("strikes"), errors="coerce")
    behind_pct = 100.0 * float((balls > strikes).mean()) if balls.notna().any() and strikes.notna().any() else 31.0

    bb_type = work.get("bb_type", pd.Series([], dtype="object")).astype(str).str.lower()
    bb_denom = max(int(bb_type.ne("nan").sum()), 1)
    gb_pct = 100.0 * float((bb_type == "ground_ball").sum() / bb_denom)
    ld_pct = 100.0 * float((bb_type == "line_drive").sum() / bb_denom)
    fb_pct = 100.0 * float((bb_type == "fly_ball").sum() / bb_denom)

    launch_speed = pd.to_numeric(work.get("launch_speed"), errors="coerce")
    launch_angle = pd.to_numeric(work.get("launch_angle"), errors="coerce")
    batted = launch_speed.notna() & launch_angle.notna()
    batted_denom = max(int(batted.sum()), 1)
    hard_hit_pct = 100.0 * float(((launch_speed >= 95.0) & batted).sum() / batted_denom)

    barrels = (
        batted
        & (
            ((launch_speed >= 98.0) & (launch_angle >= 26.0) & (launch_angle <= 30.0))
            | ((launch_speed >= 100.0) & (launch_angle >= 24.0) & (launch_angle <= 33.0))
            | ((launch_speed >= 102.0) & (launch_angle >= 8.0) & (launch_angle <= 50.0))
        )
    )
    barrel_pct = 100.0 * float(barrels.sum() / batted_denom)

    in_play = desc.str.contains("hit_into_play", na=False)
    events = work.get("events", pd.Series([], dtype="object")).astype(str).str.lower()
    hits_in_play = int(events.isin({"single", "double", "triple"}).sum())
    outs_in_play = max(int(in_play.sum()) - hits_in_play, 0)
    babip = float(hits_in_play / max(hits_in_play + outs_in_play, 1))

    if "game_date" in work.columns:
        valid_games = work.dropna(subset=["game_date"]).copy()
        valid_games["game_day"] = valid_games["game_date"].dt.date
        game_pitch_counts = valid_games.groupby("game_day").size().sort_index()
    else:
        game_pitch_counts = pd.Series(dtype="int64")

    if not game_pitch_counts.empty:
        rolling_3 = float(game_pitch_counts.tail(3).mean())
        rolling_5 = float(game_pitch_counts.tail(5).mean())
        rolling_10 = float(game_pitch_counts.tail(10).mean())
        season_max_pitch_count = float(game_pitch_counts.max())
        starts_sampled = int(game_pitch_counts.shape[0])
        avg_pitch_count = float(game_pitch_counts.mean())
    else:
        rolling_3 = rolling_5 = rolling_10 = season_max_pitch_count = avg_pitch_count = 85.0
        starts_sampled = 1

    days_rest = 4.0
    if game_pitch_counts.shape[0] >= 2:
        latest_dates = list(game_pitch_counts.index[-2:])
        days_rest = float((latest_dates[-1] - latest_dates[-2]).days)

    release_speed = pd.to_numeric(work.get("release_speed"), errors="coerce")
    inning = pd.to_numeric(work.get("inning"), errors="coerce")
    early_velo = float(release_speed[inning.between(1, 3, inclusive="both")].mean()) if inning.notna().any() else float(release_speed.mean())
    mid_velo = float(release_speed[inning.between(4, 6, inclusive="both")].mean()) if inning.notna().any() else float(release_speed.mean())
    if pd.isna(early_velo):
        early_velo = float(release_speed.mean()) if release_speed.notna().any() else 94.0
    if pd.isna(mid_velo):
        mid_velo = early_velo
    velocity_decay = max(0.0, early_velo - mid_velo)

    avg_outs_per_start = season_metrics.get("avg_outs_per_start", 15.0)
    pitches_per_ip = avg_pitch_count / max(avg_outs_per_start / 3.0, 1.0)

    manager_hook_score = clamp(
        (0.55 * clamp((96.0 - avg_pitch_count) / 24.0, 0.0, 1.0))
        + (0.30 * clamp((106.0 - season_max_pitch_count) / 26.0, 0.0, 1.0))
        + (0.15 * clamp((4.0 - days_rest) / 4.0, 0.0, 1.0)),
        0.0,
        1.0,
    )
    ttto_penalty = clamp(
        (0.40 * clamp(velocity_decay / 2.5, 0.0, 1.0))
        + (0.30 * clamp((pitches_per_pa - 3.6) / 1.0, 0.0, 1.0))
        + (0.30 * clamp(hard_hit_pct / 52.0, 0.0, 1.0)),
        0.0,
        1.0,
    )

    expected_outs_baseline = float(avg_outs_per_start)
    if expected_outs_baseline <= 0:
        expected_outs_baseline = (avg_pitch_count / max(pitches_per_pa, 3.7)) * 0.72

    return {
        "pitches_per_pa": round(pitches_per_pa, 3),
        "pitches_per_ip": round(pitches_per_ip, 3),
        "f_strike_pct": round(f_strike_pct, 3),
        "csw_pct": round(csw_pct, 3),
        "zone_pct": round(zone_pct, 3),
        "o_swing_pct": round(o_swing_pct, 3),
        "swstr_pct": round(swstr_pct, 3),
        "z_contact_pct": round(z_contact_pct, 3),
        "behind_pct": round(behind_pct, 3),
        "gb_pct": round(gb_pct, 3),
        "ld_pct": round(ld_pct, 3),
        "fb_pct": round(fb_pct, 3),
        "babip": round(babip, 4),
        "barrel_pct": round(barrel_pct, 3),
        "hard_hit_pct": round(hard_hit_pct, 3),
        "lob_pct": season_metrics.get("lob_pct", 72.0),
        "hr_per_9": season_metrics.get("hr_per_9", 1.10),
        "days_rest": round(days_rest, 2),
        "rolling_pitch_count_3": round(rolling_3, 2),
        "rolling_pitch_count_5": round(rolling_5, 2),
        "rolling_pitch_count_10": round(rolling_10, 2),
        "season_max_pitch_count": round(season_max_pitch_count, 2),
        "velocity_decay_4_6": round(velocity_decay, 3),
        "fip": season_metrics.get("fip", 4.00),
        "manager_hook_score": round(manager_hook_score, 3),
        "ttto_penalty": round(ttto_penalty, 3),
        "expected_outs_baseline": round(expected_outs_baseline, 3),
        "pitch_hand": pitch_hand,
        "starts_sampled": starts_sampled,
        "avg_pitch_count": round(avg_pitch_count, 2),
        "early_fastball_v": round(early_velo, 3),
    }


def get_pitcher_baseline_profile_and_context(
    name: str,
    season_lookup: dict[str, pd.Series],
) -> tuple[Optional[dict[str, dict[str, float]]], Optional[int], dict[str, float], dict[str, float], str]:
    first, last = name.split(" ", 1)
    lookup = pyb.playerid_lookup(last, first)
    if lookup.empty or "key_mlbam" not in lookup.columns:
        return None, None, {}, {}, "R"

    try:
        lookup_row = lookup.iloc[0]
        p_id = int(pd.to_numeric(lookup_row.get("key_mlbam"), errors="coerce"))
    except Exception:
        return None, None, {}, {}, "R"

    throws = str(lookup_row.get("throws", "R")).upper()[:1]
    if throws not in {"R", "L"}:
        throws = "R"

    end_date = datetime.now().strftime("%Y-%m-%d")
    data = pyb.statcast_pitcher(BASELINE_LOOKBACK_START, end_date, player_id=p_id)
    if data.empty:
        return None, p_id, {}, {}, throws

    baselines = build_pitch_baselines(data)
    if not baselines:
        return None, p_id, {}, {}, throws

    name_key = normalize_player_name(name)
    season_row = season_lookup.get(name_key)
    season_metrics = _extract_pitching_row_metrics(
        season_row,
        starts_sampled=int(pd.to_datetime(data["game_date"], errors="coerce").dt.date.nunique()),
    )

    advanced_context = _build_statcast_advanced_context(data, season_metrics, throws)

    fb_rows = data[data["pitch_name"].isin(FASTBALL_TYPES)] if "pitch_name" in data.columns else pd.DataFrame()
    if not fb_rows.empty and "release_speed" in fb_rows.columns:
        avg_fb_velo = float(pd.to_numeric(fb_rows["release_speed"], errors="coerce").dropna().mean())
    else:
        avg_fb_velo = float(pd.to_numeric(data.get("release_speed"), errors="coerce").dropna().mean())
    if pd.isna(avg_fb_velo):
        avg_fb_velo = 93.5

    profile = {
        "starts_sampled": int(advanced_context.get("starts_sampled", 0)),
        "total_pitches_sampled": int(len(data)),
        "avg_pitches_per_game": round(safe_float(advanced_context.get("avg_pitch_count"), 85.0), 2),
        "avg_fastball_v": round(avg_fb_velo, 2),
        "avg_outs_per_start": round(safe_float(season_metrics.get("avg_outs_per_start"), 15.0), 2),
        "pitch_hand": throws,
    }
    return baselines, p_id, profile, advanced_context, throws


def assign_pitcher_tier(
    profile: dict[str, float],
    advanced_context: dict[str, float],
    override_tier: Optional[int] = None,
    override_reason: Optional[str] = None,
) -> tuple[int, str]:
    if override_tier in {1, 2, 3}:
        return int(override_tier), f"override_{override_reason or 'manual'}"

    avg_pitches = safe_float(profile.get("avg_pitches_per_game"), 85.0)
    avg_fb_velo = safe_float(profile.get("avg_fastball_v"), 93.5)
    season_max = safe_float(advanced_context.get("season_max_pitch_count"), avg_pitches + 8.0)
    hook_score = safe_float(advanced_context.get("manager_hook_score"), 0.50)
    fip = safe_float(advanced_context.get("fip"), 4.00)
    days_rest = safe_float(advanced_context.get("days_rest"), 4.0)
    swstr = safe_float(advanced_context.get("swstr_pct"), 12.0)

    score = 0.0
    if avg_pitches >= 95:
        score += 2.0
    elif avg_pitches >= 90:
        score += 1.5
    elif avg_pitches >= 85:
        score += 1.0

    if season_max >= 105:
        score += 1.2
    elif season_max >= 98:
        score += 0.8
    elif season_max >= 92:
        score += 0.4

    if hook_score <= 0.32:
        score += 1.0
    elif hook_score <= 0.48:
        score += 0.6
    elif hook_score >= 0.70:
        score -= 0.6

    if fip <= 3.50:
        score += 0.6
    elif fip >= 4.60:
        score -= 0.5

    if avg_fb_velo >= 96.0:
        score += 0.5
    elif avg_fb_velo <= 91.5:
        score -= 0.4

    if swstr >= 13.0:
        score += 0.3
    if days_rest < 4.0:
        score -= 0.4

    if score >= 3.6:
        return 1, f"score_{score:.2f}_workhorse"
    if score >= 2.0:
        return 2, f"score_{score:.2f}_mid_rotation"
    return 3, f"score_{score:.2f}_short_leash"


def resolve_tier_override(
    pitcher_name: str,
    pitcher_id: int,
    by_name: dict[str, int],
    by_id: dict[str, int],
) -> tuple[Optional[int], Optional[str]]:
    pid_key = str(pitcher_id)
    if pid_key in by_id and by_id[pid_key] in {1, 2, 3}:
        return int(by_id[pid_key]), "id"
    name_key = normalize_player_name(pitcher_name)
    if name_key in by_name and by_name[name_key] in {1, 2, 3}:
        return int(by_name[name_key]), "name"
    return None, None


def load_existing_manifest() -> dict[str, Any]:
    payload = {}
    try:
        from backend.db import get_db
        db = get_db()
        res = db.table("mlb_model_state").select("state_value").eq("state_key", "manifest").execute()
        if res.data:
            payload = res.data[0]["state_value"]
    except Exception as e:
        # Fallback
        payload = load_json_file(MANIFEST_PATH, {})
    return payload if isinstance(payload, dict) else {}


def setup_daily_slate() -> None:
    existing_manifest = load_existing_manifest()
    today = datetime.now()
    today_str = today.strftime("%Y-%m-%d")

    print(f"Building manifest for {len(TODAY_STARTERS)} pitchers...")

    matchup_map = fetch_today_matchups(today_str)
    offense_start = (today - timedelta(days=OFFENSE_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    offense_end = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    team_offense_context = fetch_team_offense_context(offense_start, offense_end)
    bullpen_context_by_team = build_bullpen_context_by_team(fetch_recent_bullpen_data())
    umpire_overrides = load_umpire_overrides()
    umpire_cache: dict[int, str] = {}

    try:
        season_pitching = pyb.pitching_stats_range(BASELINE_LOOKBACK_START, today_str, qual=0)
    except Exception:
        season_pitching = pd.DataFrame()
    season_lookup: dict[str, pd.Series] = {}
    if isinstance(season_pitching, pd.DataFrame) and not season_pitching.empty and "Name" in season_pitching.columns:
        for _, row in season_pitching.iterrows():
            season_lookup[normalize_player_name(str(row.get("Name", "")))] = row

    tier_overrides_by_name, tier_overrides_by_id = load_tier_overrides()

    manifest: dict[str, Any] = {}
    tier_counts = {1: 0, 2: 0, 3: 0}

    for pitcher_name, pitcher_team in TODAY_STARTERS:
        try:
            pitcher_team = normalize_team_code(pitcher_team)
            baselines, pitcher_id, profile, advanced_context, pitch_hand = get_pitcher_baseline_profile_and_context(
                pitcher_name,
                season_lookup,
            )
            if not baselines or pitcher_id is None:
                print(f"SKIP {pitcher_name} - baseline unavailable")
                continue

            p_key = str(pitcher_id)
            old_payload = existing_manifest.get(p_key, {}) if isinstance(existing_manifest.get(p_key), dict) else {}
            saved_prop = safe_float(old_payload.get("prop_line"), DEFAULT_PROP_LINE)

            matchup = matchup_map.get(pitcher_team, {})
            opponent_team = normalize_team_code(matchup.get("opponent_team", "UNK"))
            venue_team = normalize_team_code(matchup.get("venue_team", pitcher_team))
            game_pk = safe_int(matchup.get("game_pk"), 0)

            umpire_name = fetch_home_plate_umpire(game_pk, umpire_cache)
            umpire_key = normalize_player_name(umpire_name)
            umpire_k_zone = safe_float(umpire_overrides.get(umpire_key, 0.50), 0.50)

            weather_ctx = get_weather_context(venue_team)
            
            base_park_factor = safe_float(PARK_FACTOR_RUNS.get(venue_team, 1.00), 1.00)
            base_hr_factor = safe_float(PARK_FACTOR_HR.get(venue_team, base_park_factor), base_park_factor)
            
            # Apply dynamic weather bot adjustments
            dpf = safe_float(weather_ctx.get("dynamic_park_factor", 1.0), 1.0)
            park_factor = base_park_factor * dpf
            park_hr_factor = base_hr_factor * dpf
            altitude_m = safe_float(STADIUM_ALTITUDE_M.get(venue_team, 100.0), 100.0)

            environment_context = {
                "venue_team": venue_team,
                "park_factor": round(park_factor, 3),
                "park_hr_factor": round(park_hr_factor, 3),
                "altitude_m": round(altitude_m, 1),
                "park_pitcher_friendliness": round(clamp((1.12 - park_factor) / 0.30, 0.0, 1.5), 3),
                "umpire_name": umpire_name,
                "umpire_k_zone": round(clamp(umpire_k_zone, 0.0, 1.0), 3),
                **weather_ctx,
            }

            opponent_ctx = team_offense_context.get(opponent_team, {})
            opponent_context = {
                "team": opponent_team,
                "obp": round(safe_float(opponent_ctx.get("obp"), 0.320), 3),
                "k_pct": round(safe_float(opponent_ctx.get("k_pct"), 0.22), 4),
                "bb_pct": round(safe_float(opponent_ctx.get("bb_pct"), 0.08), 4),
                "wrc_plus": round(safe_float(opponent_ctx.get("wrc_plus"), 100.0), 2),
                "pitches_per_pa": round(safe_float(opponent_ctx.get("pitches_per_pa"), 3.9), 3),
                "lineup_handedness_balance": round(safe_float(opponent_ctx.get("lineup_handedness_balance"), 0.50), 3),
                "pinch_hit_risk": round(safe_float(opponent_ctx.get("pinch_hit_risk"), 0.10), 3),
                "pitch_hand_adjustment_applied": pitch_hand,
            }

            bullpen_ctx = bullpen_context_by_team.get(
                pitcher_team,
                {"bullpen_status": "Fresh", "bullpen_fatigue_score": 0.35},
            )

            override_tier, override_reason = resolve_tier_override(
                pitcher_name,
                pitcher_id,
                tier_overrides_by_name,
                tier_overrides_by_id,
            )
            tier, tier_reason = assign_pitcher_tier(
                profile=profile,
                advanced_context=advanced_context,
                override_tier=override_tier,
                override_reason=override_reason,
            )
            tier_counts[tier] += 1

            line_movement = {}
            if (
                old_payload.get("slate_date") == today_str
                and isinstance(old_payload.get("line_movement"), dict)
            ):
                line_movement = dict(old_payload.get("line_movement", {}))
            if not line_movement:
                line_movement = {
                    "opening_line": round(saved_prop, 1),
                    "previous_line": round(saved_prop, 1),
                    "current_line": round(saved_prop, 1),
                    "line_move_delta": 0.0,
                    "line_move_abs": 0.0,
                    "last_move_delta": 0.0,
                    "book_count": 0,
                    "line_last_updated_utc": datetime.utcnow().isoformat(),
                }

            manifest[p_key] = {
                "name": pitcher_name,
                "team": pitcher_team,
                "opponent": opponent_team,
                "pitch_hand": pitch_hand,
                "baseline": baselines,
                "prop_line": round(saved_prop, 1),
                "line_movement": line_movement,
                "tier": tier,
                "tier_reason": tier_reason,
                "bullpen_status": bullpen_ctx.get("bullpen_status", "Fresh"),
                "bullpen_context": bullpen_ctx,
                "advanced_context": advanced_context,
                "opponent_context": opponent_context,
                "environment_context": environment_context,
                "matchup_context": {
                    "home_away": matchup.get("home_away", "unknown"),
                    "venue_team": venue_team,
                    "game_pk": game_pk,
                },
                "weather_impact": environment_context.get("weather_impact", "Normal"),
                "prediction": None,
                "slate_date": today_str,
                "starter_profile": profile,
                "manifest_updated_at_utc": datetime.utcnow().isoformat(),
            }
            print(
                f"OK {pitcher_name} | T{tier} ({tier_reason}) | "
                f"opp={opponent_team} | park={venue_team} | hook={advanced_context.get('manager_hook_score', 0):.2f}"
            )
        except Exception as exc:
            print(f"ERR {pitcher_name}: {exc}")

    try:
        from backend.db import get_db
        db = get_db()
        from datetime import datetime
        db.table("mlb_model_state").upsert({
            "state_key": "manifest",
            "state_value": manifest,
            "updated_at": datetime.utcnow().isoformat()
        }, on_conflict="state_key").execute()
    except Exception as e:
        atomic_write_json(MANIFEST_PATH, manifest)
    print("--- manifest.json created successfully ---")
    print(
        f"Tiers => T1:{tier_counts[1]} T2:{tier_counts[2]} T3:{tier_counts[3]} | "
        f"Pitchers in manifest: {len(manifest)}"
    )
    print(
        "Advanced context added: manager_hook, TTTO proxy, command, workload, "
        "opponent profile, park/weather/altitude, and umpire K-zone."
    )


if __name__ == "__main__":
    setup_daily_slate()
