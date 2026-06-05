"""TOC tree parsing and path generation."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from slugify import slugify

from yuque2obsidian.models import TocNode


class TocTree:
    """Represents a parsed Yuque TOC tree for one repo."""

    def __init__(self, nodes: list[TocNode], repo_name: str) -> None:
        self.nodes = nodes
        self.repo_name = repo_name
        self._by_uuid: dict[str, TocNode] = {}
        self._by_doc_id: dict[int, TocNode] = {}
        self._children: dict[str, list[TocNode]] = {}
        self._roots: list[TocNode] = []
        self._build_index()

    def _build_index(self) -> None:
        for node in self.nodes:
            if node.uuid:
                self._by_uuid[node.uuid] = node
            if node.doc_id is not None:
                self._by_doc_id[node.doc_id] = node

        for node in self.nodes:
            parent_id = node.effective_parent_id()
            if parent_id and parent_id in self._by_uuid:
                self._children.setdefault(parent_id, []).append(node)
            else:
                self._roots.append(node)

        # Sort children by seq, then by original order as fallback.
        for children in self._children.values():
            children.sort(key=lambda n: (n.seq or 0, self.nodes.index(n)))

    def get_node_by_doc_id(self, doc_id: int) -> Optional[TocNode]:
        return self._by_doc_id.get(doc_id)

    def get_node_path(self, node: TocNode) -> list[TocNode]:
        """Return path from root to the given node (excluding the node itself)."""
        path: list[TocNode] = []
        current = node
        while True:
            parent_id = current.effective_parent_id()
            if not parent_id or parent_id not in self._by_uuid:
                break
            parent = self._by_uuid[parent_id]
            path.append(parent)
            current = parent
        path.reverse()
        return path

    def doc_file_path(
        self,
        doc_id: int,
        doc_title: str,
        repo_name: str,
    ) -> Optional[Path]:
        """Compute relative file path for a doc within the repo.

        If a DOC node has children, place the doc itself inside a folder
        with the same name so that child docs live alongside the parent.
        """
        node = self._by_doc_id.get(doc_id)
        parts = [sanitize_filename(repo_name)]
        if node is not None:
            path_nodes = self.get_node_path(node)
            for p in path_nodes:
                if p.title:
                    parts.append(sanitize_filename(p.title))
            title = node.title or doc_title
            safe_title = sanitize_filename(title)
            # If this DOC node has children, nest the doc inside a folder
            # named after itself so child docs live in the same folder.
            if node.type == "DOC" and node.uuid in self._children:
                parts.append(safe_title)
            parts.append(safe_title + ".md")
        else:
            parts.append(sanitize_filename(doc_title) + ".md")
        return Path(*parts)


def sanitize_filename(name: str) -> str:
    """Sanitize a string to be safe as a file/folder name."""
    if not name:
        return "untitled"
    # Keep unicode characters (Chinese, etc.) and normalize whitespace.
    base = slugify(name, allow_unicode=True, lowercase=False)
    if not base:
        base = "untitled"
    # Remove Windows-forbidden chars.
    base = re.sub(r'[\\/:*?"<>|]', "-", base)
    base = base.strip(". ")
    if not base:
        base = "untitled"
    return base
