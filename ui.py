#!/usr/bin/env python3
"""Gradio Web UI for yuque2obsidian."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import gradio as gr

from yuque2obsidian.config import Config, ExportConfig, FrontmatterConfig, UiConfig, YuqueConfig, load_config
from yuque2obsidian.exporter import Exporter
from yuque2obsidian.logger import GradioLogHandler, setup_logging

logger = logging.getLogger("yuque2obsidian")


def _build_config(token: str, output_dir: str, config_path: str) -> Config:
    """Build config from UI inputs or fallback to config file."""
    # If token is provided, build config dynamically.
    if token.strip():
        return Config(
            yuque=YuqueConfig(token=token.strip()),
            export=ExportConfig(output_dir=output_dir.strip() or "./obsidian_vault"),
            frontmatter=FrontmatterConfig(),
            ui=UiConfig(),
        )
    # Otherwise fallback to config file.
    return load_config(Path(config_path))


async def _sync(token: str, output_dir: str, config_path: str, full_sync: bool, repo_filter: str) -> str:
    cfg = _build_config(token, output_dir, config_path)
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
            "直接在下方填写 Token 和导出目录即可开始同步。"
            "也可以留空 Token，从配置文件加载（参考 config.example.yaml）。"
        )

        with gr.Row():
            token = gr.Textbox(
                label="语雀 Token",
                placeholder="从 https://www.yuque.com/settings/tokens 创建",
                type="password",
            )
            output_dir = gr.Textbox(
                label="导出目录",
                value="./obsidian_vault",
                placeholder="./obsidian_vault",
            )

        with gr.Row():
            config_path = gr.Textbox(
                label="配置文件路径（可选，Token 留空时使用）",
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

        async def on_click(token: str, output_dir: str, config_path: str, full_sync: bool, repo_filter: str) -> tuple[str, str]:
            result = await _sync(token, output_dir, config_path, full_sync, repo_filter)
            return result, gradio_handler.get_text()

        sync_btn.click(
            fn=on_click,
            inputs=[token, output_dir, config_path, full_sync, repo_filter],
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
