"""Markdown post-processing: frontmatter, image/attachment download, link rewrite."""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import os
import re
import urllib.parse
from pathlib import Path
from typing import Callable, Optional

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
        assets_path: Optional[Path] = None,
        concurrency: int = 5,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._default_assets_path = assets_path
        if assets_path is not None:
            assets_path.mkdir(parents=True, exist_ok=True)
        self.semaphore = asyncio.Semaphore(concurrency)
        self._client = client
        self._existing: set[tuple[Path, str]] = set()
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

    async def download(self, url: str, assets_path: Optional[Path] = None) -> Optional[Path]:
        """Download a URL to assets_path and return the saved file path."""
        effective_path = assets_path or self._default_assets_path
        if effective_path is None:
            raise RuntimeError("No assets_path provided and no default set on ResourceDownloader")

        async with self._lock:
            if (effective_path, url) in self._existing:
                return self._filename_for_url(url, effective_path)

        client = await self._get_client()
        filename = self._compute_filename(url)
        target = effective_path / filename

        if target.exists():
            async with self._lock:
                self._existing.add((effective_path, url))
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
                        target = effective_path / self._compute_filename(url, better_name)

            effective_path.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            async with self._lock:
                self._existing.add((effective_path, url))
            logger.debug("Downloaded %s -> %s", url, target.name)
            return target

    def _filename_for_url(self, url: str, assets_path: Path) -> Optional[Path]:
        filename = self._compute_filename(url)
        target = assets_path / filename
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


def relative_asset_path(doc_file: Path, asset_file: Path) -> str:
    """Return POSIX-style relative path from doc to asset."""
    try:
        rel = os.path.relpath(str(asset_file), str(doc_file.parent))
        return Path(rel).as_posix()
    except (ValueError, OSError):
        return asset_file.name


# ---------------------------------------------------------------------------
# 1. Yuque internal doc link → Obsidian [[...]] link
# ---------------------------------------------------------------------------

YUQUE_DOC_LINK_RE = re.compile(
    r"https?://(?:www\.)?yuque\.com/([^/\s]+/[^/\s]+)/docs/([^/?#\s]+)"
)


def is_yuque_doc_link(url: str) -> Optional[tuple[str, str]]:
    """Return (namespace, slug) if URL is a Yuque doc link, else None.

    Namespace is returned with a leading slash, e.g. "/login/repo_slug".
    """
    match = YUQUE_DOC_LINK_RE.match(url.strip())
    if match:
        return (f"/{match.group(1)}", match.group(2))
    return None


def rewrite_internal_links(
    body: str,
    current_namespace: str,
    slug_to_path: dict[str, str],
    resolve_link: Callable[[str, str], Optional[str]],
) -> str:
    """Replace Yuque internal doc links with Obsidian [[...]] links.

    Handles both Markdown syntax ``[text](url)`` and HTML ``<a href="url">text</a>``.
    For intra-repo links the *slug_to_path* dict is used; cross-repo links are
    resolved via *resolve_link*.
    """
    replacements: list[tuple[int, int, str]] = []

    # Markdown links
    for match in MD_LINK_RE.finditer(body):
        url = match.group(2)
        doc_info = is_yuque_doc_link(url)
        if not doc_info:
            continue
        namespace, slug = doc_info
        local_path: Optional[str] = None
        if namespace == current_namespace and slug in slug_to_path:
            local_path = slug_to_path[slug]
        else:
            local_path = resolve_link(namespace, slug)
        if local_path:
            display = match.group(1) or Path(local_path).stem
            new_text = f"[[{local_path}|{display}]]"
            replacements.append((match.start(), match.end(), new_text))

    # HTML links
    for match in HTML_A_RE.finditer(body):
        url = match.group(1)
        doc_info = is_yuque_doc_link(url)
        if not doc_info:
            continue
        namespace, slug = doc_info
        local_path: Optional[str] = None
        if namespace == current_namespace and slug in slug_to_path:
            local_path = slug_to_path[slug]
        else:
            local_path = resolve_link(namespace, slug)
        if local_path:
            display = match.group(2) or Path(local_path).stem
            new_text = f"[[{local_path}|{display}]]"
            replacements.append((match.start(), match.end(), new_text))

    # Apply in reverse order to preserve indices.
    replacements.sort(key=lambda x: x[0], reverse=True)
    for start, end, new_text in replacements:
        body = body[:start] + new_text + body[end:]

    return body


# ---------------------------------------------------------------------------
# 2. Lake format conversion (画板 / 思维导图 / 表格等)
# ---------------------------------------------------------------------------

LAKE_CARD_RE = re.compile(r'<div[^>]*data-lake-card[^>]*>.*?</div>', re.DOTALL | re.IGNORECASE)
HTML_TABLE_RE = re.compile(r'<table[^>]*>(.*?)</table>', re.DOTALL | re.IGNORECASE)
HTML_TR_RE = re.compile(r'<tr[^>]*>(.*?)</tr>', re.DOTALL | re.IGNORECASE)
HTML_TD_RE = re.compile(r'<t[dh][^>]*>(.*?)</t[dh]>', re.DOTALL | re.IGNORECASE)
HTML_TAG_RE = re.compile(r'<[^>]+>')


def _strip_html_tags(raw: str) -> str:
    """Remove HTML tags and unescape entities."""
    text = HTML_TAG_RE.sub('', raw)
    return html.unescape(text).strip()


def _convert_html_tables(text: str) -> str:
    """Convert simple HTML tables in *text* to Markdown tables."""

    def _table_repl(m: re.Match[str]) -> str:
        table_html = m.group(1)
        rows: list[list[str]] = []
        for tr_match in HTML_TR_RE.finditer(table_html):
            cells = [_strip_html_tags(td_match.group(1)) for td_match in HTML_TD_RE.finditer(tr_match.group(1))]
            if cells:
                rows.append(cells)
        if not rows:
            return ''
        md_rows = ['| ' + ' | '.join(r) + ' |' for r in rows]
        col_count = len(rows[0])
        if len(md_rows) > 1:
            separator = '| ' + ' | '.join(['---'] * col_count) + ' |'
            md_rows.insert(1, separator)
        return '\n'.join(md_rows) + '\n'

    return HTML_TABLE_RE.sub(_table_repl, text)


def _basic_html_to_markdown(html_text: str) -> str:
    """Lightweight HTML → Markdown converter for lake *body_html* fallback."""
    text = html_text
    # Strip script / style blocks completely.
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    # Headings
    for i in range(6, 0, -1):
        text = re.sub(rf'<h{i}[^>]*>(.*?)</h{i}>', rf'{"#" * i} \1\n\n', text, flags=re.DOTALL | re.IGNORECASE)
    # Bold / italic
    text = re.sub(r'<(?:strong|b)[^>]*>(.*?)</(?:strong|b)>', r'**\1**', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<(?:em|i)[^>]*>(.*?)</(?:em|i)>', r'*\1*', text, flags=re.DOTALL | re.IGNORECASE)
    # Code blocks (pre + code)
    text = re.sub(r'<pre[^>]*>\s*<code[^>]*>(.*?)</code>\s*</pre>', r'```\n\1\n```\n\n', text, flags=re.DOTALL | re.IGNORECASE)
    # Inline code
    text = re.sub(r'<code[^>]*>(.*?)</code>', r'`\1`', text, flags=re.DOTALL | re.IGNORECASE)
    # Paragraphs
    text = re.sub(r'<p[^>]*>(.*?)</p>', r'\1\n\n', text, flags=re.DOTALL | re.IGNORECASE)
    # Line breaks
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    # Lists
    text = re.sub(r'<li[^>]*>(.*?)</li>', r'- \1', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'</?ul[^>]*>|</?ol[^>]*>', '', text, flags=re.IGNORECASE)
    # Images (already handled by ResourceDownloader, but strip alt/src noise)
    text = re.sub(r'<img[^>]*>', '', text, flags=re.IGNORECASE)
    # Strip remaining tags
    text = HTML_TAG_RE.sub('', text)
    # Unescape
    text = html.unescape(text)
    # Collapse excessive blank lines
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _handle_lake_cards(text: str, namespace: str, slug: str) -> str:
    """Replace lake board / mind-map cards with placeholders."""

    def _card_repl(m: re.Match[str]) -> str:
        card_html = m.group(0).lower()
        source_url = f"https://www.yuque.com{namespace}/docs/{slug}"
        if 'board' in card_html or 'whiteboard' in card_html:
            return f'> 🎨 画板内容无法直接导出，请查看原文：[原文]({source_url})\n\n'
        if 'mindmap' in card_html or 'mind' in card_html:
            return f'> 🧠 思维导图内容无法直接导出，请查看原文：[原文]({source_url})\n\n'
        # Unknown lake card – keep as-is but wrapped in a HTML comment so it
        # does not break Obsidian rendering.
        return f'<!-- 语雀 Lake 卡片（未识别类型）\n{m.group(0)}\n-->\n\n'

    return LAKE_CARD_RE.sub(_card_repl, text)


def convert_lake_body(
    body: str,
    body_html: Optional[str],
    namespace: str,
    slug: str,
) -> str:
    """Convert lake-format document body to Obsidian-compatible Markdown.

    Strategy:
    1. Handle known lake cards (board / mind-map) → placeholders.
    2. Convert any embedded HTML tables → Markdown tables.
    3. If *body* is very short / empty and *body_html* is present, do a basic
       HTML → Markdown conversion as fallback.
    """
    if not body:
        body = ''

    # Step 1: lake cards
    body = _handle_lake_cards(body, namespace, slug)

    # Step 2: HTML tables inside markdown body
    body = _convert_html_tables(body)

    # Step 3: fallback to body_html when body is basically empty
    if body_html and len(body.strip()) < 100:
        body = _basic_html_to_markdown(body_html)
        # Re-apply table conversion on the converted text as well.
        body = _convert_html_tables(body)
        body = _handle_lake_cards(body, namespace, slug)

    return body


# ---------------------------------------------------------------------------


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

    if doc.format:
        lines.append(f"format: {doc.format}")

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
    downloader: ResourceDownloader,
    assets_path: Path,
) -> tuple[int, int, str] | None:
    url = match.group(url_group)
    if not is_yuque_resource(url):
        return None
    asset_path = await downloader.download(url, assets_path)
    if asset_path is None:
        return None
    rel = relative_asset_path(doc_file, asset_path)
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
    assets_path: Path,
    slug_to_path: dict[str, str],
    resolve_link: Callable[[str, str], Optional[str]],
) -> str:
    """Process a document body: rewrite internal links, download resources, add frontmatter."""
    body = doc.body or ""

    # 1. Lake format conversion (画板 / 思维导图 / 表格等)
    if doc.format and doc.format.lower() == "lake":
        body = convert_lake_body(body, doc.body_html, namespace, doc.slug)

    # 2. Rewrite Yuque internal doc links → Obsidian [[...]] links
    body = rewrite_internal_links(body, namespace, slug_to_path, resolve_link)

    # 3. Download external resources and rewrite links.
    tasks: list[asyncio.Task[tuple[int, int, str] | None]] = []

    for match in MD_IMAGE_RE.finditer(body):
        tasks.append(
            asyncio.create_task(
                _replace_match(match, 2, True, body, doc_file, downloader, assets_path)
            )
        )
    for match in MD_LINK_RE.finditer(body):
        url = match.group(2)
        if is_yuque_resource(url):
            tasks.append(
                asyncio.create_task(
                    _replace_match(match, 2, False, body, doc_file, downloader, assets_path)
                )
            )

    # HTML tags processed sequentially to avoid complex concurrent regex replacements.
    html_replacements: list[tuple[int, int, str]] = []
    for match in HTML_IMG_RE.finditer(body):
        url = match.group(1)
        if is_yuque_resource(url):
            asset_path = await downloader.download(url, assets_path)
            if asset_path is not None:
                rel = relative_asset_path(doc_file, asset_path)
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
            asset_path = await downloader.download(url, assets_path)
            if asset_path is not None:
                rel = relative_asset_path(doc_file, asset_path)
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
