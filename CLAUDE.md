# yuque2obsidian — Claude 开发手册

> 本项目是一个 Python 工具，用于把语雀（Yuque）中的全部笔记完整迁移到 Obsidian。
> 支持个人知识库 + 协作/团队空间，默认增量同步，提供 CLI 和 Gradio Web UI。

## 快速启动

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml
# 编辑 config.yaml，填入语雀 Token
python main.py --config config.yaml --full-sync
```

Token 从 https://www.yuque.com/settings/tokens 创建。

## 项目结构

```
yuque2obsidian/
├── README.md
├── requirements.txt
├── config.example.yaml          # 配置模板
├── CLAUDE.md                    # 本文件
├── main.py                      # CLI 入口
├── ui.py                        # Gradio Web UI 入口
└── yuque2obsidian/
    ├── __init__.py
    ├── api.py                   # 语雀 API 客户端（异步 + 限流 + 重试）
    ├── config.py                # 配置加载与校验（Pydantic）
    ├── models.py                # Pydantic 数据模型
    ├── storage.py               # SQLite 增量同步状态
    ├── toc.py                   # TOC 目录树解析与文件路径生成
    ├── markdown_processor.py    # Markdown 处理：图片/附件下载、链接替换、frontmatter
    ├── exporter.py              # 核心导出/同步逻辑
    ├── utils.py
    └── logger.py
```

## 技术栈

- Python 3.10+（当前环境是 Python 3.9，运行正常）
- `httpx` 异步 HTTP
- `pydantic` 数据模型与配置校验
- `PyYAML` 配置文件
- `click` CLI
- `gradio` Web UI（注意：`huggingface-hub` 必须 `<0.25`，否则 Gradio 4.x 导入失败）
- `SQLite` 增量同步状态
- `tenacity` API 重试
- `python-slugify` 文件名安全化（保留中文）

## 语雀 API 关键端点

Base URL: `https://www.yuque.com/api/v2`
Auth Header: `X-Auth-Token: <token>`

| 端点 | 用途 |
|------|------|
| `GET /api/v2/user` | 当前用户信息（含 `login`） |
| `GET /api/v2/users/{login}/repos` | 个人知识库 |
| `GET /api/v2/users/{login}/groups` | 加入的团队/空间 |
| `GET /api/v2/groups/{group_login}/repos` | 团队知识库 |
| `GET /api/v2/repos/{namespace}/toc` | 知识库目录树 |
| `GET /api/v2/repos/{namespace}/docs` | 文档列表（分页，默认 20/页） |
| `GET /api/v2/repos/{namespace}/docs/{slug}` | 文档详情，`body` 字段为 Markdown |

`namespace` 格式：`/{login}/{repo_slug}` 或 `/{group_login}/{repo_slug}`

## 核心数据流

1. `GET /user` 获取当前用户 `login`
2. 并行获取：个人知识库 + 加入的团队 → 团队知识库
3. 对每个 Repo：
   - `GET /repos/{namespace}/toc` 获取目录树
   - `GET /repos/{namespace}/docs` 获取文档列表
4. 根据 TOC 的 `uuid` / `parent_uuid` 构建树，为 `type=DOC` 节点生成文件路径
5. 对每个文档：
   - 检查 SQLite 中的 `content_updated_at`，跳过未变更文档（增量同步）
   - `GET /repos/{namespace}/docs/{slug}` 获取 `body`（Markdown）
   - 解析 Markdown 中的图片/附件链接
   - 异步下载到 `assets/`，去重（URL hash 命名）
   - 替换 Markdown 链接为相对路径
   - 生成 Obsidian frontmatter
   - 写入 `.md` 文件
   - 更新 SQLite 同步状态

## 增量同步

SQLite 表 `docs` 以 `(namespace, slug)` 为主键，记录 `title`、`content_updated_at`、`last_synced_at`、`file_path`。

- 若 `content_updated_at` > 数据库中存储值 → 重新下载
- 若文档在语雀中已删除 → 本地默认保留
- CLI 支持 `--full-sync` 强制全量同步

## 图片/附件处理

识别的资源 URL：
- `cdn.nlark.com` 或任何 `nlark.com` 域名
- `yuque.com/attachments/...`
- `yuque.com` 且带图片/附件扩展名

跳过的 URL：
- `mailto:` / `tel:`
- 语雀内部文档链接（`/docs/`）

文件命名：`{hash}_{sanitized_name}.{ext}`，保留中文。

相对路径根据文档在 Vault 中的深度自动计算，例如：
- 文档在 `Vault/知识库/目录A/文档.md` → 图片链接为 `../../assets/xxx.png`

## TOC 目录映射

TOC 节点关键字段：
- `uuid`：节点唯一 ID
- `parent_uuid` / `parent_id`：父节点 ID（null 为顶层）
- `type`：`DOC` / `TITLE` / `UNCREATED`
- `title`：节点标题
- `doc_id`：关联文档 ID（仅 DOC）
- `level`：层级深度

文件路径：
```
{output_dir}/{repo_name}/{toc_parent_titles...}/{doc_title}.md
```

文件名安全化：`slugify(name, allow_unicode=True, lowercase=False)`，保留中文字符。

## 配置说明

```yaml
yuque:
  token: "必填"
  base_url: "https://www.yuque.com/api/v2"

export:
  output_dir: "./obsidian_vault"
  assets_dir: "assets"
  include_personal: true
  include_groups: true
  incremental: true
  full_sync: false
  concurrency: 5
  rate_limit: 1.3

frontmatter:
  include_source_url: true
  include_created_at: true
  include_updated_at: true
  include_tags: true

ui:
  enabled: false
  port: 7860
```

## CLI 用法

```bash
# 首次全量导出
python main.py --config config.yaml --full-sync

# 后续增量同步
python main.py --config config.yaml

# 只导出指定知识库
python main.py --config config.yaml --repo "产品文档"

# Web UI
python ui.py --config config.yaml
```

## 核心模块职责

| 文件 | 职责 |
|------|------|
| `api.py` | 封装语雀 API：认证、分页、限流（~1.3 req/s）、指数退避重试 |
| `models.py` | `User`、`Repo`、`DocSummary`、`DocDetail`、`TocNode`、`Group`、`SyncState` |
| `storage.py` | SQLite 状态：`docs`、`repos`、`sync_log` 三张表 |
| `toc.py` | 解析 TOC 扁平数组为树，生成文件相对路径，文件名安全化 |
| `markdown_processor.py` | 扫描 Markdown/HTML 中的外链，下载图片/附件，替换为相对路径，生成 frontmatter |
| `exporter.py` | 编排整个导出流程，错误收集，统计报告 |
| `main.py` | CLI 入口 |
| `ui.py` | Gradio Web UI 入口 |

## 已知问题与注意点

1. **huggingface-hub 版本兼容性**：Gradio 4.x 需要 `huggingface-hub<0.25`，已在 `requirements.txt` 中固定。
2. **Windows 控制台中文显示**：终端可能显示乱码，但生成的文件、文件夹名、数据库内容都是正确 UTF-8，在 Obsidian 中正常。
3. **API 限流**：语雀约 5000 次/小时，工具已做限流和重试。大量文档首次导出可能较慢。
4. **图片防盗链**：部分 `cdn.nlark.com` 图片可能需登录态/Referer，下载失败会记录到 `failed_downloads.json`。
5. **语雀内部链接**：当前保留原始 URL，未自动替换为 Obsidian `[[...]]` 链接。

## 扩展方向

- 语雀内部文档链接替换为 Obsidian 内部链接 `[[...]]`
- 画板/思维导图的 Lake 格式深度转换
- 评论导出
- 双向同步（Obsidian → 语雀）

## 测试验证历史

已通过模拟 API 的端到端测试验证：
- 文档按 TOC 层级正确生成到 `知识库/目录A/文档.md`
- 图片正确下载到 `assets/` 并替换为 `../../assets/xxx.png`
- 增量同步时未变更文档会跳过，`get_doc_detail` 不会被调用
- SQLite 状态正常记录
- 中文字符在生成的文件、文件夹名、数据库中均正确保留
