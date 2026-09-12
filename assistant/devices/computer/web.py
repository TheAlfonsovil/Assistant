"""Network actions for the computer branch."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote_plus, urlparse

import httpx

from assistant.domain.models import ErrorType, OperationResult
from assistant.tools import Tool, ToolDefinition


class WebTool(Tool):
    definition = ToolDefinition(
        name="web",
        description="Search the public web or fetch a public HTTP page",
        methods=["search", "fetch"],
        argument_schema={
            "query": {"type": "string"},
            "url": {"type": "string"},
            "limit": {"type": "integer", "maximum": 10},
        },
        permissions=["web.search", "web.fetch"],
        timeout=20.0,
    )

    async def execute(self, method: str, args: dict[str, Any], timeout: float) -> OperationResult:
        if method == "search":
            query = args.get("query")
            if not isinstance(query, str) or not query.strip():
                return self._invalid("web.search requires query")
            url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
            result = await self._request(url, timeout, query=query, limit=args.get("limit", 5))
            if result.success:
                limit = min(max(int(args.get("limit", 5)), 1), 10)
                result.output["results"] = self._parse_search_results(result.output["content"], limit)
                result.output.pop("content", None)
            return result
        if method == "fetch":
            url = args.get("url")
            if not isinstance(url, str):
                return self._invalid("web.fetch requires url")
            return await self._request(url, timeout)
        return self._invalid(f"Unsupported web method: {method}")

    async def _request(self, url: str, timeout: float, **metadata: Any) -> OperationResult:
        try:
            await self._validate_public_url(url)
            async with httpx.AsyncClient(
                timeout=min(max(timeout, 1.0), 30.0),
                follow_redirects=False,
                headers={"User-Agent": "Assistant-Core/0.1"},
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
            content = response.text[:100_000]
            return OperationResult(
                success=True,
                output={"url": str(response.url), "status_code": response.status_code, "content": content},
                metadata=metadata,
            )
        except (httpx.HTTPError, OSError, ValueError) as error:
            return OperationResult(
                success=False,
                error=str(error),
                error_type=ErrorType.TOOL_FAILURE,
                retryable=True,
            )

    @staticmethod
    async def _validate_public_url(url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Only public http(s) URLs are allowed")
        host = parsed.hostname
        addresses = await asyncio.to_thread(socket.getaddrinfo, host, None)
        for address in {item[4][0] for item in addresses}:
            ip = ipaddress.ip_address(address)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                raise ValueError("Private and local network destinations are blocked")

    @staticmethod
    def _invalid(message: str) -> OperationResult:
        return OperationResult(success=False, error=message, error_type=ErrorType.INVALID_ARGUMENT)

    @staticmethod
    def _parse_search_results(content: str, limit: int) -> list[dict[str, str]]:
        parser = _SearchResultParser()
        parser.feed(content)
        return parser.results[:limit]


class _SearchResultParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            href = dict(attrs).get("href")
            if href and href.startswith("http"):
                self._href = href
                self._text = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data.strip())

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href:
            title = " ".join(part for part in self._text if part)
            if title:
                self.results.append({"title": title[:200], "url": self._href})
            self._href = None
            self._text = []


__all__ = ["WebTool"]
