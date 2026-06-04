"""Markdown post-processing: frontmatter, image/attachment download, link rewrite."""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import re
import urllib.parse
from pathlib import Path
from typing import Optional

import httpx

from yuque2obsidian.config import Config
from yuque2obsidian.models import DocDetail

logger = logging.getLogger("yuque2obsidian")

# Markdown image: ![alt](url)
MD_IMAGE_RE = re.compile(r"!\[(.*?)\]\((.+?)\)")
# Markdown link: [text](url)
MD_LINK_RE = re.compile(r"(?<!!)\[(.*?)\]\((.+?)\)")
# HTML img tag
HTML_IMG_RE = re.compile(r"<img\s+[^>]*src=[\"']([^\"']+)[\"'][^>]*>", re.IGNORECASE)
# HTML a tag
HTML_A_RE = re.compile(r"<a\s+[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)

# URL patterns
CDN_NLARK = "cdn.nlark.com"
YUQUE_DOMAIN = "www.yuque.com"
YUQUE_ATTACH = "/attachments/"


class ResourceDownloader:
    """Downloads external images/attachments and caches them locally."""

    def __init__(
        self,
        assets_path: Path,
        concurrency: int = 5,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.assets_path = assets_path
        self.assets_path.mkdir(parents=True, exist_ok=True)
        self.semaphore = asyncio.Semaphore(concurrency)
        self._client = client
        self._existing: set[str] = set()
        self._failed: list[str] = []
        self._lock = asyncio.Lock()

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(60.0),
                follow_redirects=True,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    )
                },
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _compute_filename(self, url: str, content_disposition: Optional[str] = None) -> str:
        parsed = urllib.parse.urlparse(url)
        # Try to infer extension from path.
        path = parsed.path or "resource"
        base = Path(path).name
        if content_disposition:
            base = content_disposition
        if not base or base in ("/", "."):
            base = "resource"
        # Remove Windows-forbidden and URL-special chars while keeping unicode.
        base = re.sub(r'[\\/:*?"<>|]', "_", base)
        base = base.strip(". ")
        if not base:
            base = "resource"
        # Hash prefix to avoid collisions and weird names.
        url_hash = hashlib.sha256(url.encode()).hexdigest()[:8]
        if "." not in base:
            # No extension, try to infer from content-type or default to bin.
            base = f"{base}.bin"
        return f"{url_hash}_{base}"

    async def download(self, url: str) -> Optional[Path]:
        """Download a URL to assets_path and return relative filename."""
        async with self._lock:
            if url in self._existing:
                # Already downloaded in this session.
                return self._filename_for_url(url)

        client = await self._get_client()
        filename = self._compute_filename(url)
        target = self.assets_path / filename

        if target.exists():
            async with self._lock:
                self._existing.add(url)
            return target

        async with self.semaphore:
            try:
                response = await client.get(url)
                response.raise_for_status()
            except Exception as exc:
                logger.warning("Failed to download %s: %s", url, exc)
                async with self._lock:
                    self._failed.append(url)
                return None

            content = response.content
            # Try to extract better filename from Content-Disposition.
            cd = response.headers.get("content-disposition", "")
            if cd:
                fname_match = re.search(r'filename\*?=\s*"?([^"]+)"?', cd)
                if fname_match:
                    better_name = fname_match.group(1).strip()
                    if better_name:
                        target = self.assets_path / self._compute_filename(url, better_name)

            target.write_bytes(content)
            async with self._lock:
                self._existing.add(url)
            logger.debug("Downloaded %s -> %s", url, target.name)
            return target

    def _filename_for_url(self, url: str) -> Optional[Path]:
        filename = self._compute_filename(url)
        target = self.assets_path / filename
        return target if target.exists() else None

    def get_failed(self) -> list[str]:
        return list(self._failed)


def is_yuque_resource(url: str) -> bool:
    """Check if URL is an external image/attachment that should be downloaded."""
    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return False
    # Skip mailto / anchor only.
    if parsed.scheme in ("mailto", "tel"):
        return False
    # Skip plain Yuque doc links (these are internal navigation, not assets).
    if "yuque.com" in parsed.netloc and "/docs/" in parsed.path:
        return False
    # Download anything from Yuque CDN / attachments.
    if "nlark.com" in parsed.netloc:
        return True
    if "yuque.com" in parsed.netloc and "/attachments/" in parsed.path:
        return True
    # Heuristic: Yuque domain with common image extensions.
    if "yuque.com" in parsed.netloc:
        path_lower = parsed.path.lower()
        if any(path_lower.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".pdf", ".zip", ".docx")):
            return True
    return False


def is_image_url(url: str) -> bool:
    """Heuristic: is this URL likely an image?"""
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.lower()
    image_exts = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp")
    return path.endswith(image_exts) or "/image/" in path or "/png/" in path or "/jpg/" in path


def relative_asset_path(doc_file: Path, asset_file: Path, output_root: Path) -> str:
    """Return POSIX-style relative path from doc to asset."""
    # doc is under output_root/repo/...; assets are under output_root/assets.
    try:
        rel_doc_dir = doc_file.parent.relative_to(output_root)
    except ValueError:
        rel_doc_dir = Path()
    ups = "../" * len(rel_doc_dir.parts) if rel_doc_dir.parts else ""
    rel_asset = asset_file.relative_to(output_root)
    return (ups + rel_asset.as_posix()).lstrip("/") or rel_asset.as_posix()


def build_frontmatter(doc: DocDetail, namespace: str, config: Config) -> str:
    """Build YAML frontmatter for Obsidian."""
    lines = ["---"]
    lines.append(f"title: {escape_yaml(doc.title)}")

    if config.frontmatter.include_created_at and doc.created_at:
        lines.append(f"created: {doc.created_at.isoformat()}")
    if config.frontmatter.include_updated_at and doc.content_updated_at:
        lines.append(f"updated: {doc.content_updated_at.isoformat()}")
    if config.frontmatter.include_updated_at and doc.updated_at:
        lines.append(f"modified: {doc.updated_at.isoformat()}")

    if config.frontmatter.include_source_url:
        source = f"https://www.yuque.com{namespace}/docs/{doc.slug}"
        lines.append(f"source: {source}")

    if config.frontmatter.include_tags:
        lines.append("tags:")
        lines.append("  - yuque")

    lines.append("---")
    return "\n".join(lines) + "\n\n"


def escape_yaml(value: str) -> str:
    value = value or ""
    if any(c in value for c in ('"', "\n", ":", "#", "{", "}")):
        value = value.replace('"', '\\"')
        return f'"{value}"'
    return value


async def _replace_match(
    match: re.Match[str],
    url_group: int,
    is_image: bool,
    body: str,
    doc_file: Path,
    output_root: Path,
    downloader: ResourceDownloader,
) -> tuple[int, int, str] | None:
    url = match.group(url_group)
    if not is_yuque_resource(url):
        return None
    asset_path = await downloader.download(url)
    if asset_path is None:
        return None
    rel = relative_asset_path(doc_file, asset_path, output_root)
    if is_image:
        new_text = f"![{match.group(1)}]({rel})"
    else:
        new_text = f"[{match.group(1)}]({rel})"
    return (match.start(), match.end(), new_text)


async def process_markdown(
    doc: DocDetail,
    doc_file: Path,
    namespace: str,
    downloader: ResourceDownloader,
    config: Config,
) -> str:
    """Process a document body: download resources and rewrite links."""
    body = doc.body or ""
    output_root = config.output_path

    # Collect replacement tasks.
    tasks: list[asyncio.Task[tuple[int, int, str] | None]] = []

    for match in MD_IMAGE_RE.finditer(body):
        tasks.append(
            asyncio.create_task(
                _replace_match(match, 2, True, body, doc_file, output_root, downloader)
            )
        )
    for match in MD_LINK_RE.finditer(body):
        url = match.group(2)
        if is_yuque_resource(url):
            tasks.append(
                asyncio.create_task(
                    _replace_match(match, 2, False, body, doc_file, output_root, downloader)
                )
            )

    # HTML tags processed sequentially to avoid complex concurrent regex replacements.
    html_replacements: list[tuple[int, int, str]] = []
    for match in HTML_IMG_RE.finditer(body):
        url = match.group(1)
        if is_yuque_resource(url):
            asset_path = await downloader.download(url)
            if asset_path is not None:
                rel = relative_asset_path(doc_file, asset_path, output_root)
                new_tag = re.sub(
                    r'src=["\'][^"\']+["\']',
                    f'src="{html.escape(rel)}"',
                    match.group(0),
                    count=1,
                )
                html_replacements.append((match.start(), match.end(), new_tag))
    for match in HTML_A_RE.finditer(body):
        url = match.group(1)
        if is_yuque_resource(url):
            asset_path = await downloader.download(url)
            if asset_path is not None:
                rel = relative_asset_path(doc_file, asset_path, output_root)
                new_tag = re.sub(
                    r'href=["\'][^"\']+["\']',
                    f'href="{html.escape(rel)}"',
                    match.group(0),
                    count=1,
                )
                html_replacements.append((match.start(), match.end(), new_tag))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    replacements: list[tuple[int, int, str]] = html_replacements[:]
    for r in results:
        if isinstance(r, tuple):
            replacements.append(r)
        elif isinstance(r, Exception):
            logger.warning("Resource replacement task failed: %s", r)

    # Apply replacements in reverse order to preserve indices.
    replacements.sort(key=lambda x: x[0], reverse=True)
    for start, end, new_text in replacements:
        body = body[:start] + new_text + body[end:]

    # Build final markdown.
    frontmatter = build_frontmatter(doc, namespace, config)
    return frontmatter + body
