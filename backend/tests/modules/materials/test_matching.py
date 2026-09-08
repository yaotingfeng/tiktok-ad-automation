import pytest

from app.modules.materials.matching import filename_matches
from app.modules.materials.models import MaterialFile


@pytest.mark.parametrize(
    ("filename", "title", "expected"),
    [
        ("20260908_MOON 100%_01.mp4", " Moon 100% ", True),
        ("Moon 1000_01.mp4", "Moon 100%", False),
        ("Moonlight_01.mp4", "Moon Light", False),
        ("Moon-Sun_01.mp4", "Moon", True),
        ("Moon-Sun_01.mp4", "Sun", True),
        ("STRASSE_01.mp4", "Straße", True),
        (r"Moon\_01.mp4", r"Moon\_", True),
        ("Moon_01.mp4", "  ", False),
    ],
)
def test_full_title_matches_literal_filename(filename, title, expected):
    assert filename_matches(filename, title) is expected


def test_material_has_no_permanent_drama_owner():
    assert "drama_id" not in MaterialFile.model_fields
