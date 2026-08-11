"""
PDF Book Analyzer — 逐页提取知识点并生成摘要。

API Key 仅从环境变量读取，切勿写入项目文件：
  DEEPSEEK_API_KEY

Flash / Pro 共用同一 Key，通过 model 参数切换：
  deepseek-v4-flash  — 更快更便宜（默认）
  deepseek-v4-pro    — 更强

用法示例：
  python read_books.py meditations.pdf
  python read_books.py book.pdf --model deepseek-v4-pro
  python read_books.py book.pdf --model deepseek-v4-flash --analysis-model deepseek-v4-pro
  python read_books.py path/to/book.pdf --pages 10 --interval 5
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pymupdf
from openai import OpenAI
from pydantic import BaseModel, ValidationError
from termcolor import colored

# DeepSeek（OpenAI 兼容接口）
# 文档：https://api-docs.deepseek.com/
# Flash / Pro 同一 DEEPSEEK_API_KEY，同一 base_url，仅 model 不同。
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
MODEL_FLASH = "deepseek-v4-flash"
MODEL_PRO = "deepseek-v4-pro"
DEFAULT_MODEL = MODEL_FLASH
DEFAULT_ANALYSIS_MODEL = MODEL_FLASH

BASE_DIR = Path("book_analysis")
PDF_DIR = BASE_DIR / "pdfs"
KNOWLEDGE_DIR = BASE_DIR / "knowledge_bases"
SUMMARIES_DIR = BASE_DIR / "summaries"


@dataclass
class Config:
    pdf_name: str
    pdf_source: Path
    pdf_path: Path
    knowledge_path: Path
    analysis_interval: int | None
    test_pages: int | None
    model: str
    analysis_model: str
    fresh: bool


class PageContent(BaseModel):
    has_content: bool
    knowledge: list[str]


def parse_args(argv: list[str] | None = None) -> Config:
    parser = argparse.ArgumentParser(
        description="逐页分析 PDF，用 DeepSeek 提取知识点并生成摘要。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "pdf",
        help="PDF 文件路径或文件名（相对路径时在项目根目录查找）",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=20,
        metavar="N",
        help="每 N 页生成一次阶段摘要；0 表示跳过阶段摘要",
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=None,
        metavar="N",
        help="只处理前 N 页；默认处理全书",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=(
            f"逐页分析模型：{MODEL_FLASH}（快/省）或 {MODEL_PRO}（强）；"
            "同一 API Key"
        ),
    )
    parser.add_argument(
        "--analysis-model",
        default=DEFAULT_ANALYSIS_MODEL,
        help=(
            f"摘要模型：{MODEL_FLASH} 或 {MODEL_PRO}；"
            "可与 --model 不同"
        ),
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="清空该书已有 knowledge / summaries 后重新开始",
    )
    args = parser.parse_args(argv)

    source = Path(args.pdf)
    if source.suffix.lower() != ".pdf":
        parser.error("文件必须是 .pdf")

    pdf_name = source.name
    pdf_path = PDF_DIR / pdf_name
    stem = source.stem
    knowledge_path = KNOWLEDGE_DIR / f"{stem}_knowledge.json"

    return Config(
        pdf_name=pdf_name,
        pdf_source=source,
        pdf_path=pdf_path,
        knowledge_path=knowledge_path,
        analysis_interval=args.interval if args.interval > 0 else None,
        test_pages=args.pages,
        model=args.model,
        analysis_model=args.analysis_model,
        fresh=args.fresh,
    )


def create_client() -> OpenAI:
    """从环境变量读取 Key，绝不从项目文件加载密钥。"""
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        print(
            colored(
                "❌ 未找到环境变量 DEEPSEEK_API_KEY。\n"
                "   请在 shell 中设置，不要把 Key 写进项目代码或提交到仓库。\n\n"
                "   Windows PowerShell:\n"
                '     $env:DEEPSEEK_API_KEY = "sk-..."\n\n'
                "   macOS / Linux:\n"
                '     export DEEPSEEK_API_KEY="sk-..."\n',
                "red",
            ),
            file=sys.stderr,
        )
        raise SystemExit(1)

    return OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)


def setup_directories(config: Config) -> None:
    for directory in (PDF_DIR, KNOWLEDGE_DIR, SUMMARIES_DIR):
        directory.mkdir(parents=True, exist_ok=True)

    if config.fresh:
        stem = Path(config.pdf_name).stem
        for path in KNOWLEDGE_DIR.glob(f"{stem}_*"):
            path.unlink()
        for path in SUMMARIES_DIR.glob(f"{stem}_*"):
            path.unlink()
        print(colored(f"🧹 已清空 {stem} 的旧分析结果 (--fresh)", "yellow"))

    if not config.pdf_path.exists():
        if config.pdf_source.exists():
            shutil.copy2(config.pdf_source, config.pdf_path)
            print(colored(f"📄 已复制 PDF 到: {config.pdf_path}", "green"))
        else:
            raise FileNotFoundError(
                f"找不到 PDF: {config.pdf_source}（请确认路径，或把文件放在当前目录）"
            )
    elif config.pdf_source.exists() and config.pdf_source.resolve() != config.pdf_path.resolve():
        # 源文件更新时覆盖工作副本
        shutil.copy2(config.pdf_source, config.pdf_path)
        print(colored(f"📄 已更新 PDF 副本: {config.pdf_path}", "green"))


def save_knowledge_base(config: Config, knowledge_base: list[str]) -> None:
    print(colored(f"💾 保存知识点（{len(knowledge_base)} 条）...", "blue"))
    with open(config.knowledge_path, "w", encoding="utf-8") as f:
        json.dump({"knowledge": knowledge_base}, f, indent=2, ensure_ascii=False)


def load_existing_knowledge(config: Config) -> list[str]:
    if config.knowledge_path.exists():
        print(colored("📚 加载已有知识点...", "cyan"))
        with open(config.knowledge_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        points = data.get("knowledge", [])
        print(colored(f"✅ 已加载 {len(points)} 条", "green"))
        return points
    print(colored("🆕 从空知识点库开始", "cyan"))
    return []


def process_page(
    client: OpenAI,
    config: Config,
    page_text: str,
    current_knowledge: list[str],
    page_num: int,
) -> list[str]:
    print(colored(f"\n📖 处理第 {page_num + 1} 页...", "yellow"))

    system_prompt = """Analyze this page as if you're studying from a book.

SKIP content if the page contains:
- Table of contents
- Chapter listings
- Index pages
- Blank pages
- Copyright information
- Publishing details
- References or bibliography
- Acknowledgments

DO extract knowledge if the page contains:
- Preface content that explains important concepts
- Actual educational content
- Key definitions and concepts
- Important arguments or theories
- Examples and case studies
- Significant findings or conclusions
- Methodologies or frameworks
- Critical analyses or interpretations

For valid content:
- Set has_content to true
- Extract detailed, learnable knowledge points
- Include important quotes or key statements
- Capture examples with their context
- Preserve technical terms and definitions

For pages to skip:
- Set has_content to false
- Return empty knowledge list

Respond with a single JSON object only, shape:
{"has_content": true|false, "knowledge": ["point1", "point2", ...]}
"""

    completion = client.chat.completions.create(
        model=config.model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Page text:\n{page_text}"},
        ],
        response_format={"type": "json_object"},
    )

    raw = completion.choices[0].message.content or "{}"
    try:
        result = PageContent.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError) as exc:
        print(colored(f"⚠️  本页解析失败，已跳过: {exc}", "red"))
        return current_knowledge

    if result.has_content:
        print(colored(f"✅ 提取到 {len(result.knowledge)} 条知识点", "green"))
    else:
        print(colored("⏭️  跳过（无有效内容）", "yellow"))

    updated = current_knowledge + (result.knowledge if result.has_content else [])
    save_knowledge_base(config, updated)
    return updated


def analyze_knowledge_base(
    client: OpenAI,
    config: Config,
    knowledge_base: list[str],
) -> str:
    if not knowledge_base:
        print(colored("\n⚠️  无知识点，跳过摘要", "yellow"))
        return ""

    print(colored("\n🤔 生成书籍分析摘要...", "cyan"))
    completion = client.chat.completions.create(
        model=config.analysis_model,
        messages=[
            {
                "role": "system",
                "content": """Create a comprehensive summary of the provided content in a concise but detailed way, using markdown format.

Use markdown formatting:
- ## for main sections
- ### for subsections
- Bullet points for lists
- `code blocks` for any code or formulas
- **bold** for emphasis
- *italic* for terminology
- > blockquotes for important notes

Return only the markdown summary, nothing else. Do not say 'here is the summary' or anything like that before or after""",
            },
            {
                "role": "user",
                "content": "Analyze this content:\n" + "\n".join(knowledge_base),
            },
        ],
    )

    print(colored("✨ 摘要生成完成", "green"))
    return completion.choices[0].message.content or ""


def save_summary(config: Config, summary: str, is_final: bool = False) -> None:
    if not summary:
        print(colored("⏭️  无内容，跳过保存摘要", "yellow"))
        return

    stem = Path(config.pdf_name).stem
    kind = "final" if is_final else "interval"
    existing = list(SUMMARIES_DIR.glob(f"{stem}_{kind}_*.md"))
    next_number = len(existing) + 1
    summary_path = SUMMARIES_DIR / f"{stem}_{kind}_{next_number:03d}.md"

    markdown_content = f"""# Book Analysis: {config.pdf_name}
Generated on: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
Model: {config.analysis_model}

{summary}

---
*Analysis generated using PDF Book Analyzer (DeepSeek)*
"""

    print(colored(f"\n📝 保存{'最终' if is_final else '阶段'}摘要...", "cyan"))
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(markdown_content)
    print(colored(f"✅ 已保存: {summary_path}", "green"))


def print_run_banner(config: Config) -> None:
    interval = config.analysis_interval if config.analysis_interval else "关闭"
    pages = config.test_pages if config.test_pages is not None else "全书"
    print(
        colored(
            f"""
📚 PDF Book Analyzer (DeepSeek)
--------------------------------
PDF:      {config.pdf_source}
模型:     {config.model} / 摘要: {config.analysis_model}
间隔摘要: 每 {interval} 页
页数:     {pages}
续跑:     {'否 (--fresh)' if config.fresh else '是（保留已有 knowledge）'}
API Key:  仅从环境变量 DEEPSEEK_API_KEY 读取
""",
            "cyan",
        )
    )


def main(argv: list[str] | None = None) -> None:
    config = parse_args(argv)
    print_run_banner(config)

    try:
        setup_directories(config)
    except FileNotFoundError as exc:
        print(colored(f"❌ {exc}", "red"), file=sys.stderr)
        raise SystemExit(1) from exc

    client = create_client()
    knowledge_base = load_existing_knowledge(config)

    pdf_document = pymupdf.open(config.pdf_path)
    total_pages = pdf_document.page_count
    pages_to_process = (
        min(config.test_pages, total_pages)
        if config.test_pages is not None
        else total_pages
    )

    print(colored(f"\n📚 共 {total_pages} 页，将处理 {pages_to_process} 页...", "cyan"))

    for page_num in range(pages_to_process):
        page_text = pdf_document[page_num].get_text()
        knowledge_base = process_page(
            client, config, page_text, knowledge_base, page_num
        )

        is_final_page = page_num + 1 == pages_to_process

        if config.analysis_interval:
            is_interval = (page_num + 1) % config.analysis_interval == 0
            if is_interval and not is_final_page:
                print(
                    colored(
                        f"\n📊 进度: {page_num + 1}/{pages_to_process}",
                        "cyan",
                    )
                )
                interval_summary = analyze_knowledge_base(
                    client, config, knowledge_base
                )
                save_summary(config, interval_summary, is_final=False)

        if is_final_page:
            print(
                colored(
                    f"\n📊 最后一页 ({page_num + 1}/{pages_to_process})",
                    "cyan",
                )
            )
            final_summary = analyze_knowledge_base(client, config, knowledge_base)
            save_summary(config, final_summary, is_final=True)

    pdf_document.close()
    print(colored("\n✨ 处理完成 ✨", "green", attrs=["bold"]))


if __name__ == "__main__":
    main()
