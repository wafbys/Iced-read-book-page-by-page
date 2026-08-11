# PDF Book Analyzer

逐页分析 PDF 书籍，用 AI 提取知识点并生成阶段性摘要与最终总结。

基于 [echohive42/AI-reads-books-page-by-page](https://github.com/echohive42/AI-reads-books-page-by-page) 改造，作为个人项目维护与扩展。

## 功能

- 逐页 PDF 分析与知识点提取
- AI 内容理解与摘要
- 按页数间隔生成进度摘要
- 知识点持久化（JSON）
- Markdown 格式摘要输出
- 彩色终端输出
- 支持断点续跑（已有 knowledge base 可继续）
- 可配置分析间隔与测试页数
- 智能过滤目录、索引等无实质内容页
- 输出目录结构清晰

## 环境要求

- Python 3.10+
- OpenAI API Key（环境变量 `OPENAI_API_KEY`）

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置 API Key

```bash
# Windows (PowerShell)
$env:OPENAI_API_KEY = "sk-..."

# macOS / Linux
export OPENAI_API_KEY="sk-..."
```

### 3. 准备 PDF

将 PDF 放在项目根目录，并在 `read_books.py` 中修改：

```python
PDF_NAME = "your_book.pdf"
```

可选配置：

| 常量 | 说明 | 默认 |
|------|------|------|
| `ANALYSIS_INTERVAL` | 每 N 页生成一次阶段摘要；`None` 跳过 | `20` |
| `TEST_PAGES` | 只处理前 N 页；`None` 处理全书 | `60` |
| `MODEL` | 逐页分析模型 | `gpt-4o-mini` |
| `ANALYSIS_MODEL` | 摘要模型（当前代码实际使用 `MODEL`） | `o1-mini` |

### 4. 运行

```bash
python read_books.py
```

## 输出结构

```
book_analysis/
├── pdfs/              # PDF 副本
├── knowledge_bases/   # 提取的知识点 JSON
└── summaries/         # 阶段摘要与最终摘要 Markdown
```

## 项目结构

```
.
├── read_books.py      # 主程序
├── requirements.txt   # 依赖
├── LICENSE            # MIT
└── README.md
```

## License

MIT — Copyright (c) 2026 wafbys

原项目 Copyright (c) 2025 echohive，同样采用 MIT License。
