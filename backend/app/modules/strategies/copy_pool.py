from dataclasses import dataclass
from uuid import UUID, uuid5

POOL_VERSION = UUID("02a4e656-a330-40dc-864c-26e81961f3ca")
OPENERS = (
    "A story for your next break.",
    "Your next short drama awaits.",
    "Make room for a little drama.",
    "Take a moment for a new story.",
    "Enjoy a story one episode at a time.",
    "Discover a short drama today.",
    "Add a new story to your day.",
    "Spend a little time with a new drama.",
    "There is a story ready to explore.",
    "Start your next drama break.",
)
ENDINGS = (
    "Watch an episode.",
    "Start watching today.",
    "See where the story goes.",
    "Discover what happens next.",
    "Take a look at the story.",
    "Follow the story from here.",
    "Find a moment to watch.",
    "Explore the next scene.",
    "Let the story unfold.",
    "Begin with one episode.",
)


@dataclass(frozen=True)
class CopyChoice:
    copy_id: UUID
    text: str


def seed_copies() -> tuple[CopyChoice, ...]:
    """Reviewed fixed English copy; these are ad texts, never CTA options."""
    return tuple(
        CopyChoice(uuid5(POOL_VERSION, text), text)
        for start in OPENERS
        for end in ENDINGS
        if (text := f"{start} {end}")
    )
