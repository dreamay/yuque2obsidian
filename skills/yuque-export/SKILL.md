---
name: yuque-export
description: "语雀（Yuque）笔记导出到 Obsidian 的助手。支持个人知识库 + 团队协作空间导出、增量同步、Lake 格式转换、内部链接替换。TRIGGER when: 用户提到'语雀'、'yuque'、'导出到Obsidian'、'迁移笔记'、'同步语雀文档'；用户要求配置或运行 yuque2obsidian；用户询问语雀导出的问题。SKIP: 用户讨论的是其他笔记工具（Notion、Bear、Logseq 等）的迁移；用户只是泛泛地问'怎么备份笔记'而没有提到语雀/Obsidian；项目目录下没有 yuque2obsidian 相关文件且用户未明确要求使用此工具。"
license: Apache-2.0
---

# Yuque to Obsidian Exporter

将语雀（Yuque）中的全部笔记完整迁移到 Obsidian 的 Python 工具。

---

## Project Layout

```
yuque2obsidian/
├── main.py                      # CLI 入口
├── ui.py                        # Gradio Web UI 入口
├── config.yaml                  # 用户配置文件（从 config.example.yaml 复制）
├── config.example.yaml          # 配置模板
├── requirements.txt
├── pyproject.toml
├── README.md
└── yuque2obsidian/
    ├── api.py                   # 语雀 API 客户端（异步 + 限流 + 重试）
    ├── config.py                # 配置加载与校验（Pydantic）
    ├── models.py                # Pydantic 数据模型
    ├── storage.py               # SQLite 增量同步状态
    ├── toc.py                   # TOC 目录树解析与文件路径生成
    ├── markdown_processor.py    # Markdown 处理：下载资源、链接替换、frontmatter
    ├── exporter.py              # 核心导出/同步逻辑
    ├── utils.py
    └── logger.py
```

---

## Before You Start

1. **确认项目已安装依赖**
   ```bash
   pip install -r requirements.txt
   ```

2. **确认配置文件存在**
   - 检查 `config.yaml` 是否存在（从 `config.example.yaml` 复制）
   - 确认 `config.yaml` 中包含有效的语雀 Token
   - Token 从 https://www.yuque.com/settings/tokens 创建，需有**读取知识库、文档**权限

3. **确认输出目录**
   - 默认输出到 `./obsidian_vault/`
   - 如果目录已存在且有历史数据，增量同步会跳过未变更文档

---

## Output Requirement

当用户要求导出/同步语雀笔记时，**必须**通过 CLI 调用 `main.py`：

```bash
python main.py --config config.yaml [options]
```

不要尝试用 Web UI（`ui.py`）完成批量导出任务，CLI 更适合自动化和日志追踪。

---

## Defaults

除非用户明确要求否则：

- **首次导出** → 使用 `--full-sync` 强制全量
- **后续同步** → 不使用 `--full-sync`，走增量同步
- **导出范围** → 个人知识库 + 团队协作空间（由 `config.yaml` 中的 `include_personal` / `include_groups` 控制）
- **并发数** → 保持默认 `concurrency: 5`，`rate_limit: 1.3`（约 5000 次/小时）

---

## CLI Reference

```bash
# 首次全量导出
python main.py --config config.yaml --full-sync

# 后续增量同步（只下载修改过的文档）
python main.py --config config.yaml

# 只导出指定知识库
python main.py --config config.yaml --repo "知识库名称"

# 使用自定义配置文件路径
python main.py --config /path/to/config.yaml
```

---

## Subcommands

如果用户请求是一个裸子命令（无额外描述），直接匹配下表执行：

| Subcommand | Action |
|---|---|
| `full-sync` | 执行全量同步：`python main.py --config config.yaml --full-sync` |
| `sync` / `incremental` | 执行增量同步：`python main.py --config config.yaml` |
| `status` | 查看同步状态：读取 SQLite 数据库 `{output_dir}/.yuque2obsidian.db` 中的 `docs` 和 `sync_log` 表 |

---

## Config File Structure

```yaml
yuque:
  token: "your-yuque-token-here"      # 必填
  base_url: "https://www.yuque.com/api/v2"

export:
  output_dir: "./obsidian_vault"      # Obsidian 库输出目录
  assets_dir: "assets"                # 资源文件夹名（放在各知识库内部）
  include_personal: true              # 导出个人知识库
  include_groups: true                # 导出团队/协作知识库
  incremental: true                   # 启用增量同步
  full_sync: false                    # 是否强制全量（CLI --full-sync 会覆盖）
  concurrency: 5                      # API 并发数
  rate_limit: 1.3                     # 每秒最大请求数

frontmatter:
  include_source_url: true
  include_created_at: true
  include_updated_at: true
  include_tags: true
```

---

## Export Directory Structure

```
obsidian_vault/
├── 产品文档/
│   ├── assets/
│   │   └── img_a1b2c3d4.png
│   ├── 需求文档/                    # 父文档有子节点时，放入同名文件夹
│   │   ├── 需求文档.md
│   │   ├── 子需求1.md
│   │   └── 子需求2.md
│   └── README.md
├── 技术wiki/
│   ├── assets/
│   │   └── img_x1y2z3w4.png
│   └── 后端/
│       └── API设计.md
└── .yuque2obsidian.db               # SQLite 增量同步状态
```

---

## Common Tasks

### 1. 用户说"导出语雀笔记" / "同步语雀到 Obsidian"

- 检查 `config.yaml` 是否存在且包含 token
- 询问是否是首次导出（决定是否加 `--full-sync`）
- 执行 `python main.py --config config.yaml [--full-sync]`
- 观察输出日志，汇总结果

### 2. 用户说"导出某个知识库"

- 执行 `python main.py --config config.yaml --repo "知识库名称" [--full-sync]`
- 如果知识库名不确定，先执行一次无 `--repo` 的导出，从日志中列出所有知识库名称

### 3. 用户说"重新全量导出"

- 提醒 `--full-sync` 会重新下载所有文档，覆盖本地文件
- 执行 `python main.py --config config.yaml --full-sync`

### 4. 用户遇到导出错误或文档缺失

排查清单：
- **Token 权限**：确认 Token 有读取对应知识库的权限
- **配置范围**：检查 `include_personal` / `include_groups` 设置
- **API 限流**：语雀限制约 5000 次/小时，工具已内置限流和重试
- **错误日志**：查看具体报错的 repo 和 doc
- **下载失败**：检查 `{output_dir}/failed_downloads.json`

### 5. 用户想了解导出状态

```bash
# 查看最近同步的文档
sqlite3 obsidian_vault/.yuque2obsidian.db \
  "SELECT namespace, title, last_synced_at FROM docs ORDER BY last_synced_at DESC LIMIT 20;"

# 查看同步日志
sqlite3 obsidian_vault/.yuque2obsidian.db \
  "SELECT * FROM sync_log ORDER BY started_at DESC LIMIT 5;"
```

---

## Feature Reference

| 功能 | 说明 |
|------|------|
| 增量同步 | 根据 `content_updated_at` 跳过未变更文档，状态存于 SQLite |
| 全量同步 | `--full-sync` 强制重新下载所有文档 |
| 指定知识库 | `--repo "名称"` 只导出匹配的知识库 |
| TOC 层级映射 | 按语雀目录树生成文件夹；父文档有子节点时放入同名文件夹 |
| 图片/附件下载 | 下载到各知识库 `assets/` 目录，去重（URL hash 命名） |
| 内部链接替换 | 语雀 `https://www.yuque.com/.../docs/xxx` → Obsidian `[[...]]` |
| Lake 格式转换 | 画板/思维导图生成占位提示、HTML 表格→Markdown、HTML→Markdown fallback |
| Frontmatter | 自动生成 YAML frontmatter（title、created、updated、source、tags） |
| 中文字符保留 | 文件名使用 `slugify(allow_unicode=True)`，保留中文 |

---

## API Coverage

本项目基于语雀**官方 OpenAPI v2**：

| 端点 | 用途 |
|------|------|
| `GET /api/v2/user` | 当前用户信息 |
| `GET /api/v2/users/{login}/repos` | 个人知识库 |
| `GET /api/v2/users/{login}/groups` | 加入的团队 |
| `GET /api/v2/groups/{group_login}/repos` | 团队知识库 |
| `GET /api/v2/repos/{namespace}/toc` | 目录树 |
| `GET /api/v2/repos/{namespace}/docs` | 文档列表 |
| `GET /api/v2/repos/{namespace}/docs/{slug}` | 文档详情 |

**未覆盖的功能**（需接入非官方 API，可后续扩展）：评论导出、小记导出、密码验证知识库。

---

## Reading Guide

**首次配置/导出** → 阅读「Before You Start」+「CLI Reference」

**排查导出问题** → 阅读「Common Tasks」→ Task 4

**了解导出结果结构** → 阅读「Export Directory Structure」+「Feature Reference」

**修改/扩展功能** → 阅读「Project Layout」定位相关模块源码
