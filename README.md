# PDF Book Analyzer

逐页分析 PDF 书籍，用 **DeepSeek** 提取知识点并生成阶段性摘要与最终总结。

基于 [echohive42/AI-reads-books-page-by-page](https://github.com/echohive42/AI-reads-books-page-by-page) 改造。

## 功能

- 命令行指定 PDF 文件
- 逐页知识点提取 + 阶段 / 最终摘要
- API Key **只从环境变量读取**，不写进项目
- DeepSeek OpenAI 兼容接口（默认 **V4 Flash**）
- Flash / Pro 共用同一 Key，用 `--model` 切换
- 知识点 JSON 持久化，默认支持续跑（`--fresh` 可清空重来）
- 智能跳过目录、索引等无实质内容页

## 环境要求

- Python 3.10+
- [DeepSeek API Key](https://platform.deepseek.com/api_keys)（环境变量 `DEEPSEEK_API_KEY`）

## 模型（Flash / Pro）

官方 API 文档：https://api-docs.deepseek.com/

| 模型 ID | 说明 | 默认 |
|---------|------|------|
| `deepseek-v4-flash` | 更快、更便宜（当前为 V4-Flash-0731） | ✅ 默认 |
| `deepseek-v4-pro` | 更强、更贵 | 可选 |

要点：

- **同一个** `DEEPSEEK_API_KEY` 可用于 Flash 和 Pro
- **同一个** `base_url`：`https://api.deepseek.com`
- 切换方式：只改请求里的 **model**（本项目的 `--model` / `--analysis-model`）
- 旧名 `deepseek-chat` / `deepseek-reasoner` 已进入淘汰路径，本项目不再使用

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置 API Key（不要放进项目）

```powershell
# Windows PowerShell（当前会话）
$env:DEEPSEEK_API_KEY = "sk-..."
```

```bash
# macOS / Linux
export DEEPSEEK_API_KEY="sk-..."
```

Key 只通过环境变量传入；仓库已忽略 `.env`，请勿把真实 Key 提交到 git。

### 3. 运行

```bash
# 默认：全书用 deepseek-v4-flash
python read_books.py meditations.pdf

# 只跑前 10 页，每 5 页出一次阶段摘要
python read_books.py meditations.pdf --pages 10 --interval 5

# 全流程用 Pro
python read_books.py meditations.pdf --model deepseek-v4-pro --analysis-model deepseek-v4-pro

# 逐页用 Flash 省钱，摘要用 Pro
python read_books.py meditations.pdf --model deepseek-v4-flash --analysis-model deepseek-v4-pro

# 清空该书旧结果重跑
python read_books.py path/to/book.pdf --fresh
```

### 常用参数

| 参数 | 说明 | 默认 |
|------|------|------|
| `pdf` | PDF 路径或文件名（必填） | — |
| `--interval N` | 每 N 页生成阶段摘要；`0` 关闭 | `20` |
| `--pages N` | 只处理前 N 页；省略则全书 | 全书 |
| `--model` | 逐页分析模型 | `deepseek-v4-flash` |
| `--analysis-model` | 摘要模型 | `deepseek-v4-flash` |
| `--fresh` | 清空该书已有 knowledge / summaries | 关 |

```bash
python read_books.py -h
```

运行时传入的 `--model` / `--analysis-model` 会覆盖上述默认值；Key 不变。

## 输出结构

```
book_analysis/
├── pdfs/              # PDF 副本
├── knowledge_bases/   # 知识点 JSON
└── summaries/         # 阶段 / 最终 Markdown 摘要
```

## 项目结构

```
.
├── read_books.py      # 主程序
├── requirements.txt
├── .env.example       # 仅变量名说明，勿填真实 Key
├── LICENSE
└── README.md
```

## License

MIT — Copyright (c) 2026 wafbys

原项目 Copyright (c) 2025 echohive，同样采用 MIT License。
