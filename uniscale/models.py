from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional, Tuple
from pydantic import BaseModel, Field


class DeviceType(str, Enum):
    YUNMAI = "yunmai"
    FITDAYS = "fitdays"
    ETEKCITY = "etekcity"
    VIATOM_BP2 = "viatom_bp2"
    OMRON = "omron"
    UNKNOWN = "unknown"


class WeightMeasurement(BaseModel):
    weight_kg: float
    weight_lb: float = Field(default=0.0)
    body_fat_percent: Optional[float] = None
    bmi: Optional[float] = None
    impedance: Optional[int] = None
    muscle_mass_lb: Optional[float] = None
    water_percent: Optional[float] = None
    bone_mass_lb: Optional[float] = None
    visceral_fat: Optional[int] = None
    bmr_kcal: Optional[int] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    device_name: str
    device_address: str

    def model_post_init(self, __context):
        if self.weight_lb == 0.0 and self.weight_kg > 0:
            self.weight_lb = round(self.weight_kg * 2.20462262, 1)


class BloodPressureMeasurement(BaseModel):
    systolic: int  # mmHg
    diastolic: int  # mmHg
    pulse: int  # bpm
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    device_name: str = "Blood Pressure Monitor"
    device_address: str = "00:00:00:00:00:00"


class EcgRecording(BaseModel):
    heart_rate: int = 0
    sample_rate_hz: int = 125
    samples_mv: List[float] = Field(default_factory=list)
    duration_seconds: float = 30.0
    rhythm_status: str = "Normal Sinus Rhythm"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    device_name: str = "ECG Monitor"
    device_address: str = "00:00:00:00:00:00"


class HrZoneTime(BaseModel):
    zone: int  # 1 = warm up ... 5 = maximum
    name: str = ""
    min_bpm: Optional[int] = None
    seconds: float = 0.0


class WorkoutSplit(BaseModel):
    distance_m: float
    seconds: float
    avg_hr: Optional[int] = None


class ActivityWorkout(BaseModel):
    sport: str = "generic"
    sub_sport: Optional[str] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    duration_seconds: float = 0.0
    distance_meters: float = 0.0
    distance_km: float = 0.0
    calories: Optional[int] = None
    avg_heart_rate: Optional[int] = None
    max_heart_rate: Optional[int] = None
    avg_cadence: Optional[int] = None
    elevation_gain_m: Optional[float] = None
    steps: Optional[int] = None
    avg_power: Optional[int] = None  # running/cycling power, W
    max_power: Optional[int] = None
    aerobic_training_effect: Optional[float] = None
    vo2max: Optional[float] = None
    device_name: str = "Garmin"
    source_file: Optional[str] = None
    # Watch extras (Gadgetbridge summary); all optional
    anaerobic_training_effect: Optional[float] = None
    training_load: Optional[float] = None
    recovery_time_hours: Optional[float] = None
    body_battery_start: Optional[int] = None
    body_battery_end: Optional[int] = None
    sweat_loss_ml: Optional[int] = None
    avg_temperature_c: Optional[float] = None
    ground_contact_ms: Optional[float] = None
    vertical_oscillation_mm: Optional[float] = None
    vertical_ratio_percent: Optional[float] = None
    stride_length_m: Optional[float] = None
    ground_contact_balance: Optional[float] = None  # Left % (e.g. 49.5 for 49.5% L / 50.5% R)
    max_cadence: Optional[int] = None
    elevation_loss_m: Optional[float] = None
    normalized_power: Optional[int] = None
    active_calories: Optional[int] = None
    start_lat: Optional[float] = None
    start_lon: Optional[float] = None
    end_lat: Optional[float] = None
    end_lon: Optional[float] = None
    gps_track: Optional[List[Tuple[float, float]]] = None  # [(lat, lon), ...]
    hr_zones: Optional[List[HrZoneTime]] = None
    splits: Optional[List[WorkoutSplit]] = None
    hr_samples: Optional[List[Tuple[int, int]]] = None  # (seconds from start, bpm); may run past the end
    # Heart rate recovery: bpm fallen 1 and 2 minutes after the workout ended
    hr_recovery_1min: Optional[int] = None
    hr_recovery_2min: Optional[int] = None

    def model_post_init(self, __context):
        if self.distance_km == 0.0 and self.distance_meters > 0:
            self.distance_km = round(self.distance_meters / 1000.0, 2)


class DailyActivity(BaseModel):
    date: str  # YYYY-MM-DD
    steps: int = 0
    distance_km: float = 0.0
    calories: Optional[int] = None
    active_minutes: Optional[int] = None
    resting_heart_rate: Optional[int] = None
    avg_heart_rate: Optional[int] = None
    min_heart_rate: Optional[int] = None
    max_heart_rate: Optional[int] = None
    avg_spo2: Optional[float] = None
    heart_points: Optional[int] = None
    # Garmin stress (0-100): the day's average and peak; Body Battery: the latest reading, the day's high and low
    avg_stress: Optional[int] = Field(default=None, ge=0, le=100)
    max_stress: Optional[int] = Field(default=None, ge=0, le=100)
    body_battery: Optional[int] = Field(default=None, ge=0, le=100)
    body_battery_high: Optional[int] = Field(default=None, ge=0, le=100)
    body_battery_low: Optional[int] = Field(default=None, ge=0, le=100)
    source: str = "garmin"


class LabMarker(BaseModel):
    name: str
    value: float
    unit: str = ""
    reference_range: Optional[str] = None
    flag: Optional[str] = None  # "H", "L", or "Normal"
    category: Optional[str] = None  # "lipid", "metabolic", "thyroid", "cbc", "other"


class BloodTestMeasurement(BaseModel):
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    lab_name: str = ""
    panel_name: str = "Comprehensive Metabolic & Lipid Panel"
    fasting: Optional[bool] = None
    markers: Dict[str, LabMarker] = Field(default_factory=dict)
    notes: Optional[str] = None
    type: str = "blood_test"


class SupplementItem(BaseModel):
    name: str
    dosage: float
    unit: str = "mg"
    category: str = "supplement"
    frequency: str = "daily"
    slots: Optional[List[str]] = None  # wake, breakfast, lunch, dinner, bed; [] = as needed
    purpose: str = ""


class SupplementIntake(BaseModel):
    name: str
    dosage: float
    unit: str = "mg"
    category: str = "supplement"  # supplement, medication, nootropic, vitamin, mineral
    frequency: str = "daily"      # daily, morning, evening, as_needed
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    notes: Optional[str] = None
    slot: Optional[str] = None  # which checklist slot this dose ticks off
    type: str = "supplement"


class SleepSession(BaseModel):
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    date: str  # YYYY-MM-DD
    duration_seconds: int = 0
    duration_hours: Optional[float] = None
    deep_sleep_seconds: int = 0
    deep_sleep_minutes: Optional[int] = None
    rem_sleep_seconds: int = 0
    rem_sleep_minutes: Optional[int] = None
    light_sleep_seconds: int = 0
    light_sleep_minutes: Optional[int] = None
    awake_seconds: int = 0
    awake_minutes: Optional[int] = None
    sleep_score: Optional[int] = None  # reported by the device (e.g. Garmin)
    estimated_sleep_score: Optional[int] = None  # computed by the phone app from stages
    quality_score: Optional[int] = None
    recovery_score: Optional[int] = None
    deep_score: Optional[int] = None
    rem_score: Optional[int] = None
    avg_spo2: Optional[float] = None
    lowest_spo2: Optional[float] = None
    avg_respiration: Optional[float] = None
    avg_sleep_stress: Optional[float] = None
    hrv_rmssd_ms: Optional[float] = None
    hrv_status: Optional[str] = None
    device_name: str = "Garmin"
    type: str = "sleep"


class GenomicVariant(BaseModel):
    rsid: str
    gene: str
    genotype: str
    trait: str
    clinical_significance: str
    recommendation: str
    category: str = "longevity"  # longevity, cardiovascular, pharmacogenomics, metabolism, fitness


class GenomicProfile(BaseModel):
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source: str = "Raw DNA Analysis"
    variants: Dict[str, GenomicVariant] = Field(default_factory=dict)
    summary: Dict[str, str] = Field(default_factory=dict)
    type: str = "genomics"

