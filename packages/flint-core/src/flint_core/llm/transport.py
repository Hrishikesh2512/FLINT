"""Default HTTP transport (requests). Tests inject fakes instead."""

from __future__ import annotations

from typing import Any

import requests

from flint_core.llm.base import ProviderError, TransportResult


def requests_transport(
    url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float
) -> TransportResult:
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=timeout)
    except requests.RequestException as exc:
        raise ProviderError(f"network error: {exc}") from exc

    try:
        body = response.json()
    except ValueError:
        body = {"raw": response.text[:500]}
    return TransportResult(status=response.status_code, body=body)
