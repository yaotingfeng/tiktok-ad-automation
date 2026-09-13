from app.modules.builds.mini_targets import explicit_mini_id, url_key


def test_only_explicit_tiktok_mini_ids_are_trusted():
    assert explicit_mini_id("https://www.tiktok.com/minis/short-code") is None
    assert (
        explicit_mini_id("https://www.tiktok.com/minis/open?minis_id=mini-real")
        == "mini-real"
    )
    assert (
        explicit_mini_id("https://other.example/minis/open?minis_id=mini-real") is None
    )
    assert (
        explicit_mini_id("https://www.tiktok.com/minis/open?minis_id=a&minis_id=b")
        is None
    )
    assert url_key("https://www.tiktok.com/minis/a") != url_key(
        "https://www.tiktok.com/minis/b"
    )

    assert (
        explicit_mini_id("https://www.tiktok.com/minis/open?minis_id=a&minis_id=")
        is None
    )
