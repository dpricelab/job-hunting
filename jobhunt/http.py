"""One shared HTTP client, with sane timeouts and polite retries."""

from __future__ import annotations

import time

import requests

from . import __version__

DEFAULT_TIMEOUT = 20
USER_AGENT = f"jobhunt/{__version__} (personal job search tool)"
RETRY_STATUSES = {429, 500, 502, 503, 504}


class HttpError(Exception):
    """A request failed in a way the caller should report, not crash on."""


_session: requests.Session | None = None


def session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
                "Accept-Language": "en-GB,en;q=0.9",
            }
        )
    return _session


def get(
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    retries: int = 3,
) -> requests.Response:
    """GET with exponential backoff on transient failures.

    Raises HttpError on a non-retryable status or after exhausting retries.
    """
    return _request("GET", url, params=params, headers=headers, timeout=timeout, retries=retries)


def post(
    url: str,
    *,
    json: dict | None = None,
    headers: dict | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    retries: int = 3,
) -> requests.Response:
    """POST a JSON body, with the same retry behaviour as `get`."""
    return _request("POST", url, json=json, headers=headers, timeout=timeout, retries=retries)


def _request(
    method: str,
    url: str,
    *,
    params: dict | None = None,
    json: dict | None = None,
    headers: dict | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    retries: int = 3,
) -> requests.Response:
    last_error = ""
    delay = 1.0

    for attempt in range(retries):
        try:
            response = session().request(
                method, url, params=params, json=json, headers=headers, timeout=timeout
            )
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            if response.status_code < 400:
                return response
            last_error = f"HTTP {response.status_code} for {url}"
            if response.status_code not in RETRY_STATUSES:
                raise HttpError(last_error)

        if attempt < retries - 1:
            time.sleep(delay)
            delay *= 2

    raise HttpError(last_error or f"request to {url} failed")


def get_json(url: str, **kwargs):
    return _as_json(get(url, **kwargs), url)


def post_json(url: str, payload: dict, **kwargs):
    return _as_json(post(url, json=payload, **kwargs), url)


def _as_json(response: requests.Response, url: str):
    try:
        return response.json()
    except ValueError as exc:
        raise HttpError(f"expected JSON from {url}: {exc}") from exc


def get_text(url: str, **kwargs) -> str:
    return get(url, **kwargs).text
