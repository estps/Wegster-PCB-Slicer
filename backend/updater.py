
from __future__ import annotations

import json
import os
import re
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

__all__ = ["UpdateInfo", "check_for_update", "current_version", "parse_version"]

REPO = os.environ.get("WEGSTR_UPDATE_REPO", "estps/Wegster-PCB-Slicer")
API_URL = "https://api.github.com/repos/{repo}/releases"
TIMEOUT = 6.0

FALLBACK_VERSION = "1.1.0"


@dataclass
class UpdateInfo:

    version: str
    current: str
    url: str
    name: str = ""
    notes: str = ""
    prerelease: bool = False
    asset_url: str | None = None


def current_version() -> str:
    try:
        from main import VERSION

        return VERSION
    except Exception:
        return FALLBACK_VERSION


def parse_version(text: str) -> tuple[int, ...]:
    cleaned = str(text).strip().lstrip("vV")
    parts = re.findall(r"\d+", cleaned)
    if not parts:
        return (0,)
    return tuple(int(part) for part in parts[:4])


def _is_newer(candidate: str, current: str) -> bool:
    left = parse_version(candidate)
    right = parse_version(current)
    length = max(len(left), len(right))
    left = left + (0,) * (length - len(left))
    right = right + (0,) * (length - len(right))
    return left > right


def _fetch(url: str) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "Wegstr-PCB-Slicer",
        },
    )
    context = ssl.create_default_context()
    with urllib.request.urlopen(request, timeout=TIMEOUT, context=context) as response:
        return json.loads(response.read().decode("utf-8"))


def _pick_asset(release: dict[str, Any]) -> str | None:
    assets = release.get("assets") or []
    for asset in assets:
        name = str(asset.get("name", "")).lower()
        if name.endswith(".zip"):
            return asset.get("browser_download_url")
    for asset in assets:
        url = asset.get("browser_download_url")
        if url:
            return url
    return None


def check_for_update(
    repo: str | None = None,
    current: str | None = None,
    include_prerelease: bool = True,
) -> UpdateInfo | None:
    repo = repo or REPO
    current = current or current_version()

    try:
        releases = _fetch(API_URL.format(repo=repo))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, ssl.SSLError):
        return None

    if not isinstance(releases, list):
        return None

    best: dict[str, Any] | None = None
    for release in releases:
        if not isinstance(release, dict) or release.get("draft"):
            continue
        if release.get("prerelease") and not include_prerelease:
            continue
        tag = str(release.get("tag_name") or release.get("name") or "")
        if not tag:
            continue
        if best is None or _is_newer(tag, str(best.get("tag_name") or best.get("name"))):
            best = release

    if best is None:
        return None

    tag = str(best.get("tag_name") or best.get("name") or "")
    if not _is_newer(tag, current):
        return None

    return UpdateInfo(
        version=tag.lstrip("vV"),
        current=current,
        url=str(best.get("html_url") or f"https://github.com/{repo}/releases"),
        name=str(best.get("name") or tag),
        notes=str(best.get("body") or "")[:2000],
        prerelease=bool(best.get("prerelease")),
        asset_url=_pick_asset(best),
    )
