"""Read-only verification of login, API and OAuth bootstrap entry points."""

import argparse
from urllib.parse import urlsplit

import httpx


def check(base_url: str) -> None:
    url = urlsplit(base_url)
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path not in {"", "/"}
    ):
        raise ValueError("Use an origin URL without credentials, path or query")
    with httpx.Client(base_url=base_url.rstrip("/"), timeout=15) as client:
        health = client.get("/api/utils/health-check/")
        assert health.status_code == 200 and health.json() is True, "Health check failed"
        login = client.get("/login", headers={"Accept": "text/html"})
        assert login.status_code == 200 and "text/html" in login.headers.get(
            "content-type", ""
        ), "Built login page unavailable"
        callback = client.get("/api/integrations/tiktok/callback")
        assert callback.status_code in {400, 422, 503}, "Unexpected callback status"
        assert callback.headers.get("content-type", "").startswith(
            "application/json"
        ), "Callback is not an API response"
        assert isinstance(callback.json().get("code"), str), "Callback error code missing"
        assert client.get("/api/bootstrap-unknown").status_code == 404, "API falls into SPA"
    print("PASS: health, built login, callback business error and API boundary")
    print("This checks route availability; OAuth authorization is not verified.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url")
    args = parser.parse_args()
    try:
        check(args.base_url)
    except (AssertionError, ValueError, httpx.HTTPError) as error:
        # HTTP exception text may include a supplied URL; report only its type.
        raise SystemExit(f"Bootstrap verification failed ({type(error).__name__})") from None
