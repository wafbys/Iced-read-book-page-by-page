# PDF Book Analyzer

逐页提取知识点并生成带页码的 Markdown 导读。默认 **auto**：预读抽样后自动选择 Flash/Pro 与 high/max。

基于 [echohive42/AI-reads-books-page-by-page](https://github.com/echohive42/AI-reads-books-page-by-page) 改造。

## 怎么用

```bash
pip install -r requirements.txt
$env:DEEPSEEK_API_KEY = "sk-..."

# 默认 auto（预读评估）
python read_books.py your_book.pdf

# 固定档（命令行参数）
python read_books.py your_book.pdf --profile economy
python read_books.py your_book.pdf --profile balanced
python read_books.py your_book.pdf --profile quality
python read_books.py your_book.pdf -p auto
```

| 参数 | 说明 |
|------|------|
| `pdf` | PDF 路径或文件名（必填） |
| `--profile` / `-p` | `auto` \| `economy` \| `balanced` \| `quality`（默认 `auto`） |

未传 `--profile` 时，可读环境变量 `READ_BOOKS_PROFILE` 作为后备。

## 策略档

| 档位 | 行为 |
|------|------|
| **auto**（默认） | 预读约 5 页 → 评估难度 → 动态选抽页/总结强度 |
| **economy** | 固定：全 Flash，无审校 |
| **balanced** | 固定：Flash 抽 + Pro 结/审 high；页码稀可升 max 再审 |
| **quality** | 固定：Pro 抽+thinking；结/审 max |

**auto 评估维度（简）**：难度、文本噪声、术语密度、结构复杂度 → 是否 Pro 抽页、总结 high/max、是否审校等。

**其它自动微调**：分块随页数/知识量变化；审校后页码过稀时可再 max 审一轮。

## 产出

```
book_analysis/
  书名.pdf
  书名_knowledge.json
  书名.md
  书名_gold.md             # 可选人工金标准
```

## 重复执行

| 状态 | 行为 |
|------|------|
| 未抽完 | 从 `next_page` 续跑 |
| 抽完 + 已有 md | **跳过** |
| 抽完 + 无 md | 只跑总结 |
| 重写总结 | 删 `书名.md` 再执行（可换 `--profile`） |
| 重抽全书 | 删 `书名_knowledge.json` |
| 人工迭代 | 润色稿另存 `书名_gold.md`，删 md 再跑 |

## API Key

仅环境变量 `DEEPSEEK_API_KEY`。

```powershell
$env:DEEPSEEK_API_KEY = "sk-..."
```

## License

MIT — Copyright (c) 2026 wafbys  

原项目 Copyright (c) 2025 echohive，MIT。
