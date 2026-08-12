# PDF Book Analyzer

逐页提取知识点并生成带页码的 Markdown 导读。默认 **auto**：预读抽样 → 分析报告 → 确认或改选档位。

基于 [echohive42/AI-reads-books-page-by-page](https://github.com/echohive42/AI-reads-books-page-by-page) 改造。

**要求**：Python 3.10+  

**目标 PDF**：中文、英文或**中英混排**的学术/技术类文本 PDF（非通用多语种/扫描 OCR 工具）。抽取会跳过目录、纯参考文献表、索引等体裁页（中英线索均识别）。

## 怎么用

```bash
pip install -r requirements.txt
```

设置 API Key（任选其一）：

```bash
# Linux / macOS (bash / zsh)
export DEEPSEEK_API_KEY="sk-..."
```

```powershell
# Windows PowerShell
$env:DEEPSEEK_API_KEY = "sk-..."
```

```cmd
:: Windows CMD
set DEEPSEEK_API_KEY=sk-...
```

也可在项目根目录（或当前目录）放 `.env`（参考 `.env.example`）。启动时可选加载，**不覆盖**已有环境变量。

```bash
# 默认 auto（预读 → 确认）
python read_books.py your_book.pdf

# 固定档（无交互确认）
python read_books.py your_book.pdf --profile economy
python read_books.py your_book.pdf --profile balanced
python read_books.py your_book.pdf --profile quality

# 指定产出目录（默认 ./book_analysis，相对当前工作目录）
python read_books.py your_book.pdf --out-dir ./my_out

# auto 跳过确认，直接采用预读映射（脚本/CI）
python read_books.py your_book.pdf -y
```

| 参数 | 说明 |
|------|------|
| `pdf` | PDF 路径或文件名（必填） |
| `--profile` / `-p` | `auto` \| `economy` \| `balanced` \| `quality`（默认 `auto`） |
| `--out-dir` | 产出目录（默认 `book_analysis`，相对 **当前工作目录**） |
| `--yes` / `-y` | auto 模式跳过确认，直接采用预读映射策略 |

未传 `--profile` 时，可读环境变量 `READ_BOOKS_PROFILE`。

## 策略档

| 档位 | 行为 |
|------|------|
| **auto**（默认） | 预读 → 展示分析 → **确认或改选** → 策略写入 knowledge；续跑复用 |
| **economy** | 固定：全 Flash，无审校，总结关闭 thinking |
| **balanced** | 固定：Flash 抽 + Pro 结/审 high；页码稀可升 max 再审 |
| **quality** | 固定：Pro 抽+thinking；结/审 max |

**auto 机制（简）**：

1. 抽样页 → Flash 预读打分  
2. 代码映射（成本硬闸 + noise/terms）  
3. 展示报告，确认或改选 economy / balanced / quality  
4. **决议写入 `书名_knowledge.json` 的 `meta`**（`strategy_spec` 等）  
5. 再次运行：有策略则复用；**删除 knowledge 可重新预读/选档**  

## 产出（每本书）

```
book_analysis/                 # 或 --out-dir
  书名.pdf                     # 工作副本
  书名_knowledge.json          # 进度 + 知识点 + 策略决议 + 指纹
  书名.md                      # 导读成品
  书名_gold.md                 # 可选人工金标准
```

**没有单独的 preflight 文件。** 策略与进度在同一个 knowledge 里：删进度 = 可重选策略；只删 md = 只重总结。

（若目录里还有旧的 `书名_preflight.json`，程序会读一次并迁入 knowledge，之后可手动删掉。）

## 工作目录与进度

- 产出目录相对 **进程当前工作目录**，不是脚本所在目录。  
- 进度按 PDF **文件名**区分；同名不同内容靠 **SHA-256 指纹**拦截。  
- 指纹不一致 / 缺指纹 / 中途换抽取模型：直接拒绝 → **删 knowledge 后重跑**（无 `--force`，靠删产物重置）。  

## 怎么读产物（自用）

三层分工：

| 文件 | 角色 |
|------|------|
| **原书 PDF** | 完整内容；精读以它为准 |
| **`书名.md`** | 带页码的导读/地图：问题意识、章节骨架、关键页 |
| **`书名_knowledge.json`** | 页级知识点清单 + 进度/策略；比 md 细，仍可能漏页内次要句 |

**推荐用法**：用 md 导航 → 按页码回 PDF 读原文。主线结构一般够用。  

**不必指望**：

- md 的页码穷尽所有「值得读」的页（长书导读是**抽样式地图**，有条目的页也可能没在 md 里出现）  
- md ≈ 全书笔记复述（条目会被强压缩；例如两百多页书可有上千条 knowledge，md 仍是一篇短导读）  
- 纯扫描/无文字层 PDF、小说赏析、任意小语种——非设计目标  

**抽取会刻意跳过**（中英线索）：整页目录、纯参考文献表、书末索引、版权/空白等；正文里的简短文献引用仍会抽。  

**auto**：难书偏 Pro/max，清晰书可 Flash 抽 + high 总结；决议在 knowledge 的 `meta` 里，续跑不重复预读。  

## 重复执行

| 状态 | 行为 |
|------|------|
| 未抽完 | 从 `next_page` 续跑（复用 knowledge 中策略） |
| 有解析失败页 | 每轮重访 `skipped_parse` 一次 |
| 抽完 + 已有 md | **跳过** |
| 抽完 + 无 md | 只跑总结 |
| 重写总结 | 删 `书名.md` 再跑（可换 `--profile` 影响总结侧） |
| 重抽 / 重选策略 | 删 `书名_knowledge.json` |
| 人工迭代 | 润色稿另存 `书名_gold.md`，删 md 再跑 |

## API Key

使用环境变量 `DEEPSEEK_API_KEY`（不要把真实 Key 提交进仓库）。

| 系统 | 命令 |
|------|------|
| Linux / macOS | `export DEEPSEEK_API_KEY="sk-..."` |
| Windows PowerShell | `$env:DEEPSEEK_API_KEY = "sk-..."` |
| Windows CMD | `set DEEPSEEK_API_KEY=sk-...` |

## 平台支持

Windows / Linux / macOS 通用。注意 shell 环境变量语法不同，以及产出目录相对 **CWD**。

## 测试

```bash
pip install -r requirements.txt
python -m pytest -q
```

## License

MIT — Copyright (c) 2026 wafbys  

原项目 Copyright (c) 2025 echohive，MIT。
