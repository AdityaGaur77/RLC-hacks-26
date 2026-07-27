"""A small, deliberately polite Socrata (SODA 2.0) client.

Palimpsest reads public endpoints only, at a low fixed rate, identifying itself
in the User-Agent. It never authenticates, never writes, and never submits a
form. The archive is built entirely from what the portals publish to anyone.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterator

log = logging.getLogger("palimpsest.socrata")

USER_AGENT = (
    "Palimpsest/0.1 (public-record integrity research; "
    "https://github.com/palimpsest-watch/palimpsest)"
)

DISCOVERY_ENDPOINT = "https://api.us.socrata.com/api/catalog/v1"

# Unauthenticated SODA requests share a per-IP budget. One request every 700ms
# keeps us well under it and leaves the portal comfortable.
MIN_INTERVAL_S = 0.7

RETRY_STATUS = {429, 500, 502, 503, 504}


class SocrataError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, url: str = ""):
        super().__init__(message)
        self.status = status
        self.url = url


@dataclass
class Response:
    data: Any
    headers: dict[str, str]
    url: str
    elapsed: float


class SocrataClient:
    def __init__(
        self,
        min_interval: float = MIN_INTERVAL_S,
        timeout: float = 60.0,
        max_retries: int = 4,
        app_token: str | None = None,
    ):
        self.min_interval = min_interval
        self.timeout = timeout
        self.max_retries = max_retries
        self.app_token = app_token
        self._last_request = 0.0
        self.request_count = 0

    # -- transport ---------------------------------------------------------

    def _throttle(self) -> None:
        gap = time.monotonic() - self._last_request
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last_request = time.monotonic()

    def _raw(self, url: str) -> Response:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if self.app_token:
            headers["X-App-Token"] = self.app_token

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            req = urllib.request.Request(url, headers=headers)
            started = time.time()
            try:
                self.request_count += 1
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    body = r.read()
                    return Response(
                        data=json.loads(body) if body else None,
                        headers={k: v for k, v in r.headers.items()},
                        url=url,
                        elapsed=time.time() - started,
                    )
            except urllib.error.HTTPError as e:
                last_error = e
                if e.code not in RETRY_STATUS or attempt == self.max_retries:
                    detail = ""
                    try:
                        detail = e.read()[:400].decode("utf-8", "replace")
                    except Exception:
                        pass
                    raise SocrataError(
                        f"HTTP {e.code} for {url}: {detail}", e.code, url
                    ) from e
                # Honour Retry-After when the portal supplies it.
                wait = float(e.headers.get("Retry-After") or 0) or 2.0 * (2**attempt)
                log.warning("HTTP %s on %s; retrying in %.1fs", e.code, url, wait)
                time.sleep(min(wait, 60.0))
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                last_error = e
                if attempt == self.max_retries:
                    raise SocrataError(f"{type(e).__name__} for {url}: {e}", None, url)
                wait = 2.0 * (2**attempt)
                log.warning("%s on %s; retrying in %.1fs", type(e).__name__, url, wait)
                time.sleep(wait)

        raise SocrataError(f"exhausted retries for {url}: {last_error}", None, url)

    # -- SODA --------------------------------------------------------------

    def query(self, domain: str, fourfour: str, **soql: Any) -> Response:
        """Run a SoQL query. Keys are passed as ``select=``, ``where=`` etc."""
        params = {f"${k}": v for k, v in soql.items() if v is not None}
        url = (
            f"https://{domain}/resource/{fourfour}.json?"
            + urllib.parse.urlencode(params)
        )
        return self._raw(url)

    def rows(self, domain: str, fourfour: str, **soql: Any) -> list[dict[str, Any]]:
        r = self.query(domain, fourfour, **soql)
        if not isinstance(r.data, list):
            raise SocrataError(f"expected a row list, got {type(r.data).__name__}", url=r.url)
        return r.data

    def scalar_count(self, domain: str, fourfour: str, where: str | None = None) -> int:
        rows = self.rows(domain, fourfour, select="count(*) AS n", where=where)
        if not rows:
            return 0
        return int(rows[0].get("n") or rows[0].get("count") or 0)

    def paginate(
        self,
        domain: str,
        fourfour: str,
        page_size: int = 5000,
        max_rows: int | None = None,
        **soql: Any,
    ) -> Iterator[dict[str, Any]]:
        """Yield rows page by page.

        A stable ``$order`` is essential: without one, SODA does not guarantee a
        consistent ordering between pages and rows can be silently duplicated or
        skipped across the page boundary.
        """
        soql.setdefault("order", ":id")
        offset = 0
        while True:
            page = self.rows(
                domain, fourfour, limit=page_size, offset=offset, **soql
            )
            if not page:
                return
            for row in page:
                yield row
                offset += 1
                if max_rows is not None and offset >= max_rows:
                    return
            if len(page) < page_size:
                return

    # -- metadata ----------------------------------------------------------

    def metadata(self, domain: str, fourfour: str) -> dict[str, Any]:
        """Dataset metadata via the views API (columns, update cadence, owner)."""
        url = f"https://{domain}/api/views/{fourfour}.json"
        r = self._raw(url)
        if not isinstance(r.data, dict):
            raise SocrataError("unexpected metadata payload", url=url)
        return r.data

    def catalog(
        self, domain: str, limit: int = 100, offset: int = 0, only: str = "dataset"
    ) -> dict[str, Any]:
        """Enumerate a portal's published assets via the Discovery API."""
        params = {
            "domains": domain,
            "search_context": domain,
            "only": only,
            "limit": limit,
            "offset": offset,
        }
        url = DISCOVERY_ENDPOINT + "?" + urllib.parse.urlencode(params)
        r = self._raw(url)
        if not isinstance(r.data, dict):
            raise SocrataError("unexpected catalog payload", url=url)
        return r.data


def response_provenance(headers: dict[str, str]) -> dict[str, str]:
    """Pull the portal's own freshness assertions out of the response headers.

    ``X-SODA2-Truth-Last-Modified`` is the portal stating when it believes the
    underlying data last changed. Recording it lets us compare the portal's
    account of itself against what we independently observed.
    """
    keep = (
        "Last-Modified",
        "ETag",
        "X-SODA2-Truth-Last-Modified",
        "X-SODA2-Data-Out-Of-Date",
        "Age",
        "Date",
    )
    return {k: headers[k] for k in keep if k in headers}
