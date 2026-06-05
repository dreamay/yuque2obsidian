"""Core export/sync orchestration."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from yuque2obsidian.api import YuqueAPI
from yuque2obsidian.config import Config
from yuque2obsidian.markdown_processor import (
    ResourceDownloader,
    build_frontmatter,
    process_markdown,
    relative_asset_path,
)
from yuque2obsidian.markdown_processor import _markdownify_html as markdownify_html
from yuque2obsidian.markdown_processor import _looks_like_garbage as looks_like_garbage
from yuque2obsidian.models import DocDetail, DocSummary, Repo, SyncState
from yuque2obsidian.storage import Storage
from yuque2obsidian.toc import TocTree, sanitize_filename
from yuque2obsidian.utils import safe_write

logger = logging.getLogger("yuque2obsidian")


class Exporter:
    """Export Yuque docs to Obsidian-compatible Markdown files."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.api = YuqueAPI(
            token=config.yuque.token,
            base_url=config.yuque.base_url,
            concurrency=config.export.concurrency,
            rate_limit=config.export.rate_limit,
        )
        self.storage = Storage(config.db_path)
        self.downloader = ResourceDownloader(
            concurrency=config.export.concurrency,
        )
        self.output_path = config.output_path
        self._repo_filter: Optional[str] = None
        self._link_cache: dict[tuple[str, str], Optional[str]] = {}

    def _resolve_doc_link(self, namespace: str, slug: str) -> Optional[str]:
        """Cross-repo link resolver backed by SQLite state cache."""
        key = (namespace, slug)
        if key in self._link_cache:
            return self._link_cache[key]
        state = self.storage.get_doc_state(namespace, slug)
        path = state.file_path if state else None
        self._link_cache[key] = path
        return path

    async def close(self) -> None:
        await self.api.close()
        await self.downloader.close()

    def set_repo_filter(self, repo_name: Optional[str]) -> None:
        self._repo_filter = repo_name.strip() if repo_name else None

    async def run(self) -> None:
        """Run the full export/sync process."""
        log_id = self.storage.start_sync_log()
        started_at = datetime.now()
        logger.info("Starting sync at %s", started_at.isoformat())

        try:
            user = await self.api.get_user()
            logger.info("Authenticated as %s (%s)", user.name, user.login)

            repos = await self._collect_repos(user.login)
            logger.info("Collected %d repositories", len(repos))

            docs_total = 0
            docs_updated = 0
            docs_skipped = 0
            errors: list[str] = []

            for repo in repos:
                if self._repo_filter and repo.name != self._repo_filter:
                    logger.debug("Skipping repo '%s' due to filter", repo.name)
                    continue
                try:
                    t, u, s, e = await self._sync_repo(repo)
                    docs_total += t
                    docs_updated += u
                    docs_skipped += s
                    errors.extend(e)
                except Exception as exc:
                    msg = f"Failed to sync repo {repo.namespace}: {exc}"
                    logger.exception(msg)
                    errors.append(msg)

            # Persist failed downloads report.
            failed = self.downloader.get_failed()
            if failed:
                failed_path = self.output_path / "failed_downloads.json"
                failed_path.write_text(
                    json.dumps(failed, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                logger.warning("%d downloads failed; see %s", len(failed), failed_path)

            self.storage.finish_sync_log(
                log_id, docs_total, docs_updated, docs_skipped, errors
            )
            logger.info(
                "Sync finished. Total: %d, Updated: %d, Skipped: %d, Errors: %d",
                docs_total,
                docs_updated,
                docs_skipped,
                len(errors),
            )
        except Exception as exc:
            self.storage.finish_sync_log(log_id, 0, 0, 0, [str(exc)])
            raise

    async def _collect_repos(self, login: str) -> list[Repo]:
        repos: list[Repo] = []

        if self.config.export.include_personal:
            personal = await self.api.list_user_repos(login)
            repos.extend(personal)

        if self.config.export.include_groups:
            groups = await self.api.list_user_groups(login)
            group_tasks = [
                asyncio.create_task(self.api.list_group_repos(group.login))
                for group in groups
            ]
            group_results = await asyncio.gather(*group_tasks, return_exceptions=True)
            for result in group_results:
                if isinstance(result, Exception):
                    logger.warning("Failed to list group repos: %s", result)
                else:
                    repos.extend(result)

        return repos

    async def _sync_repo(
        self, repo: Repo
    ) -> tuple[int, int, int, list[str]]:
        logger.info("Syncing repo: %s (%s)", repo.name, repo.namespace)
        self.storage.upsert_repo_state(repo.namespace, repo.name, repo.type)

        toc_nodes = await self.api.get_repo_toc(repo.namespace)
        toc_tree = TocTree(toc_nodes, repo.name)

        doc_summaries = await self.api.list_repo_docs(repo.namespace)
        logger.info(
            "Repo '%s': %d docs, %d TOC nodes", repo.name, len(doc_summaries), len(toc_nodes)
        )

        # Build slug -> local file path mapping for intra-repo link rewriting.
        slug_to_path: dict[str, str] = {}
        for summary in doc_summaries:
            path = toc_tree.doc_file_path(
                summary.id, summary.title or summary.slug, repo.name
            )
            if path is None:
                path = Path(toc_tree.repo_name) / f"{summary.slug}.md"
            slug_to_path[summary.slug] = str(path.as_posix())

        # Determine docs to sync.
        docs_to_sync: list[DocSummary] = []
        for summary in doc_summaries:
            if not self.config.export.incremental or self.config.export.full_sync:
                docs_to_sync.append(summary)
                continue
            state = self.storage.get_doc_state(repo.namespace, summary.slug)
            if state is None:
                docs_to_sync.append(summary)
            elif state.content_updated_at != summary.content_updated_at:
                docs_to_sync.append(summary)
            elif state.file_path and not (self.output_path / state.file_path).exists():
                # File was manually deleted since last sync – re-export it.
                logger.info(
                    "Re-exporting %s/%s because local file was deleted",
                    repo.namespace,
                    summary.slug,
                )
                docs_to_sync.append(summary)

        docs_total = len(doc_summaries)
        docs_updated = len(docs_to_sync)
        docs_skipped = docs_total - docs_updated

        if not docs_to_sync:
            logger.info("Repo '%s' is up to date", repo.name)
            return docs_total, 0, docs_skipped, []

        # Fetch and process docs concurrently with controlled concurrency.
        semaphore = asyncio.Semaphore(self.config.export.concurrency)
        errors: list[str] = []

        async def process_one(summary: DocSummary) -> None:
            async with semaphore:
                try:
                    await self._process_doc(summary, repo, toc_tree, slug_to_path)
                except Exception as exc:
                    msg = f"Failed to process doc {repo.namespace}/{summary.slug}: {exc}"
                    logger.exception(msg)
                    errors.append(msg)

        tasks = [asyncio.create_task(process_one(summary)) for summary in docs_to_sync]
        await asyncio.gather(*tasks, return_exceptions=True)
        return docs_total, docs_updated, docs_skipped, errors

    async def _process_doc(
        self,
        summary: DocSummary,
        repo: Repo,
        toc_tree: TocTree,
        slug_to_path: dict[str, str],
    ) -> None:
        detail = await self.api.get_doc_detail(repo.namespace, summary.slug)

        # For Lake-format docs, try the unofficial web API first – it returns
        # server-converted Markdown which is usually much cleaner than local
        # HTML→Markdown conversion.
        if detail.format and detail.format.lower() == "lake" and detail.book_id:
            web_md = await self.api.get_doc_markdown_via_web_api(
                detail.slug, detail.book_id
            )
            if web_md is not None:
                detail.body = web_md
                logger.debug(
                    "Using web API markdown for lake doc %s/%s", repo.namespace, detail.slug
                )

        rel_path = toc_tree.doc_file_path(
            detail.id,
            detail.title or detail.slug,
            repo.name,
        )
        if rel_path is None:
            rel_path = Path(toc_tree.repo_name) / f"{detail.slug}.md"

        doc_file = self.output_path / rel_path

        # Assets go into the same directory as the doc, not the repo root.
        assets_path = doc_file.parent / self.config.export.assets_dir

        # Handle special document types (board / table / sheet).
        fmt = (detail.format or "").lower()
        if fmt == "board":
            final_md = await self._process_board_doc(detail, doc_file, assets_path, repo.namespace)
        elif fmt in ("table", "sheet"):
            final_md = await self._process_table_doc(detail, doc_file, assets_path, repo.namespace)
        else:
            # Standard markdown processing for lake / markdown docs.
            final_md = await process_markdown(
                detail,
                doc_file,
                repo.namespace,
                self.downloader,
                self.config,
                assets_path,
                slug_to_path,
                self._resolve_doc_link,
            )

        safe_write(doc_file, final_md)
        logger.debug("Wrote %s", doc_file)

        self.storage.upsert_doc_state(
            SyncState(
                namespace=repo.namespace,
                slug=detail.slug,
                title=detail.title,
                content_updated_at=detail.content_updated_at,
                last_synced_at=datetime.now(),
                file_path=str(rel_path.as_posix()),
            )
        )

    async def _process_board_doc(
        self,
        doc: DocDetail,
        doc_file: Path,
        assets_path: Path,
        namespace: str,
    ) -> str:
        """Export a board (画板) document.

        Yuque renders boards as HTML.  We first try to find a rendered image
        inside *body_html*; if found we download it and reference it in the
        Markdown.  Otherwise we save *body_html* as a companion `.html` file.
        """
        frontmatter = build_frontmatter(doc, namespace, self.config)
        body_html = doc.body_html or ""
        source_url = f"https://www.yuque.com{namespace}/docs/{doc.slug}"

        # 1. Try to extract a rendered image.
        img_match = re.search(
            r'<img\s+[^>]*src=["\']([^"\']+)["\'][^>]*>',
            body_html,
            re.IGNORECASE,
        )
        if img_match:
            img_url = img_match.group(1)
            asset_path = await self.downloader.download(img_url, assets_path)
            if asset_path is not None:
                rel = relative_asset_path(doc_file, asset_path)
                return f"{frontmatter}![{doc.title or '画板'}]({rel})\n\n"

        # 2. No image – save body_html as HTML file.
        html_file = doc_file.with_suffix(".html")
        html_content = (
            '<!DOCTYPE html>\n<html>\n<head>\n'
            f'<meta charset="utf-8">\n'
            f'<title>{html.escape(doc.title or "")}</title>\n'
            '</head>\n<body>\n'
            f'{body_html}\n'
            '</body>\n</html>'
        )
        safe_write(html_file, html_content)
        return (
            f"{frontmatter}> 🎨 画板已导出为 HTML：[{html_file.name}]({html_file.name})\n\n"
            f"> 或查看原文：[原文]({source_url})\n\n"
        )

    async def _process_table_doc(
        self,
        doc: DocDetail,
        doc_file: Path,
        assets_path: Path,
        namespace: str,
    ) -> str:
        """Export a table / sheet (表格 / 数据表) document.

        We try markdownify on *body_html* first.  If the result is clean we
        return it; otherwise we fall back to saving the raw HTML as a companion
        `.html` file so nothing is lost.
        """
        frontmatter = build_frontmatter(doc, namespace, self.config)
        body_html = doc.body_html or ""
        source_url = f"https://www.yuque.com{namespace}/docs/{doc.slug}"

        if body_html:
            md = markdownify_html(body_html)
            if not looks_like_garbage(md):
                return frontmatter + md + "\n\n"

        # Fallback: save raw HTML.
        html_file = doc_file.with_suffix(".html")
        html_content = (
            '<!DOCTYPE html>\n<html>\n<head>\n'
            f'<meta charset="utf-8">\n'
            f'<title>{html.escape(doc.title or "")}</title>\n'
            '</head>\n<body>\n'
            f'{body_html}\n'
            '</body>\n</html>'
        )
        safe_write(html_file, html_content)
        return (
            f"{frontmatter}> 📊 表格已导出为 HTML：[{html_file.name}]({html_file.name})\n\n"
            f"> 或查看原文：[原文]({source_url})\n\n"
        )
