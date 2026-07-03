import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from bs4 import BeautifulSoup

from backend.ml.mlb_quant.orchestrator import clamp, normalize_player_name, UMPIRE_OVERRIDES_PATH, load_json_file, safe_float

logger = logging.getLogger(__name__)

class UmpireScraper:
    """
    Scrapes live umpire tendencies (e.g. strike zone expansion) to replace static JSON.
    Falls back to umpire_overrides.json if scraping fails or is blocked.
    """
    
    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        }
        
    def fetch_tendencies(self) -> dict[str, float]:
        tendencies = {}
        
        # Strategy 1: Swish Analytics (Often blocked or URL changes)
        try:
            tendencies = self._scrape_swish()
            if tendencies:
                logger.info(f"Scraped {len(tendencies)} umpires from SwishAnalytics")
                return tendencies
        except Exception as e:
            logger.debug(f"SwishAnalytics scraper failed: {e}")
            
        # Strategy 2: UmpScorecards (Often blocked or JS rendered)
        try:
            tendencies = self._scrape_umpscorecards()
            if tendencies:
                logger.info(f"Scraped {len(tendencies)} umpires from UmpScorecards")
                return tendencies
        except Exception as e:
            logger.debug(f"UmpScorecards scraper failed: {e}")
            
        # Fallback: Static overrides
        logger.warning("Live umpire scraping failed. Falling back to static umpire_overrides.json")
        return self._load_static_overrides()

    def _scrape_swish(self) -> dict[str, float]:
        """Attempt to scrape Swish Analytics Umpire Factors"""
        url = "https://swishanalytics.com/mlb/mlb-umpire-factors"
        r = requests.get(url, headers=self.headers, timeout=10)
        r.raise_for_status()
        
        tables = pd.read_html(r.text)
        if not tables:
            return {}
            
        df = tables[0]
        # Swish typically has columns like 'Umpire', 'K%', 'BB%', 'Runs'
        # We want to map this to a 0.0 - 1.0 K-zone expansiveness score.
        tendencies = {}
        if "Umpire" in df.columns and "K%" in df.columns:
            # Normalize K% to a 0.0-1.0 scale where average is 0.50
            # Typical MLB K% is ~22%. 
            for _, row in df.iterrows():
                name = normalize_player_name(str(row["Umpire"]))
                k_pct = safe_float(row["K%"], 22.0)
                # Map 20% to 0.0, 24% to 1.0
                score = clamp((k_pct - 20.0) / 4.0, 0.0, 1.0)
                tendencies[name] = round(score, 3)
        return tendencies
        
    def _scrape_umpscorecards(self) -> dict[str, float]:
        """Attempt to scrape UmpScorecards Umpire stats"""
        url = "https://umpscorecards.com/umpires/"
        r = requests.get(url, headers=self.headers, timeout=10)
        r.raise_for_status()
        
        tables = pd.read_html(r.text)
        if not tables:
            return {}
            
        df = tables[0]
        tendencies = {}
        if "Umpire" in df.columns and "Accuracy" in df.columns:
            for _, row in df.iterrows():
                name = normalize_player_name(str(row["Umpire"]))
                # If they don't have K-rate, we use favorability/accuracy as a proxy or just 0.5
                acc = safe_float(row["Accuracy"], 94.0)
                score = clamp((acc - 92.0) / 4.0, 0.0, 1.0)
                tendencies[name] = round(score, 3)
        return tendencies

    def _load_static_overrides(self) -> dict[str, float]:
        payload = load_json_file(UMPIRE_OVERRIDES_PATH, {})
        overrides: dict[str, float] = {}
        if isinstance(payload, dict):
            for k, v in payload.items():
                overrides[normalize_player_name(str(k))] = clamp(safe_float(v, 0.50), 0.0, 1.0)
        return overrides
