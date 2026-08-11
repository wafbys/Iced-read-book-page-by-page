# PDF Book Analyzer

质量优先：用 **DeepSeek V4 Pro + thinking** 逐页提取知识点，并生成带页码的 Markdown 导读（含终稿审校）。

基于 [echohive42/AI-reads-books-page-by-page](https://github.com/echohive42/AI-reads-books-page-by-page) 改造。

## 怎么用

```bash
pip install -r requirements.txt
$env:DEEPSEEK_API_KEY = "sk-..."   # PowerShell
python read_books.py your_book.pdf
```

**只有一个参数：PDF。** 本配置**不计成本**，优先质量与完整性。

| 固定策略 | 值 |
|----------|-----|
| 逐页抽取 | `deepseek-v4-pro` + thinking（high） |
| 全书总结 | `deepseek-v4-pro` + thinking（max）+ **第二轮审校** |
| 产出目录 | `book_analysis/` 平铺 |

```
book_analysis/
  书名.pdf
  书名_knowledge.json
  书名.md
  书名_gold.md          # 可选人工金标准
```

## 重复执行

| 状态 | 行为 |
|------|------|
| 未抽完 | 从 `next_page` 续跑 |
| 抽完 + 已有 md | **跳过** |
| 抽完 + 无 md | 只跑总结（含审校） |
| 重写总结 | 删 `书名.md` 再执行 |
| 重抽全书 | 删 `书名_knowledge.json` |
| 人工迭代 | 润色稿另存 `书名_gold.md`，删 md 再跑 |

## 质量管线（简）

1. **抽取（Pro+thinking）**  
   书签定位 · 前两页要点 · 下页预览 · 空结果/解析失败重试 · 去重 · JSON 原子写入  

2. **总结**  
   少分块（长上下文）· 分块消化（thinking）· 合并终稿（thinking max）· **编辑审校**（对照知识点补页码/补遗漏）  

3. **可选 gold**  
   注入终稿与审校，对齐你偏好的结构与术语  

> 无 OCR；扫描件需先转文字层 PDF。  
> 费用与耗时明显高于 Flash 方案，请确保账户余额充足。

## API Key

仅环境变量 `DEEPSEEK_API_KEY`（勿写入仓库）。

```powershell
$env:DEEPSEEK_API_KEY = "sk-..."
# 永久：
[System.Environment]::SetEnvironmentVariable("DEEPSEEK_API_KEY", "sk-...", "User")
```

```cmd
set DEEPSEEK_API_KEY=sk-...
setx DEEPSEEK_API_KEY "sk-..."
```

## License

MIT — Copyright (c) 2026 wafbys  

原项目 Copyright (c) 2025 echohive，MIT。
