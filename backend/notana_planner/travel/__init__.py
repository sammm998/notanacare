"""Travel-time providers and matrix construction."""

from __future__ import annotations

import logging
import os

from ..domain import Location
from .base import BaseMatrix, TrafficConditions, TravelMatrix, TravelTimeProvider
from .synthetic import SyntheticTravelConfig, SyntheticTravelTimeProvider

log = logging.getLogger(__name__)

__all__ = [
    "BaseMatrix",
    "TrafficConditions",
    "TravelMatrix",
    "TravelTimeProvider",
    "SyntheticTravelConfig",
    "SyntheticTravelTimeProvider",
    "make_provider",
    "build_base_matrix",
]


def make_provider(preference: str = "auto") -> TravelTimeProvider:
    """Choose a provider. ``auto`` uses Google only if a key is configured."""
    preference = (preference or "auto").lower()
    if preference in ("auto", "google") and os.environ.get("GOOGLE_MAPS_API_KEY"):
        try:
            from .google_routes import GoogleRoutesTravelTimeProvider

            return GoogleRoutesTravelTimeProvider()
        except Exception as exc:  # noqa: BLE001
            log.warning("Google provider unavailable (%s); using synthetic", exc)
    return SyntheticTravelTimeProvider()


_MATRIX_CACHE: dict[tuple, BaseMatrix] = {}


def build_base_matrix(provider: TravelTimeProvider, locations: list[Location]) -> BaseMatrix:
    """In-process cache on top of whatever caching the provider does itself."""
    if getattr(provider, "memoize", True) is False:
        return provider.build_matrix(locations)
    key = (provider.name, tuple((loc.id, round(loc.lat, 6), round(loc.lon, 6)) for loc in locations))
    if key not in _MATRIX_CACHE:
        _MATRIX_CACHE[key] = provider.build_matrix(locations)
    return _MATRIX_CACHE[key]
