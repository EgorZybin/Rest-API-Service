from __future__ import annotations

from typing import Any

import httpx

from sherlock_api.logging import get_logger

log = get_logger(__name__)

REPORT_TXT_HARD_CAP_BYTES = 5_000_000  # 5 MiB
REPORT_TXT_TIMEOUT = 30.0


async def fetch_report_txt(url: str) -> tuple[str | None, bool]:
    full = url.rstrip("/") + "/txt"
    try:
        async with httpx.AsyncClient(timeout=REPORT_TXT_TIMEOUT, follow_redirects=True) as client:
            r = await client.get(full)
            r.raise_for_status()
            body = r.text
    except Exception as e:
        log.warning("report_txt.fetch_failed", url=full, err=repr(e))
        return None, False
    truncated = False
    if len(body.encode("utf-8")) > REPORT_TXT_HARD_CAP_BYTES:
        truncated = True
        body = body.encode("utf-8")[:REPORT_TXT_HARD_CAP_BYTES].decode("utf-8", errors="ignore")
    return body, truncated


async def merge_report_txt_if_present(data: dict[str, Any]) -> None:
    """Same as email/vin/…: if `report_url` is set, attach `report_txt` + `report_txt_truncated`."""
    url = data.get("report_url")
    if not isinstance(url, str) or not url.strip():
        return
    body, truncated = await fetch_report_txt(url)
    data["report_txt"] = body
    data["report_txt_truncated"] = truncated
