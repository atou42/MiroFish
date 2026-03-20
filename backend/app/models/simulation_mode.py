from enum import Enum


class SimulationMode(str, Enum):
    """Supported simulation runtimes."""

    SOCIAL = "social"
    WORLD = "world"

    @classmethod
    def normalize(cls, value: str | None) -> "SimulationMode":
        if not value:
            return cls.SOCIAL
        lowered = str(value).strip().lower()
        if lowered == cls.WORLD.value:
            return cls.WORLD
        return cls.SOCIAL
