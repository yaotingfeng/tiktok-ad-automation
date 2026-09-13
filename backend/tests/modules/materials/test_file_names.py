from app.modules.materials.file_names import video_file_name


def test_original_english_name_and_number_are_preserved():
    name = "01-20260910-The General's Wrath-CL6-JUNBO-LH-1.mp4"
    assert video_file_name(name) == name
    assert video_file_name(name, correlation="01234567") == name[:-4] + "-01234567.mp4"


def test_long_unicode_name_keeps_trailing_episode_and_bounded_utf8():
    name = "长剧名" * 50 + "-CL6-JUNBO-LH-10.mp4"
    result = video_file_name(name, correlation="01234567")
    assert len(result.encode()) <= 100
    assert result.endswith("-CL6-JUNBO-LH-10-01234567.mp4")
    assert "/" not in video_file_name("/tmp/path\\name\n.mp4")
