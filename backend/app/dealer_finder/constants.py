"""Shared constants for Dealer Finder."""

from __future__ import annotations

PLAN_VERSION = "v1-district-city-expand"

DEFAULT_CATEGORIES: list[str] = [
    "weighing scale dealers",
    "electronic weighing scale dealers",
    "label printing scale dealers",
    "digital weighing machine distributors",
    "retail billing scale suppliers",
    "industrial weighing scale suppliers",
    "weighbridge dealers",
]

# Job / unit statuses
STATUS_PENDING = "Pending"
STATUS_RUNNING = "Running"
STATUS_COMPLETED = "Completed"
STATUS_ALREADY_SEARCHED = "Already Searched"
STATUS_PARTIAL = "Partially Completed"
STATUS_FAILED = "Failed"
STATUS_PAUSED = "Paused"
STATUS_CANCELLED = "Cancelled"

COOLDOWN_PRESETS_DAYS = (7, 15, 30, 60, 90)

# Stage-1 (discovery) vs Stage-2 (Enterprise phone/website) field masks
FIELD_MASK_STAGE1 = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.addressComponents",
        "nextPageToken",
    ]
)
FIELD_MASK_STAGE2 = ",".join(
    [
        "places.id",
        "places.nationalPhoneNumber",
        "places.internationalPhoneNumber",
        "places.websiteUri",
    ]
)

ORG_DEFAULT = "yesweigh"
LANGUAGE_DEFAULT = "en"
COUNTRY_DEFAULT = "IN"
