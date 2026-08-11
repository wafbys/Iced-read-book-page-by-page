# PDF Book Analyzer

逐页用 **DeepSeek Flash** 提取知识点，全书用 **Pro** 生成一篇带页码的 Markdown 总结。

基于 [echohive42/AI-reads-books-page-by-page](https://github.com/echohive42/AI-reads-books-page-by-page) 改造。

## 怎么用

```bash
pip install -r requirements.txt

# 设置 Key（不要写进项目）—— PowerShell 示例
$env:DEEPSEEK_API_KEY = "sk-..."

python read_books.py your_book.pdf
```

**只有一个参数：PDF 路径或文件名。**

| 固定策略 | 值 |
|----------|-----|
| 逐页抽取 | `deepseek-v4-flash` |
| 全书总结 | `deepseek-v4-pro` |
| 产出目录 | 全部在 `book_analysis/`（无子目录） |

同一书文件：

```
book_analysis/
  书名.pdf                 # PDF 副本
  书名_knowledge.json      # 进度 + 知识点（机器用）
  书名.md                  # 给人读的总结
  书名_gold.md             # 可选：人工金标准 / 润色稿
```

## 重复执行

| 状态 | 行为 |
|------|------|
| 抽取未完成 | 从 JSON 的 `next_page` **续跑** |
| 抽取完成 + 总结已存在 | **跳过**，提示已完成 |
| 抽取完成 + 无总结 | 只生成总结 |
| 想重写总结 | **先删除** `book_analysis/<书名>.md`，再执行 |
| 想重抽全书 | **先删除** `book_analysis/<书名>_knowledge.json`（及可选 md） |
| 人工润色迭代 | 把满意的 md 另存为 `书名_gold.md`，删 `书名.md` 再跑 → 总结会参考金标准 |

## 配置 API Key

Key 只走环境变量 `DEEPSEEK_API_KEY`。

**会话（关窗口失效）**

```powershell
$env:DEEPSEEK_API_KEY = "sk-..."
```

```cmd
set DEEPSEEK_API_KEY=sk-...
```

**永久（用户级）**

```powershell
[System.Environment]::SetEnvironmentVariable("DEEPSEEK_API_KEY", "sk-...", "User")
$env:DEEPSEEK_API_KEY = [System.Environment]::GetEnvironmentVariable("DEEPSEEK_API_KEY", "User")
```

```cmd
setx DEEPSEEK_API_KEY "sk-..."
REM 需新开终端
```

## 输出

JSON 示例（`书名_knowledge.json`）：

```json
{
  "knowledge": [
    {"page": 7, "text": "……"}
  ],
  "next_page": 84,
  "skipped": {
    "blank": [],
    "no_content": [1, 2, 3],
    "parse_error": []
  }
}
```

- `page`：PDF 阅读器页码（从 1 起）
- `next_page`：下一待处理页（0-based）；等于总页数表示抽取完成
- 读者一般**只需看** `book_analysis/<书名>.md`

总结结构：`导读` → `分题详述`（要点带页码）→ `主题索引`（术语 → 页码）。

### 质量相关实现

- **抽取（Flash，关 thinking）**
  - 跳过纯目录/索引等；要点自洽、保术语；去重；长页截断
  - 注入 **PDF 书签**定位当前章节
  - 附带 **上一页要点** 作连贯上下文（禁止照抄）
- **总结（Pro，终稿开 thinking + high effort）**
  - 分块先「节选消化」再合并终稿；强制 bullet 带页码
  - 终稿可参考书签目录命名分题
  - 若存在 `书名_gold.md`，作结构/术语/遗漏参考（事实仍以知识点页码为准）
- **读者**主要看 `book_analysis/<书名>.md`；JSON 仅进度与原料

### 人工金标准怎么用

1. 先正常跑出 `书名.md`，你自己改到满意  
2. 复制为 `book_analysis/书名_gold.md`  
3. 删除 `书名.md`，再执行 `python read_books.py 书名.pdf`  
4. 程序只重跑总结，并参考 gold（不重抽页，除非你删了 JSON）

> **无 OCR**：只读文字层。扫描件需先 OCR。  
> **thinking 更贵更慢**：仅终稿总结开启；抽取与分块中间稿关闭。

## 依赖

- Python 3.10+
- `pip install -r requirements.txt`
- [DeepSeek API Key](https://platform.deepseek.com/api_keys)

## License

MIT — Copyright (c) 2026 wafbys

原项目 Copyright (c) 2025 echohive，同样采用 MIT License。
