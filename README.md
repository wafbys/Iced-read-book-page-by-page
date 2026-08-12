# PDF Book Analyzer

逐页提取知识点并生成带页码的 Markdown 导读。默认 **auto**：预读抽样后自动选择 Flash/Pro 与 high/max。

基于 [echohive42/AI-reads-books-page-by-page](https://github.com/echohive42/AI-reads-books-page-by-page) 改造。

**要求**：Python 3.10+

## 怎么用

```bash
pip install -r requirements.txt
```

设置 API Key（任选其一）：

```bash
# Linux / macOS (bash / zsh) — 当前会话
export DEEPSEEK_API_KEY="sk-..."
```

```powershell
# Windows PowerShell — 当前会话
$env:DEEPSEEK_API_KEY = "sk-..."
```

```cmd
:: Windows CMD — 当前会话
set DEEPSEEK_API_KEY=sk-...
```

也可在项目根目录放置 `.env`（参考 `.env.example`）。程序启动时会**可选加载** `.env`，**不覆盖**已存在的环境变量。

```bash
# 默认 auto（预读评估）
python read_books.py your_book.pdf

# 固定档
python read_books.py your_book.pdf --profile economy
python read_books.py your_book.pdf --profile balanced
python read_books.py your_book.pdf --profile quality
python read_books.py your_book.pdf -p auto

# 指定产出目录（默认 ./book_analysis，相对当前工作目录）
python read_books.py your_book.pdf --out-dir ./my_out

# 危险操作确认（无指纹旧进度覆盖 PDF / 中途切换抽取模型）
python read_books.py your_book.pdf --force

# auto 跳过交互确认，直接采用预读映射（脚本/CI）
python read_books.py your_book.pdf -y
```

| 参数 | 说明 |
|------|------|
| `pdf` | PDF 路径或文件名（必填） |
| `--profile` / `-p` | `auto` \| `economy` \| `balanced` \| `quality`（默认 `auto`） |
| `--out-dir` | 产出目录（默认 `book_analysis`，相对 **当前工作目录**） |
| `--force` | 允许无指纹时覆盖 PDF 副本；抽取未完成时切换不一致的抽取模型 |
| `--yes` / `-y` | auto 模式跳过确认，直接采用预读映射策略 |

未传 `--profile` 时，可读环境变量 `READ_BOOKS_PROFILE` 作为后备。

## 策略档

| 档位 | 行为 |
|------|------|
| **auto**（默认） | 预读 → 展示分析 → **确认或改选档位** → 写入采样文件；续跑不再询问 |
| **economy** | 固定：全 Flash，无审校，**总结关闭 thinking** |
| **balanced** | 固定：Flash 抽 + Pro 结/审 high；页码稀可升 max 再审 |
| **quality** | 固定：Pro 抽+thinking；结/审 max |

**auto 机制（简）**：

1. 抽样页（约 5%/20%/40%/65%/85%）→ Flash 预读打分  
2. 代码映射为流水线（成本硬闸 + noise/terms 规则）  
3. **展示分析报告**，请你确认或改选 `economy` / `balanced` / `quality`  
4. 决议写入 **`书名_preflight.json`（采样文件）**；再次运行直接复用，**不再询问**  
5. **删除采样文件** → 重新预读并再次确认；脚本可用 `-y` 跳过确认  

**其它自动微调**：分块随页数/知识量变化；审校后页码过稀时可再 max 审一轮。
## 产出

```
book_analysis/              # 或 --out-dir 指定的目录
  书名.pdf
  书名_knowledge.json       # 含 next_page、PDF 指纹、strategy_spec
  书名_preflight.json       # auto 采样决议（删此文件可重新预读/选档）
  书名.md
  书名_gold.md              # 可选人工金标准
```

## 工作目录与进度文件

- 产出目录默认是 **进程当前工作目录** 下的 `book_analysis/`，不是脚本所在目录。换目录执行同一命令会写成另一套进度。
- 进度按 **PDF 文件名**（stem）区分：`书名_knowledge.json`。同名不同内容会通过 **SHA-256 指纹** 检测；不一致时拒绝续跑，需删 JSON 或换回原 PDF。
- 旧进度若 **缺少指纹** 且已有实质抽取内容：须加 `--force` 确认当前 PDF 后继续（随后会补写指纹）。无指纹时也不会静默用不同源文件覆盖副本。
- auto 的用户决议在 **`书名_preflight.json`**：有则跳过预读与确认；**删除该文件**才会重新分析并再次选择。  
- knowledge 里仍有 `strategy_spec`；若仅有旧进度、尚无采样文件，会静默生成采样文件以免打断续跑。  
- 抽取未完成时切换会改变抽页模型的 `--profile`：默认拒绝，避免一本 knowledge 混档；确认后可加 `--force`。
- 长书审校时若知识点过长需抽样，审校改为 **只补页码、禁止按残缺知识删初稿**。

## 重复执行

| 状态 | 行为 |
|------|------|
| 未抽完 | 从 `next_page` 续跑（`auto` 复用已存策略） |
| 抽完 + 已有 md | **跳过** |
| 抽完 + 无 md | 只跑总结（`auto` 复用已存策略） |
| 重写总结 | 删 `书名.md` 再执行（可换 `--profile`） |
| 重抽全书 | 删 `书名_knowledge.json` |
| 人工迭代 | 润色稿另存 `书名_gold.md`，删 md 再跑 |

## API Key

使用环境变量 `DEEPSEEK_API_KEY`（不要把真实 Key 提交进仓库）。

**当前会话**

| 系统 | 命令 |
|------|------|
| Linux / macOS | `export DEEPSEEK_API_KEY="sk-..."` |
| Windows PowerShell | `$env:DEEPSEEK_API_KEY = "sk-..."` |
| Windows CMD | `set DEEPSEEK_API_KEY=sk-...` |

**永久（用户级）**

```bash
# Linux / macOS — 写入 shell 配置后重开终端
echo 'export DEEPSEEK_API_KEY="sk-..."' >> ~/.bashrc   # bash
echo 'export DEEPSEEK_API_KEY="sk-..."' >> ~/.zshrc    # zsh
```

```powershell
# Windows PowerShell（用户级，新开终端生效）
[System.Environment]::SetEnvironmentVariable("DEEPSEEK_API_KEY", "sk-...", "User")
```

```cmd
:: Windows CMD（用户级，新开终端生效）
setx DEEPSEEK_API_KEY "sk-..."
```

**可选 `.env`**（项目根目录，已被 `.gitignore` 忽略）：

```bash
DEEPSEEK_API_KEY=sk-...
# READ_BOOKS_PROFILE=auto
```

## 平台支持

Windows / Linux / macOS 通用。依赖与路径 API 跨平台；注意 shell 设置环境变量的语法不同，以及产出目录相对 **CWD**。

## 测试

```bash
pip install -r requirements.txt
python -m pytest -q
```

## License

MIT — Copyright (c) 2026 wafbys  

原项目 Copyright (c) 2025 echohive，MIT。
