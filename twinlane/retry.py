"""Validated retry settings. Malformed settings disable retries rather than widen them."""
import math


def settings(policy):
    raw = (policy or {}).get('retry', {'max': 2, 'backoff_s': [30, 90]})
    if not isinstance(raw, dict):
        return 0, []
    maximum, delays = raw.get('max', 2), raw.get('backoff_s', [30, 90])
    if type(maximum) is not int or maximum < 0 or not isinstance(delays, list) or not delays:
        return 0, []
    if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in delays):
        return 0, []
    return maximum, delays
