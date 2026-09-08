from types import SimpleNamespace
from uuid import UUID

import pytest

from app.core.errors import DomainError
from app.modules.strategies.copy_pool import seed_copies
from app.modules.strategies.grouping import make_groups


def test_tail_same_assets_different_copies_and_stable_replay():
    pool = seed_copies()
    assert (
        len(pool)
        == len({x.text for x in pool})
        == len({x.copy_id for x in pool})
        == 100
    )
    files = [
        SimpleNamespace(material_id=UUID(int=i + 1), file_name=f"Drama-{i:02}.mp4")
        for i in reversed(range(23))
    ]
    groups = make_groups(files, group_size=10, creative_count=3, pool=pool, seed=51)
    assert [len(x.material_ids) for x in groups] == [10, 10, 3]
    assert all(len({x.text for x in group.copies}) == 3 for group in groups)
    assert groups == make_groups(
        files, group_size=10, creative_count=3, pool=pool, seed=51
    )
    assert groups[0].material_ids == tuple(UUID(int=i + 1) for i in range(10))


def test_duplicate_names_keep_distinct_materials_but_duplicate_ids_do_not_repeat():
    files = [
        SimpleNamespace(material_id=UUID(int=i), file_name="Moon.mp4")
        for i in (3, 1, 2, 1)
    ]
    groups = make_groups(
        files, group_size=2, creative_count=1, pool=seed_copies(), seed=1
    )
    assert [x.material_ids for x in groups] == [
        (UUID(int=1), UUID(int=2)),
        (UUID(int=3),),
    ]
    assert (
        make_groups([], group_size=2, creative_count=1, pool=seed_copies(), seed=1)
        == ()
    )


@pytest.mark.parametrize(
    "count,pool",
    [(101, seed_copies()), (99, seed_copies()[:98]), (2, (seed_copies()[0],) * 100)],
)
def test_insufficient_unique_copy_never_reduces_creatives(count, pool):
    with pytest.raises(DomainError, match="copy_pool_exhausted"):
        make_groups([], group_size=10, creative_count=count, pool=pool, seed=1)


@pytest.mark.parametrize(
    "size,count", [(0, 1), (1, 0), (True, 1), (1, False), (1.5, 1)]
)
def test_bad_group_parameters_are_explicit(size, count):
    with pytest.raises(DomainError, match="invalid_group_config"):
        make_groups(
            [], group_size=size, creative_count=count, pool=seed_copies(), seed=1
        )
