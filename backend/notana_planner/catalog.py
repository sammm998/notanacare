"""Intervention catalogue, skills and delegations used by the generator."""

from __future__ import annotations

from dataclasses import dataclass

# Delegations: formally delegated medical tasks (Swedish "delegering").
MEDICATION = "MEDICATION"
INSULIN = "INSULIN"
WOUND_CARE = "WOUND_CARE"
CATHETER = "CATHETER"
DELEGATIONS = (MEDICATION, INSULIN, WOUND_CARE, CATHETER)

# Skills / competences.
HOIST = "HOIST"  # lift / patient-hoist trained
DEMENTIA = "DEMENTIA"
PALLIATIVE = "PALLIATIVE"
SKILLS = (HOIST, DEMENTIA, PALLIATIVE)

# Language is a person attribute, not a skill (Finnish: minority-language right).
LANGUAGES = {
    "sv": "Swedish", "fi": "Finnish", "ar": "Arabic", "fa": "Persian", "so": "Somali",
    "bcs": "Bosnian/Croatian/Serbian", "es": "Spanish", "pl": "Polish", "en": "English",
}

# Interventions that make a visit "intimate care" (gender wishes usually apply here).
INTIMATE_TYPES = frozenset({
    "hygiene", "shower", "toileting", "dressing", "undressing", "catheter", "bedtime", "oral_care", "skin_care",
})

# Share of employees holding each qualification (before team-level noise).
EMPLOYEE_QUALIFICATION_RATES = {
    MEDICATION: 0.70,
    INSULIN: 0.45,  # conditional on MEDICATION
    WOUND_CARE: 0.16,
    CATHETER: 0.28,
    HOIST: 0.55,
    DEMENTIA: 0.45,
    PALLIATIVE: 0.15,
}


@dataclass(frozen=True, slots=True)
class InterventionType:
    key: str
    label: str
    min_duration: int
    max_duration: int
    priority: int
    slots: tuple[str, ...]
    delegations: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    time_critical: bool = False  # generates a hard window when included
    needs_transfer: bool = False  # candidate for double staffing


M, D, A, E = "morning", "midday", "afternoon", "evening"

INTERVENTION_TYPES: tuple[InterventionType, ...] = (
    InterventionType("wake_up", "Wake-up assistance", 4, 7, 3, (M,)),
    InterventionType("hygiene", "Personal hygiene", 5, 8, 3, (M, E)),
    InterventionType("dressing", "Dressing", 4, 8, 3, (M,)),
    InterventionType("breakfast", "Breakfast assistance", 5, 8, 3, (M,)),
    InterventionType("medication", "Medication administration", 4, 6, 5, (M, D, A, E), (MEDICATION,), time_critical=True),
    InterventionType("insulin", "Insulin injection", 5, 7, 5, (M, E), (MEDICATION, INSULIN), time_critical=True),
    InterventionType("eye_drops", "Eye drops", 4, 4, 4, (M, E), (MEDICATION,), time_critical=True),
    InterventionType("compression", "Compression stockings", 4, 6, 3, (M, E)),
    InterventionType("transfer", "Mobility / transfer assistance", 4, 8, 4, (M, D, A, E), needs_transfer=True),
    InterventionType("shower", "Shower assistance", 8, 10, 3, (M, A), needs_transfer=True),
    InterventionType("toileting", "Toileting assistance", 4, 6, 4, (M, D, A, E)),
    InterventionType("catheter", "Catheter care", 4, 7, 4, (M, E), (CATHETER,)),
    InterventionType("bed_making", "Bed making", 4, 6, 2, (M, E)),
    InterventionType("oral_care", "Oral care", 4, 6, 3, (M, E)),
    InterventionType("skin_care", "Skin care / lotion", 4, 6, 3, (M, E)),
    InterventionType("wellbeing_check", "Health / wellbeing check", 4, 6, 3, (M, D)),
    InterventionType("lunch", "Lunch assistance", 6, 9, 3, (D,)),
    InterventionType("meal_heating", "Meal preparation", 4, 7, 3, (D, E)),
    InterventionType("fluids", "Fluid / nutrition check", 4, 5, 3, (D, A)),
    InterventionType("dishes", "Dishes / kitchen", 4, 7, 1, (D, E)),
    InterventionType("social", "Social contact / supervision", 4, 8, 2, (D, A)),
    InterventionType("walk", "Walk / exercise", 6, 8, 2, (D, A)),
    InterventionType("snack", "Afternoon snack / coffee", 4, 6, 2, (A,)),
    InterventionType("cleaning", "Cleaning", 6, 10, 1, (A,)),
    InterventionType("laundry", "Laundry", 4, 8, 1, (A,)),
    InterventionType("shopping", "Shopping list / errands", 4, 7, 1, (A, D)),
    InterventionType("wound_care", "Wound care", 7, 10, 4, (A, D, M), (WOUND_CARE,)),
    InterventionType("safety_check", "Safety check / alarm test", 4, 5, 2, (D, A, E)),
    InterventionType("dinner", "Dinner assistance", 6, 9, 3, (E,)),
    InterventionType("undressing", "Undressing", 4, 6, 3, (E,)),
    InterventionType("bedtime", "Bedtime assistance", 4, 8, 3, (E,)),
    InterventionType("evening_routine", "Evening routine / tidying", 4, 6, 2, (E,)),
    # Not generated as planned demand: created by live incidents.
    InterventionType("alarm_response", "Safety alarm response (trygghetslarm)", 15, 30, 5, ()),
)

INTERVENTION_BY_KEY = {t.key: t for t in INTERVENTION_TYPES}

# Generation-only slot definitions: [slot_start, slot_end] and preferred-time range.
SLOTS = {
    M: {"start": 6 * 60 + 30, "end": 10 * 60 + 30, "pref": (7 * 60, 9 * 60 + 30), "count": (8, 12)},
    D: {"start": 10 * 60 + 30, "end": 14 * 60, "pref": (10 * 60 + 45, 13 * 60 + 15), "count": (6, 9)},
    A: {"start": 14 * 60, "end": 17 * 60 + 30, "pref": (14 * 60 + 30, 16 * 60 + 30), "count": (6, 8)},
    E: {"start": 17 * 60 + 30, "end": 22 * 60, "pref": (18 * 60, 21 * 60), "count": (7, 11)},
}
SLOT_ORDER = (M, D, A, E)
