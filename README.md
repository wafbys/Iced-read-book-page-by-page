# PDF Book Analyzer

逐页分析 PDF 书籍，用 **DeepSeek** 提取知识点并生成阶段性摘要与最终总结。

基于 [echohive42/AI-reads-books-page-by-page](https://github.com/echohive42/AI-reads-books-page-by-page) 改造。

## 功能

- 命令行指定 PDF 文件
- 逐页知识点提取 + 阶段 / 最终摘要
- API Key **只从环境变量读取**，不写进项目
- DeepSeek OpenAI 兼容接口（默认 **V4 Flash**）
- Flash / Pro 共用同一 Key，用 `--model` 切换
- **真正续跑**：`knowledge.json` 记录 `next_page`，中断后从下一页继续
- 文字层过短的页面**本地跳过**（无 OCR；扫描件不会白烧 API）
- 长知识点库**分块摘要再合并**，降低上下文爆掉风险
- API 限流 / 网络错误自动重试；默认关闭 thinking 以稳住 JSON、省成本
- `--fresh` 清空该书旧结果后重跑

> **无 OCR**：只读 PDF 文字层。扫描件请先 OCR 成可选中文字的 PDF，或换电子版。

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

Key 只通过环境变量 `DEEPSEEK_API_KEY` 传入；**不要**写进代码或提交到 git。仓库已忽略 `.env`。

#### Windows — 当前会话（关掉窗口即失效）

**PowerShell**

```powershell
$env:DEEPSEEK_API_KEY = "sk-..."
# 验证
echo $env:DEEPSEEK_API_KEY
```

**CMD**

```cmd
set DEEPSEEK_API_KEY=sk-...
REM 验证（注意：set 与变量名之间不要多空格）
echo %DEEPSEEK_API_KEY%
```

#### Windows — 永久（用户级，新开终端生效）

**PowerShell（推荐）**

```powershell
# 写入当前用户环境变量（永久）
[System.Environment]::SetEnvironmentVariable(
  "DEEPSEEK_API_KEY",
  "sk-...",
  "User"
)

# 让「当前这个」PowerShell 窗口立刻也能用（无需重开）
$env:DEEPSEEK_API_KEY = [System.Environment]::GetEnvironmentVariable(
  "DEEPSEEK_API_KEY", "User"
)
```

**CMD**

```cmd
REM 写入当前用户环境变量（永久）；新开 CMD/PowerShell 后生效
setx DEEPSEEK_API_KEY "sk-..."

REM 注意：setx 不会更新「当前已打开」的窗口，请新开一个终端再跑程序
```

也可用图形界面：  
`设置 → 系统 → 关于 → 高级系统设置 → 环境变量 → 用户变量 → 新建`  
变量名 `DEEPSEEK_API_KEY`，变量值为你的 Key → 确定后**重新打开**终端。

#### 删除 / 取消永久设置

```powershell
# PowerShell：删除用户级变量
[System.Environment]::SetEnvironmentVariable("DEEPSEEK_API_KEY", $null, "User")
Remove-Item Env:DEEPSEEK_API_KEY -ErrorAction SilentlyContinue
```

```cmd
REM CMD：清除用户级（setx 无法直接删，用 PowerShell 更干净；或用系统设置 GUI 删除）
setx DEEPSEEK_API_KEY ""
```

#### macOS / Linux（会话）

```bash
export DEEPSEEK_API_KEY="sk-..."
```

永久可写入 `~/.bashrc` / `~/.zshrc` 等同名 `export` 行。

### 3. 运行

```bash
# 默认：全书用 deepseek-v4-flash
python read_books.py infinite_math.pdf

# 试跑：前 3 页（默认不写阶段摘要）
python read_books.py infinite_math.pdf --pages 3

# 只跑前 10 页，并每 5 页出一次阶段摘要
python read_books.py infinite_math.pdf --pages 10 --interval 5

# 中断后直接再跑同一命令 → 从 next_page 续跑
python read_books.py infinite_math.pdf --pages 10

# 全流程用 Pro
python read_books.py book.pdf --model deepseek-v4-pro --analysis-model deepseek-v4-pro

# 逐页用 Flash 省钱，摘要用 Pro
python read_books.py book.pdf --model deepseek-v4-flash --analysis-model deepseek-v4-pro

# 清空该书旧结果重跑
python read_books.py book.pdf --fresh
```

### 常用参数

| 参数 | 说明 | 默认 |
|------|------|------|
| `pdf` | PDF 路径或文件名（必填） | — |
| `--interval N` | 每 N 页生成阶段摘要；`0` 关闭（更省 API） | `0`（关闭） |
| `--pages N` | 只处理前 N 页（`N >= 1`）；省略则全书 | 全书 |
| `--model` | 逐页分析模型 | `deepseek-v4-flash` |
| `--analysis-model` | 摘要模型 | `deepseek-v4-flash` |
| `--fresh` | 清空该书已有 knowledge / summaries | 关 |

```bash
python read_books.py -h
```

运行时传入的 `--model` / `--analysis-model` 会覆盖上述默认值；Key 不变。

## 续跑与输出

`book_analysis/knowledge_bases/<书名>_knowledge.json` 结构示例：

```json
{
  "knowledge": [
    {"page": 3, "text": "知识点……"},
    {"page": 3, "text": "另一条……"},
    {"page": 5, "text": "……"}
  ],
  "next_page": 12
}
```

- `page`：PDF 阅读器中的页码（**从 1 起**），与对照阅读一致
- `next_page`：下一待处理页的 **0-based** 索引（已处理完前 12 页则为 `12`）
- 再次运行同一 PDF（且不用 `--fresh`）会从该页继续，**不会**从头重复追加
- 摘要 Markdown 中：综合总结会引用（第 N 页）；文末附**按页知识点索引**
- 旧版无 `next_page` 的文件会被拒绝；旧版纯字符串知识点可续跑但无页码，建议 `--fresh`

```
book_analysis/
├── pdfs/              # PDF 副本
├── knowledge_bases/   # 知识点 + 进度 JSON
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
