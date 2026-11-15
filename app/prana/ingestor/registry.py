from typing import Dict, Optional, Tuple

# Mapping of (source, normalized_metric_name) -> (canonical_metric, canonical_unit)
CANONICAL_METRICS: Dict[Tuple[str, str], Tuple[str, str]] = {
    # Apple Health (Health Auto Export)
    ("apple_health", "heart_rate"): ("heart_rate", "bpm"),
    ("apple_health", "resting_heart_rate"): ("resting_heart_rate", "bpm"),
    ("apple_health", "walking_heart_rate_average"): ("walking_heart_rate", "bpm"),
    ("apple_health", "heart_rate_variability"): ("hrv_sdnn", "ms"),
    ("apple_health", "respiratory_rate"): ("respiratory_rate", "breaths/min"),
    ("apple_health", "vo2_max"): ("vo2_max", "ml/kg/min"),
    ("apple_health", "blood_oxygen_saturation"): ("spo2", "%"),
    ("apple_health", "step_count"): ("steps", "count"),
    ("apple_health", "sleep_analysis"): ("sleep_duration", "hr"),
    ("apple_health", "active_energy"): ("active_calories", "kcal"),
    ("apple_health", "basal_energy_burned"): ("basal_calories", "kcal"),
    ("apple_health", "apple_sleeping_wrist_temperature"): ("skin_temperature_delta", "degC"),
    ("apple_health", "apple_exercise_time"): ("exercise_minutes", "min"),
    ("apple_health", "weight_body_mass"): ("body_weight", "kg"),
    ("apple_health", "body_mass"): ("body_weight", "kg"),
    # Oura Ring BLE
    ("oura_ble", "heart_rate"): ("heart_rate", "bpm"),
    ("oura_ble", "resting_heart_rate"): ("resting_heart_rate", "bpm"),
    ("oura_ble", "hrv"): ("hrv_rmssd", "ms"),
    ("oura_ble", "hrv_rmssd"): ("hrv_rmssd", "ms"),
    ("oura_ble", "spo2"): ("spo2", "%"),
    ("oura_ble", "skin_temperature"): ("skin_temperature", "degC"),
    ("oura_ble", "skin_temperature_delta"): ("skin_temperature_delta", "degC"),
    ("oura_ble", "temperature_delta"): ("skin_temperature_delta", "degC"),
    ("oura_ble", "sleep_duration"): ("sleep_duration", "hr"),
    ("oura_ble", "steps"): ("steps", "count"),
    ("oura_ble", "activity_met"): ("activity_met", "MET"),
}


def resolve_canonical_metric(source: str, metric_name: str) -> Optional[Tuple[str, str]]:
    """Return (canonical_metric, canonical_unit) if known, else None."""
    key = (source.strip().lower(), metric_name.strip().lower())
    return CANONICAL_METRICS.get(key)
