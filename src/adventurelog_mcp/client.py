"""Thin async client for the AdventureLog REST API.

Authenticates with a personal API key (``X-API-Key``). Every write goes through
AdventureLog's own serializers and permission checks; this module adds no
access logic of its own.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import httpx


class ApiError(Exception):
    """An HTTP error from AdventureLog, with the response body flattened to text."""

    def __init__(self, status: int, method: str, path: str, detail: str, body: Any = None):
        self.status = status
        self.body = body
        self.method = method
        self.path = path
        self.detail = detail
        super().__init__(f"AdventureLog {method} {path} -> HTTP {status}: {detail}")


def _flatten(body: Any) -> str:
    """Turn a DRF error body ({"field": ["msg"]}, {"detail": ...}, [...]) into one line."""
    if isinstance(body, dict):
        parts = []
        for key, val in body.items():
            text = _flatten(val)
            parts.append(text if key in ("detail", "error", "non_field_errors") else f"{key}: {text}")
        return "; ".join(parts)
    if isinstance(body, list):
        return "; ".join(_flatten(v) for v in body)
    return str(body)


def _check_path(path: str) -> None:
    """Defence in depth against path injection: only plain API paths on our own server."""
    route = path.split("?", 1)[0]
    lowered = route.lower()
    if (not route.startswith("/") or "//" in route or "\\" in route or "%2e" in lowered or "%2f" in lowered
            or any(seg in (".", "..") for seg in route.split("/"))):
        raise ValueError(f"Refusing suspicious API path: {path!r}")


class AdventureLogClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if not api_key:
            raise ValueError(
                "No AdventureLog API key. Create one under Settings -> Security "
                "and set ADVENTURELOG_API_KEY."
            )
        self.base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"X-API-Key": api_key, "Accept": "application/json"},
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
    ) -> Any:
        _check_path(path)
        resp = await self._http.request(method, path, params=params, json=json, data=data, files=files)
        if resp.is_error:
            try:
                body = resp.json()
                detail = _flatten(body)
            except ValueError:
                body, detail = None, resp.text[:500] or resp.reason_phrase
            raise ApiError(resp.status_code, method, path, detail, body)
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    async def get(self, path: str, **params: Any) -> Any:
        return await self.request("GET", path, params={k: v for k, v in params.items() if v is not None})

    async def get_all(self, path: str, **params: Any) -> list[Any]:
        """Follow DRF pagination and return every result. Unpaginated lists pass through."""
        page = await self.get(path, **params)
        if isinstance(page, list):
            return page
        results = list(page.get("results", []))
        while page.get("next"):
            # DRF builds `next` from the request Host, which may differ behind a proxy: keep path + query only.
            nxt = urlsplit(page["next"])
            page = await self.request("GET", nxt.path + (f"?{nxt.query}" if nxt.query else ""))
            results.extend(page.get("results", []))
        return results

    async def post(self, path: str, data: Any) -> Any:
        return await self.request("POST", path, json=data)

    async def patch(self, path: str, data: Any) -> Any:
        return await self.request("PATCH", path, json=data)

    async def upload(self, path: str, data: dict[str, Any], files: dict[str, Any]) -> Any:
        """Multipart POST (file uploads)."""
        return await self.request("POST", path, data=data, files=files)

    async def delete(self, path: str, **params: Any) -> None:
        await self.request("DELETE", path, params=params or None)
