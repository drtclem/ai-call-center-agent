"""City → zone, from the dataset's SCHEMA.md. Unknown cities (e.g. Manor, Dripping Springs:
in the service area since April 2026 but with no zone in the scheduler, I14) return None,
which means "hand off", never "guess a zone" (D10)."""

ZONES: dict[str, str] = {
    "round rock": "north",
    "cedar park": "north",
    "pflugerville": "north",
    "georgetown": "north",
    "leander": "north",
    "hutto": "north",
    "austin": "central",
    "kyle": "south",
    "buda": "south",
}


def zone_for(city: str) -> str | None:
    return ZONES.get(city.strip().lower())
