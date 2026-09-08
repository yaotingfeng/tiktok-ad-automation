from collections.abc import Iterable
from dataclasses import dataclass
from random import Random
from typing import Protocol
from uuid import UUID

from app.core.errors import DomainError
from app.modules.strategies.copy_pool import CopyChoice


class NamedMaterial(Protocol):
    @property
    def material_id(self) -> UUID: ...

    @property
    def file_name(self) -> str: ...


@dataclass(frozen=True)
class GroupPlan:
    group_no: int
    material_ids: tuple[UUID, ...]
    copies: tuple[CopyChoice, ...]


def make_groups(
    materials: Iterable[NamedMaterial],
    *,
    group_size: int,
    creative_count: int,
    pool: tuple[CopyChoice, ...],
    seed: int,
) -> tuple[GroupPlan, ...]:
    """Called once per drama, before expanding across the account directory."""
    if any(
        type(value) is not int or value < 1 for value in (group_size, creative_count)
    ):
        raise DomainError("invalid_group_config", "invalid_group_config")
    unique = {item.material_id: item for item in materials}
    ordered = sorted(
        unique.values(), key=lambda item: (item.file_name, item.material_id)
    )
    copies = tuple({item.text: item for item in pool if item.text.strip()}.values())
    if creative_count > len(copies):
        raise DomainError("copy_pool_exhausted", "copy_pool_exhausted")
    rng = Random(seed)
    return tuple(
        GroupPlan(
            index // group_size + 1,
            tuple(item.material_id for item in ordered[index : index + group_size]),
            tuple(rng.sample(copies, creative_count)),
        )
        for index in range(0, len(ordered), group_size)
    )
