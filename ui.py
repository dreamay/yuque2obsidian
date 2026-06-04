#!/usr/bin/env python3
"""Gradio Web UI for yuque2obsidian."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import gradio as gr

from yuque2obsidian.config import Config, load_config
from yuque2obsidian.exporter import Exporter
from yuque2obsidian.logger import GradioLogHandler, setup_logging

logger = logging.getLogger("yuque2obsidian")


def _load_config_safely(path: str) -> Config:
    return load_config(Path(path))


async def _sync(config_path: str, full_sync: bool, repo_filter: str) -> str:
    cfg = _load_config_safely(config_path)
    if full_sync:
        cfg.export.full_sync = True

    exporter = Exporter(cfg)
    if repo_filter.strip():
        exporter.set_repo_filter(repo_filter.strip())

    try:
        await exporter.run()
        return "Sync completed successfully."
    except Exception as exc:
        logger.exception("Sync failed")
        return f"Sync failed: {exc}"
    finally:
        await exporter.close()


def create_ui() -> gr.Blocks:
    gradio_handler = GradioLogHandler(capacity=300)
    setup_logging(level="INFO", gradio_handler=gradio_handler)

    with gr.Blocks(title="yuque2obsidian") as demo:
        gr.Markdown("# 语雀 → Obsidian 迁移工具")
        gr.Markdown(
            "填写配置文件路径（参考 config.example.yaml），点击同步即可将语雀知识库导出为 Obsidian Markdown。"
        )

        with gr.Row():
            config_path = gr.Textbox(
                label="配置文件路径",
                value="config.yaml",
                placeholder="config.yaml",
            )
            full_sync = gr.Checkbox(label="强制全量同步", value=False)
            repo_filter = gr.Textbox(
                label="仅同步指定知识库（可选）",
                placeholder="留空则同步全部",
            )

        sync_btn = gr.Button("开始同步", variant="primary")
        status = gr.Textbox(label="状态", interactive=False)
        logs = gr.Textbox(
            label="运行日志",
            lines=20,
            interactive=False,
            autoscroll=True,
        )

        async def on_click(config_path: str, full_sync: bool, repo_filter: str) -> tuple[str, str]:
            result = await _sync(config_path, full_sync, repo_filter)
            return result, gradio_handler.get_text()

        sync_btn.click(
            fn=on_click,
            inputs=[config_path, full_sync, repo_filter],
            outputs=[status, logs],
        )

        # Refresh logs periodically while sync is not running so earlier logs show up.
        demo.load(
            lambda: gradio_handler.get_text(),
            outputs=logs,
            every=1,
        )

    return demo


def main() -> None:
    import click as _click

    @_click.command()
    @_click.option("--config", "-c", default="config.yaml", help="Path to configuration file.")
    @_click.option("--port", "-p", default=None, type=int, help="Port for Gradio UI.")
    def _launch(config: str, port: int | None) -> None:
        cfg_port = 7860
        try:
            cfg = load_config(config)
            cfg_port = cfg.ui.port
        except FileNotFoundError:
            logger.warning("Config file %s not found; using defaults.", config)
        except Exception as exc:
            logger.warning("Failed to load config %s: %s; using defaults.", config, exc)

        demo = create_ui()
        demo.launch(server_name="0.0.0.0", server_port=port or cfg_port, show_error=True)

    _launch()


if __name__ == "__main__":
    main()
