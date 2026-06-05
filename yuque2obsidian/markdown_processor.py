"""Markdown post-processing: frontmatter, image/attachment download, link rewrite."""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import os
import re
import urllib.parse
from pathlib import Path
from typing import Callable, Optional

import httpx
import markdownify

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


def _markdownify_html(html_text: str) -> str:
    """Convert HTML to Markdown using markdownify (handles complex HTML well)."""
    # markdownify uses BeautifulSoup under the hood and produces high-quality
    # Markdown for headings, lists, tables, code blocks, emphasis, etc.
    md = markdownify.markdownify(
        html_text,
        heading_style="ATX",
        strip=["script", "style"],
    )
    # Collapse excessive blank lines.
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


# Lakesheet-specific patterns
LAKE_SHEET_RE = re.compile(r'<div[^>]*data-lake-card=["\'][^"\']*sheet[^"\']*["\'][^>]*>.*?</div>', re.DOTALL | re.IGNORECASE)
LAKE_SHEET_IMG_RE = re.compile(
    r'<div[^>]*data-lake-card=["\'][^"\']*sheet[^"\']*["\'][^>]*>.*?<img\s+[^>]*src=["\']([^"\']+)["\'][^>]*>.*?</div>',
    re.DOTALL | re.IGNORECASE,
)


def _handle_lake_cards(text: str, namespace: str, slug: str) -> str:
    """Replace lake board / mind-map / sheet cards with placeholders."""
    source_url = f"https://www.yuque.com{namespace}/docs/{slug}"

    # 1. Lakesheet – try to extract a rendered image first, otherwise placeholder.
    def _sheet_repl(m: re.Match[str]) -> str:
        # Try to find an <img> inside the lake-sheet card (Yuque sometimes
        # renders the sheet as a static image).
        img_match = re.search(
            r'<img\s+[^>]*src=["\']([^"\']+)["\'][^>]*>',
            m.group(0),
            re.IGNORECASE,
        )
        if img_match:
            img_url = img_match.group(1)
            return f'> 📊 数据表（原文图片）：![sheet]({img_url})\n\n> 或查看原文：[原文]({source_url})\n\n'
        return f'> 📊 数据表内容无法直接导出，请查看原文：[原文]({source_url})\n\n'

    text = LAKE_SHEET_RE.sub(_sheet_repl, text)

    # 2. Board / mind-map placeholders
    def _card_repl(m: re.Match[str]) -> str:
        card_html = m.group(0).lower()
        if 'board' in card_html or 'whiteboard' in card_html:
            return f'> 🎨 画板内容无法直接导出，请查看原文：[原文]({source_url})\n\n'
        if 'mindmap' in card_html or 'mind' in card_html:
            return f'> 🧠 思维导图内容无法直接导出，请查看原文：[原文]({source_url})\n\n'
        # Unknown lake card – keep as-is but wrapped in a HTML comment so it
        # does not break Obsidian rendering.
        return f'<!-- 语雀 Lake 卡片（未识别类型）\n{m.group(0)}\n-->\n\n'

    return LAKE_CARD_RE.sub(_card_repl, text)


def _looks_like_garbage(text: str) -> bool:
    """Heuristic: does the converted text look garbled / unreadable?

    We check for excessive HTML entities, stray tags, or very high ratio of
    non-printable / markup characters.
    """
    if not text:
        return True
    # Lots of un-decoded HTML entities
    if text.count('&') > 10 and re.search(r'&[a-zA-Z#0-9]+;', text):
        return True
    # Still contains raw <tags> after conversion
    raw_tags = re.findall(r'<[^>]+>', text)
    if len(raw_tags) > 5:
        return True
    return False


def convert_lake_body(
    body: str,
    body_html: Optional[str],
    namespace: str,
    slug: str,
    doc_format: Optional[str] = None,
) -> str:
    """Convert lake-format document body to Obsidian-compatible Markdown.

    Strategy:
    1. For standalone *sheet* documents (format="sheet") try markdownify on
       body_html, but fall back to a placeholder if the output looks garbled.
    2. For embedded lakesheets inside a Lake doc, extract any rendered image
       or replace with a placeholder.
    3. Replace lake cards (board / mind-map) with placeholders.
    4. If the body is short or contains heavy HTML, convert with markdownify.
    5. Post-process HTML tables.
    """
    if not body:
        body = ''

    source_url = f"https://www.yuque.com{namespace}/docs/{slug}"

    # ------------------------------------------------------------------
    # Standalone sheet-type document
    # ------------------------------------------------------------------
    if doc_format and doc_format.lower() == "sheet":
        if body_html:
            # If the HTML contains a lakesheet card, try to grab the image.
            img_match = LAKE_SHEET_IMG_RE.search(body_html)
            if img_match:
                img_url = img_match.group(1)
                return f"> 📊 数据表（原文图片）：![sheet]({img_url})\n\n> 或查看原文：[原文]({source_url})\n\n"
            # Otherwise attempt markdownify, but guard against garbage.
            md = _markdownify_html(body_html)
            if not _looks_like_garbage(md):
                return md
        return f"> 📊 数据表内容无法直接导出，请查看原文：[原文]({source_url})\n\n"

    # ------------------------------------------------------------------
    # Normal Lake doc (may contain embedded lakesheets, boards, etc.)
    # ------------------------------------------------------------------

    # Step 1: lakesheet JSON detection (rare, but official API may include it).
    _sheet_json = _try_extract_lakesheet(body)
    if _sheet_json is not None:
        return _sheet_json

    # Step 2: replace known lake cards with placeholders.
    body = _handle_lake_cards(body, namespace, slug)

    # Step 3: decide whether we need heavy HTML→Markdown conversion.
    body_stripped = body.strip()
    has_substantial_html = body.count('<') > 5 and '<div' in body.lower()
    needs_conversion = len(body_stripped) < 100 or has_substantial_html

    if needs_conversion and body_html:
        # Use markdownify for robust HTML→Markdown conversion.
        body = _markdownify_html(body_html)
        # Re-apply card placeholders (markdownify preserves blockquote text).
        body = _handle_lake_cards(body, namespace, slug)

    # Step 4: fix up tables that markdownify may have left with quirks.
    body = _convert_html_tables(body)

    return body


def _try_extract_lakesheet(body: str) -> Optional[str]:
    """Detect and convert a lakesheet (data-table) JSON payload to Markdown.

    Yuque lakesheet documents sometimes store the table data as JSON inside
    the body.  If we find a recognisable sheet structure we convert it to a
    Markdown table; otherwise return None so normal processing continues.
    """
    text = body.strip()
    if not text.startswith('{'):
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None

    # Look for sheet data in a few known shapes.
    sheet = data.get('sheet') or data.get('data', {}).get('sheet')
    if not sheet:
        return None

    # sheet may be a dict with headers + rows, or a list of rows.
    headers: list[str] = []
    rows: list[list[str]] = []

    if isinstance(sheet, dict):
        headers = [str(h) for h in sheet.get('header', [])]
        for row in sheet.get('rows', []):
            if isinstance(row, dict):
                rows.append([str(row.get(h, '')) for h in headers])
            elif isinstance(row, list):
                rows.append([str(c) for c in row])
    elif isinstance(sheet, list) and sheet:
        first = sheet[0]
        if isinstance(first, dict):
            headers = list(first.keys())
            for row in sheet:
                rows.append([str(row.get(h, '')) for h in headers])
        elif isinstance(first, list):
            headers = [str(c) for c in first]
            for row in sheet[1:]:
                rows.append([str(c) for c in row])

    if not headers:
        return None

    lines = ['| ' + ' | '.join(headers) + ' |']
    lines.append('| ' + ' | '.join(['---'] * len(headers)) + ' |')
    for row in rows:
        lines.append('| ' + ' | '.join(row) + ' |')
    return '\n'.join(lines) + '\n'


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
        body = convert_lake_body(body, doc.body_html, namespace, doc.slug, doc.format)

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
