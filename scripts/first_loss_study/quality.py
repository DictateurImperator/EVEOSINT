"""Validate derived ages without discarding the original observation."""

from datetime import datetime, timedelta, timezone
import math

# Deliberately earlier than EVE's launch/beta: Unix-epoch placeholders are not
# character birthdays. Do not invent a replacement birthday or age.
EARLIEST_BIRTHDAY = datetime(2000, 1, 1, tzinfo=timezone.utc)


def clean_features(features, loss_time):
    age = features.get("age_days")
    if age is None:
        return features, False
    try:
        valid = math.isfinite(float(age)) and float(age) >= 0
        birthday = loss_time - timedelta(days=float(age)) if valid else None
        valid = valid and birthday >= EARLIEST_BIRTHDAY
    except (ValueError, TypeError, OverflowError):
        valid = False
    if valid:
        return features, False
    result = dict(features)
    result.update(
        age_days=None,
        age_days_raw=age,
        age_quality="invalid_birthday",
        observed_since_birth=False,
    )
    return result, True
