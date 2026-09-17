"""Constants for the Sky Events integration."""

from __future__ import annotations

DOMAIN = "sky_events"

# One bus event per notable stage. This integration deliberately does not send
# notifications itself: it publishes state and announces stages, and the
# household decides what to do with them. That keeps recipient policy, quiet
# hours and transport out of a shared integration - see README.
EVENT_STAGE = f"{DOMAIN}_stage"

PLATFORMS = ["sensor"]

# Config-entry data
CONF_WEATHER_ENTITY = "weather_entity"
CONF_AURORA_PROBABILITY_ENTITY = "aurora_probability_entity"
CONF_AURORA_ALERT_ENTITY = "aurora_alert_entity"
CONF_DETAILS_PATH = "details_path"

# Options (all tunable without a restart)
CONF_CLOUD_LIMIT = "cloud_limit"
CONF_POSSIBLE_KP = "possible_kp"
CONF_PROMISING_KP = "promising_kp"
CONF_METEOR_MIN_ZHR = "meteor_min_zhr"
CONF_METEOR_MIN_RADIANT_ALTITUDE = "meteor_min_radiant_altitude"
CONF_METEOR_MOON_LIMIT = "meteor_moon_illumination_limit"
CONF_METEOR_WARN_DAYS = "meteor_warn_days"
CONF_MOONRISE_CORRIDOR_ENABLED = "moonrise_corridor_enabled"
CONF_MOONRISE_AZIMUTH_MIN = "moonrise_azimuth_min"
CONF_MOONRISE_AZIMUTH_MAX = "moonrise_azimuth_max"
CONF_STALE_HOURS = "stale_hours"

# Kp thresholds are meaningful only relative to GEOMAGNETIC latitude, so these
# defaults suit a mid-latitude site (~50 deg N) and should be raised further
# south and lowered further north. They are options rather than constants for
# exactly that reason.
DEFAULT_CLOUD_LIMIT = 60
DEFAULT_POSSIBLE_KP = 5.0
DEFAULT_PROMISING_KP = 6.0
DEFAULT_METEOR_MIN_ZHR = 15
DEFAULT_METEOR_MIN_RADIANT_ALTITUDE = 20
DEFAULT_METEOR_MOON_LIMIT = 60
DEFAULT_METEOR_WARN_DAYS = 60
DEFAULT_MOONRISE_AZIMUTH_MIN = 0
DEFAULT_MOONRISE_AZIMUTH_MAX = 360
DEFAULT_STALE_HOURS = 12

NOAA_KP_URL = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index-forecast.json"
NASA_ECLIPSE_SOURCE = "https://eclipse.gsfc.nasa.gov/"

SOLAR_SAFETY = (
    "Use certified solar-eclipse glasses or an approved solar filter. "
    "Regular sunglasses are not safe."
)

UPDATE_INTERVAL_MINUTES = 5
NOAA_REFRESH_HOURS = 3

# Nothing downstream uses an eclipse further out than the solar glasses
# reminder, so the local eclipse search only has to look this far ahead. The
# AppDaemon original searched a decade on every evaluation.
ECLIPSE_HORIZON_DAYS = 400

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.stages"
