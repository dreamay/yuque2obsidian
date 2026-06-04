# yuque2obsidian

把语雀（Yuque）中的全部笔记完整迁移到 Obsidian 的 Python 工具。

## 功能

- 导出**个人知识库** + **协作/团队空间**里的文档
- 默认启用**增量同步**，只更新修改过的文档（可用 `--full-sync` 强制全量）
- 按语雀**原有目录树（TOC）层级**在 Obsidian 中创建文件夹
- 自动**下载图片/附件**到 `assets/`，并把 Markdown 链接改成本地相对路径
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
│   │   ├── 2024-Q1.md
│   │   └── 2024-Q2.md
│   └── 设计规范.md
├── 技术wiki/
│   ├── 后端/
│   │   └── API设计.md
│   └── README.md
└── assets/
    ├── img_a1b2c3d4.png
    └── attach_e5f6g7h8.pdf
```

## 配置文件说明

```yaml
yuque:
  token: "必填"                      # 语雀 API Token
  base_url: "https://www.yuque.com/api/v2"

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

## 技术栈

- Python 3.10+
- `httpx` 异步 HTTP
- `pydantic` 数据校验
- `click` CLI
- `gradio` Web UI
- `SQLite` 增量同步状态

## 注意事项

1. **Token 权限**：Token 必须具有读取对应知识库的权限，否则团队/协作库会报错或缺失。
2. **API 限流**：语雀对 API 调用有限制（约 5000 次/小时），工具已内置限流和重试。如果文档数量很大，首次导出可能需要较长时间。
3. **图片防盗链**：部分 `cdn.nlark.com` 图片可能需要登录态或 Referer 才能访问。如果下载失败，会记录到 `failed_downloads.json`。
4. **特殊格式**：画板、思维导图等语雀特有格式，如果是图片链接则下载；如果是嵌入代码/iframe，则保留原始链接或 HTML 块。
5. **语雀内部链接**：当前版本保留原始 URL，后续可扩展为替换为 Obsidian 内部链接 `[[...]]`。

## License

MIT
