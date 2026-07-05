import os
import sys
import logging

# Ensure backend paths are loaded
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.db import get_db

logger = logging.getLogger(__name__)

class WeatherMOS:
    """
    Model Output Statistics (MOS) Engine.
    Learns systematic biases in raw NWP (Numerical Weather Prediction) outputs
    by comparing historical forecasts against actual station readings.
    """
    def __init__(self):
        self.db = get_db()
        
    def calculate_bias(self, station_id: str, model_name: str, lead_time_days: int, metric: str = "high") -> float:
        """
        Calculates the historical bias for a specific station, model, and lead time.
        Positive bias means the model historically runs TOO WARM (Actual is lower than Predicted).
        Negative bias means the model historically runs TOO COOL (Actual is higher than Predicted).
        
        Returns the correction to apply (e.g., +1.2 means add 1.2 to the model's output).
        """
        # Fetch last 30 verifications to compute recent bias
        res = self.db.table("weather_verification").select("*") \
            .eq("station_id", station_id) \
            .eq("model_name", model_name) \
            .eq("lead_time_days", lead_time_days) \
            .order("target_date", desc=True) \
            .limit(30) \
            .execute()
            
        data = res.data or []
        if not data:
            return 0.0 # No history, assume 0 bias
            
        errors = []
        for row in data:
            if metric == "high" and row.get("predicted_high") and row.get("actual_high"):
                # Error = Actual - Predicted
                # If model predicted 90 but actual was 92, error is +2.0
                errors.append(row["actual_high"] - row["predicted_high"])
            elif metric == "low" and row.get("predicted_low") and row.get("actual_low"):
                errors.append(row["actual_low"] - row["predicted_low"])
                
        if not errors:
            return 0.0
            
        # Simple mean error (could be upgraded to a rolling regression)
        bias_correction = sum(errors) / len(errors)
        logger.info(f"MOS: {station_id} {model_name} at day {lead_time_days} has {metric} bias of {bias_correction:+.2f}°F")
        return bias_correction

# Singleton instance
mos_engine = WeatherMOS()
