# yuque2obsidian

把语雀（Yuque）中的全部笔记完整迁移到 Obsidian 的 Python 工具。

作者：[dreamsong](https://github.com/dreamay)

## 功能

- 导出**个人知识库** + **协作/团队空间**里的文档
- 默认启用**增量同步**，只更新修改过的文档（可用 `--full-sync` 强制全量）
- 按语雀**原有目录树（TOC）层级**在 Obsidian 中创建文件夹
- 自动**下载图片/附件**到各文档目录的 `assets/`，并把 Markdown 链接改成本地相对路径
- **语雀内部文档链接**自动替换为 Obsidian `[[...]]` 内部链接
- **Lake 格式**完整支持：
  - **表格 (lakesheet)**：解压 zlib 数据，转为标准 Markdown 表格
  - **数据表 (laketable)**：导出列结构定义
  - **画板 (lakeboard)**：提取思维导图文本节点，附原文链接
  - 普通 Lake 文档：优先使用非官方 API 的服务端 Markdown 转换
- 生成 Obsidian 风格的 **YAML frontmatter**（title、created、updated、source、tags）
- 处理语雀 API **限流**和**重试**
- 提供 **CLI** 和 **Gradio Web UI** 两种使用方式

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 创建语雀 Token

登录语雀后，访问 [https://www.yuque.com/settings/tokens](https://www.yuque.com/settings/tokens) 创建一个有**读取知识库、文档**权限的 Token。

### 3. 配置

```bash
cp config.example.yaml config.yaml
```

编辑 `config.yaml`，填入你的 Token：

```yaml
yuque:
  token: "your-yuque-token-here"
  # cookie: ""  # 可选，见下方说明

export:
  output_dir: "./obsidian_vault"
```

### 4. 运行

#### CLI

```bash
# 首次全量导出
python main.py --config config.yaml --full-sync

# 后续增量同步
python main.py --config config.yaml

# 只导出指定知识库
python main.py --config config.yaml --repo "产品文档"

# 排查问题时开启 debug 日志
python main.py --config config.yaml -v
```

#### Web UI

```bash
python ui.py --config config.yaml
```

默认在 http://localhost:7860 打开界面。

## 导出后的目录结构示例

```
obsidian_vault/
├── 产品文档/
│   ├── 需求评审/
│   │   ├── assets/
│   │   │   └── img_a1b2c3d4.png
│   │   ├── 2024-Q1.md
│   │   └── 2024-Q2.md
│   ├── assets/
│   │   └── attach_e5f6g7h8.pdf
│   └── 设计规范.md
├── 技术wiki/
│   ├── 后端/
│   │   ├── assets/
│   │   │   └── img_x1y2z3w4.png
│   │   └── API设计.md
│   └── README.md
└── .yuque2obsidian.db
```

## 配置文件说明

```yaml
yuque:
  token: "必填"                      # 语雀 API Token
  base_url: "https://www.yuque.com/api/v2"
  # 浏览器 Cookie（不用填，仅测试）
  # 获取方式：浏览器登录语雀 → F12 → Network → 复制 Cookie 头的值
  cookie: ""

export:
  output_dir: "./obsidian_vault"     # Obsidian 库输出目录
  assets_dir: "assets"               # 资源文件夹名
  include_personal: true             # 导出个人知识库
  include_groups: true               # 导出团队/协作知识库
  incremental: true                  # 默认启用增量同步
  full_sync: false                   # 是否强制全量
  concurrency: 5                     # API 并发数
  rate_limit: 1.3                    # 每秒最大请求数（5000/小时 ≈ 1.39/s）

frontmatter:
  include_source_url: true
  include_created_at: true
  include_updated_at: true
  include_tags: true

ui:
  enabled: false
  port: 7860
```

## Lake 格式处理说明

语雀的 Lake 编辑器产生多种文档格式，本工具对每种做了专门处理：

| 文档格式 | `format` 字段 | 处理方式 |
|---------|-------------|---------|
| 表格 | `lakesheet` | 解压 body 中的 zlib 数据 → 解析 JSON → Markdown 表格 |
| 数据表 | `laketable` | 解析列定义 → 输出表头结构 |
| 画板/思维导图 | `lakeboard` | 提取文本节点为列表 + 原文链接 |

## 技术栈

- Python 3.10+
- `httpx` 异步 HTTP
- `pydantic` 数据校验
- `click` CLI
- `gradio` Web UI
- `SQLite` 增量同步状态
- `markdownify` HTML→Markdown fallback

## 注意事项

1. **Token 权限**：Token 必须具有读取对应知识库的权限，否则团队/协作库会报错或缺失。
2. **API 限流**：语雀对 API 调用有限制（约 5000 次/小时），工具已内置限流和重试。如果文档数量很大，首次导出可能需要较长时间。
3. **图片防盗链**：部分 `cdn.nlark.com` 图片可能需要登录态或 Referer 才能访问。如果下载失败，会记录到 `failed_downloads.json`。
4. **Cookie 配置**：表格/画板/数据表的导出**不需要** cookie（直接从官方 API 的 body 字段解析）。Cookie 仅在普通 Lake 文档需要更高质量的服务端 Markdown 转换时可选使用。
5. **Debug 日志**：遇到问题时，加 `-v` 参数可看到详细的 API 调用日志，包括认证失败、类型检测等信息。
6. **语雀内部链接**：已自动将 `https://www.yuque.com/.../docs/xxx` 替换为 Obsidian `[[...]]` 内部链接。同知识库链接通过目录树即时解析，跨知识库链接通过 SQLite 状态表解析。

## License

Apache License 2.0
