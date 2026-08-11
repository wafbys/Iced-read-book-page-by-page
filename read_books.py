"""
PDF 书籍分析器 — 逐页提取知识点并生成摘要。

API Key 仅从环境变量读取，切勿写入项目文件：
  DEEPSEEK_API_KEY

Flash / Pro 共用同一 Key，通过 model 参数切换：
  deepseek-v4-flash  — 更快更便宜（默认）
  deepseek-v4-pro    — 更强

用法示例：
  python read_books.py book.pdf
  python read_books.py book.pdf --model deepseek-v4-pro
  python read_books.py book.pdf --model deepseek-v4-flash --analysis-model deepseek-v4-pro
  python read_books.py path/to/book.pdf --pages 10
  python read_books.py book.pdf --interval 20   # 需要阶段摘要时再开
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pymupdf
from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    OpenAI,
    RateLimitError,
)
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

# 本地跳过：文字层过短视为空页（无 OCR，扫描件通常落在这里）
MIN_PAGE_CHARS = 40

# 摘要：按字符粗分块，避免一次性塞爆上下文
SUMMARY_CHUNK_CHARS = 80_000
API_MAX_RETRIES = 5
API_RETRY_BASE_SECONDS = 2.0

# 关闭 thinking：结构化 JSON 更稳、更省；摘要同样默认关闭
EXTRA_BODY_NO_THINKING = {"thinking": {"type": "disabled"}}

BASE_DIR = Path("book_analysis")
PDF_DIR = BASE_DIR / "pdfs"
KNOWLEDGE_DIR = BASE_DIR / "knowledge_bases"
SUMMARIES_DIR = BASE_DIR / "summaries"

# 缺 Key 时的完整配置说明（会话 / 永久 · CMD / PowerShell）
# 标题单独着色，正文保持默认色，避免整段红字刺眼
API_KEY_SETUP_HELP = """
请在系统环境中设置，不要把 Key 写进项目代码或提交到仓库。

【当前会话】关掉终端即失效
  PowerShell:
    $env:DEEPSEEK_API_KEY = "sk-..."
    echo $env:DEEPSEEK_API_KEY
  CMD:
    set DEEPSEEK_API_KEY=sk-...
    echo %DEEPSEEK_API_KEY%

【永久·用户级】新开终端后生效
  PowerShell:
    [System.Environment]::SetEnvironmentVariable(
      "DEEPSEEK_API_KEY", "sk-...", "User")
    # 当前窗口立刻生效：
    $env:DEEPSEEK_API_KEY = [System.Environment]::GetEnvironmentVariable(
      "DEEPSEEK_API_KEY", "User")
  CMD:
    setx DEEPSEEK_API_KEY "sk-..."
    （setx 不更新当前窗口，请新开 CMD/PowerShell 再运行）

【图形界面】
  设置 → 系统 → 关于 → 高级系统设置 → 环境变量 → 用户变量 → 新建
  名称：DEEPSEEK_API_KEY    值：你的 Key
  确定后重新打开终端。

更多说明见 README「配置 API Key」。
""".strip()


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


@dataclass
class KnowledgeItem:
    """单条知识点，绑定 PDF 页码（1-based，与阅读器页码一致）。"""

    page: int
    text: str

    def to_dict(self) -> dict:
        return {"page": self.page, "text": self.text}

    def as_line(self) -> str:
        return f"[第 {self.page} 页] {self.text}"


@dataclass
class KnowledgeState:
    """持久化知识点 + 续跑进度。next_page 为下一待处理页（0-based）。"""

    knowledge: list[KnowledgeItem]
    next_page: int


class PageContent(BaseModel):
    has_content: bool
    knowledge: list[str]


def normalize_knowledge_list(raw: list) -> list[KnowledgeItem]:
    """兼容旧版纯字符串列表；新版为 {page, text}。"""
    items: list[KnowledgeItem] = []
    for entry in raw:
        if isinstance(entry, dict) and "text" in entry:
            page = int(entry.get("page") or 0)
            text = str(entry["text"]).strip()
            if text:
                items.append(KnowledgeItem(page=page, text=text))
        elif isinstance(entry, str) and entry.strip():
            # 旧文件无页码
            items.append(KnowledgeItem(page=0, text=entry.strip()))
    return items


def parse_args(argv: list[str] | None = None) -> Config:
    parser = argparse.ArgumentParser(
        prog="read_books.py",
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
        default=0,
        metavar="N",
        help="每 N 页生成一次阶段摘要；0 关闭（默认，更省 API）",
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=None,
        metavar="N",
        help="只处理前 N 页（N>=1）；默认处理全书",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=(
            f"逐页分析模型：{MODEL_FLASH}（快/省）或 {MODEL_PRO}（强）；"
            "共用同一把密钥"
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
        help="清空该书已有知识点与摘要后重新开始",
    )
    args = parser.parse_args(argv)

    source = Path(args.pdf)
    if source.suffix.lower() != ".pdf":
        parser.error("文件必须是 .pdf")
    if args.pages is not None and args.pages < 1:
        parser.error("--pages 必须 >= 1")
    if args.interval < 0:
        parser.error("--interval 不能为负数（用 0 关闭阶段摘要）")

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
        # 仅标题着色，长说明用默认色，避免整屏红字刺眼
        print(colored("❌ 未找到环境变量 DEEPSEEK_API_KEY", "yellow"), file=sys.stderr)
        print(API_KEY_SETUP_HELP, file=sys.stderr)
        raise SystemExit(1)

    return OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)


def format_api_error(exc: Exception) -> str:
    """把常见 API 错误翻成可读中文说明。"""
    status = getattr(exc, "status_code", None)
    message = ""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error") or body
        if isinstance(err, dict):
            message = str(err.get("message") or "")
        elif err:
            message = str(err)
    if not message:
        message = str(exc)

    if status == 402 or "insufficient balance" in message.lower():
        return (
            "DeepSeek 账户余额不足（HTTP 402）。\n"
            "  请到 https://platform.deepseek.com/ 充值后再运行。\n"
            "  当前进度已写入 knowledge 文件，充值后直接再跑同一命令即可续跑。"
        )
    if status == 401:
        return (
            "API 密钥无效或未授权（HTTP 401）。\n"
            "  请检查环境变量 DEEPSEEK_API_KEY 是否正确、是否已启用。"
        )
    if status == 403:
        return f"无权访问该接口或模型（HTTP 403）。\n  详情：{message}"
    if status == 404:
        return (
            f"模型或接口不存在（HTTP 404）。\n"
            f"  请检查 --model / --analysis-model 名称。\n  详情：{message}"
        )
    if status == 429:
        return f"请求过于频繁（HTTP 429）。\n  详情：{message}"
    if status is not None:
        return f"API 返回错误（HTTP {status}）：{message}"
    return f"API 调用失败：{message}"


def chat_create_with_retry(client: OpenAI, **kwargs):
    """对瞬时网络/限流错误自动重试；余额不足等 4xx 直接抛出。"""
    last_error: Exception | None = None
    for attempt in range(1, API_MAX_RETRIES + 1):
        try:
            return client.chat.completions.create(**kwargs)
        except (RateLimitError, APIConnectionError, APITimeoutError) as exc:
            last_error = exc
        except APIError as exc:
            # 5xx 可重试；4xx（除限流已捕获）不重试
            status = getattr(exc, "status_code", None)
            if status is None or status < 500:
                raise
            last_error = exc

        if attempt >= API_MAX_RETRIES:
            break
        delay = API_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
        print(
            colored(
                f"⚠️  API 调用失败（第 {attempt}/{API_MAX_RETRIES} 次）："
                f"{last_error}；{delay:.0f} 秒后重试…",
                "yellow",
            )
        )
        time.sleep(delay)

    assert last_error is not None
    raise last_error


def setup_directories(config: Config) -> None:
    for directory in (PDF_DIR, KNOWLEDGE_DIR, SUMMARIES_DIR):
        directory.mkdir(parents=True, exist_ok=True)

    if config.fresh:
        stem = Path(config.pdf_name).stem
        for path in KNOWLEDGE_DIR.glob(f"{stem}_*"):
            path.unlink()
        for path in SUMMARIES_DIR.glob(f"{stem}_interval_*.md"):
            path.unlink()
        for path in SUMMARIES_DIR.glob(f"{stem}_final_*.md"):
            path.unlink()
        print(colored(f"🧹 已清空 {stem} 的旧分析结果（--fresh）", "yellow"))

    if not config.pdf_path.exists():
        if config.pdf_source.exists():
            shutil.copy2(config.pdf_source, config.pdf_path)
            print(colored(f"📄 已复制 PDF 到：{config.pdf_path}", "green"))
        else:
            raise FileNotFoundError(
                f"找不到 PDF：{config.pdf_source}（请确认路径，或把文件放在当前目录）"
            )
    elif (
        config.pdf_source.exists()
        and config.pdf_source.resolve() != config.pdf_path.resolve()
    ):
        shutil.copy2(config.pdf_source, config.pdf_path)
        print(colored(f"📄 已更新 PDF 副本：{config.pdf_path}", "green"))


def save_knowledge_state(config: Config, state: KnowledgeState) -> None:
    print(
        colored(
            f"💾 保存知识点（{len(state.knowledge)} 条，"
            f"下一页索引 next_page={state.next_page}）…",
            "blue",
        )
    )
    payload = {
        "knowledge": [item.to_dict() for item in state.knowledge],
        "next_page": state.next_page,
    }
    with open(config.knowledge_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def load_knowledge_state(config: Config) -> KnowledgeState:
    if config.knowledge_path.exists():
        print(colored("📚 加载已有知识点与进度…", "cyan"))
        with open(config.knowledge_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        raw_points = data.get("knowledge", [])
        # 兼容旧文件：无 next_page 时禁止盲从第 0 页续跑（会重复追加）
        if "next_page" not in data:
            print(
                colored(
                    "⚠️  旧版知识点文件缺少 next_page 进度字段，无法安全续跑。",
                    "yellow",
                ),
                file=sys.stderr,
            )
            print(
                "   请加上 --fresh 重新开始，或手动在 JSON 中写入：\n"
                '   "next_page": <下一待处理页的 0-based 索引>',
                file=sys.stderr,
            )
            raise SystemExit(1)
        next_page = int(data["next_page"])
        points = normalize_knowledge_list(raw_points)
        unpaged = sum(1 for p in points if p.page <= 0)
        if unpaged:
            print(
                colored(
                    f"⚠️  有 {unpaged} 条旧知识点无页码（显示为第 0 页）；"
                    "建议 --fresh 重跑以绑定页码。",
                    "yellow",
                )
            )
        print(
            colored(
                f"✅ 已加载 {len(points)} 条知识点，将从第 {next_page + 1} 页继续",
                "green",
            )
        )
        return KnowledgeState(knowledge=points, next_page=next_page)

    print(colored("🆕 从空知识点库开始", "cyan"))
    return KnowledgeState(knowledge=[], next_page=0)


def is_blank_page(page_text: str) -> bool:
    return len(page_text.strip()) < MIN_PAGE_CHARS


def process_page(
    client: OpenAI,
    config: Config,
    page_text: str,
    state: KnowledgeState,
    page_num: int,
) -> KnowledgeState:
    print(colored(f"\n📖 处理第 {page_num + 1} 页…", "yellow"))

    # 无论是否调 API，处理完本页后 next_page 前进，保证续跑正确
    next_state_page = page_num + 1

    if is_blank_page(page_text):
        print(
            colored(
                f"⏭️  本地跳过（文字层不足 {MIN_PAGE_CHARS} 个字符；本程序无 OCR）",
                "yellow",
            )
        )
        state = KnowledgeState(knowledge=state.knowledge, next_page=next_state_page)
        save_knowledge_state(config, state)
        return state

    pdf_page = page_num + 1  # 与常见 PDF 阅读器一致的 1-based 页码
    system_prompt = """你在研读一本书的某一页。请提取可学习的知识点。

以下页面请跳过（has_content=false，knowledge 为空列表）：
- 目录、章节列表
- 索引页
- 空白页
- 版权页、出版信息
- 参考文献、致谢

以下内容需要提取（has_content=true）：
- 阐明重要概念的序言
- 正文教育/论述内容
- 关键定义与概念
- 重要论点或理论
- 带上下文的例子与案例
- 重要结论或发现
- 方法论或框架
- 批判性分析或解读

对有效内容：
- 提取详细、可学习的知识点
- 保留重要引文或关键表述
- 例子需带上下文
- 保留专业术语与定义
- 单条知识点内不必写页码（系统会自动标注）

只返回一个 JSON 对象，格式固定为：
{"has_content": true或false, "knowledge": ["知识点1", "知识点2", ...]}
"""

    completion = chat_create_with_retry(
        client,
        model=config.model,
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": f"当前为 PDF 第 {pdf_page} 页。\n\n页面正文：\n{page_text}",
            },
        ],
        response_format={"type": "json_object"},
        extra_body=EXTRA_BODY_NO_THINKING,
    )

    raw = completion.choices[0].message.content or "{}"
    try:
        result = PageContent.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError) as exc:
        print(colored(f"⚠️  本页结果解析失败，已跳过：{exc}", "yellow"))
        state = KnowledgeState(knowledge=state.knowledge, next_page=next_state_page)
        save_knowledge_state(config, state)
        return state

    if result.has_content and result.knowledge:
        new_items = [
            KnowledgeItem(page=pdf_page, text=t.strip())
            for t in result.knowledge
            if t and t.strip()
        ]
        print(
            colored(
                f"✅ 提取到 {len(new_items)} 条知识点（第 {pdf_page} 页）",
                "green",
            )
        )
        knowledge = state.knowledge + new_items
    else:
        print(colored("⏭️  模型判定：本页无有效内容", "yellow"))
        knowledge = state.knowledge

    state = KnowledgeState(knowledge=knowledge, next_page=next_state_page)
    save_knowledge_state(config, state)
    return state


def chunk_items(
    items: list[KnowledgeItem], max_chars: int
) -> list[list[KnowledgeItem]]:
    """按累计字符数把知识点分成多块（保留页码）。"""
    chunks: list[list[KnowledgeItem]] = []
    current: list[KnowledgeItem] = []
    size = 0
    for item in items:
        item_len = len(item.as_line()) + 1
        if current and size + item_len > max_chars:
            chunks.append(current)
            current = []
            size = 0
        current.append(item)
        size += item_len
    if current:
        chunks.append(current)
    return chunks


def build_page_index_markdown(knowledge: list[KnowledgeItem]) -> str:
    """按页码聚合的确定性索引，便于对照 PDF 阅读。"""
    if not knowledge:
        return ""

    by_page: dict[int, list[str]] = {}
    for item in knowledge:
        by_page.setdefault(item.page, []).append(item.text)

    lines = ["## 按页知识点索引", "", "（页码为 PDF 阅读器中的页码，从 1 起。）", ""]
    for page in sorted(by_page.keys()):
        label = "未知页" if page <= 0 else f"第 {page} 页"
        lines.append(f"### {label}")
        for text in by_page[page]:
            lines.append(f"- {text}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _summarize_block(
    client: OpenAI,
    config: Config,
    lines: list[str],
    *,
    meta: bool = False,
) -> str:
    if meta:
        user_content = (
            "请将下列若干段分块摘要合并为一篇连贯、完整的 Markdown 总结"
            "（覆盖目前已有的全书内容）。"
            "合并时必须保留并统一各段中的 PDF 页码引用，格式为（第 N 页）。\n\n"
            + "\n\n---\n\n".join(lines)
        )
    else:
        user_content = (
            "以下每条知识点前已标注 PDF 页码，形如 [第 N 页]。\n"
            "请分析并总结，并在要点处标明出处页码。\n\n"
            + "\n".join(lines)
        )

    completion = chat_create_with_retry(
        client,
        model=config.analysis_model,
        messages=[
            {
                "role": "system",
                "content": """请对给定内容做简洁但详实的综合总结，使用 Markdown 格式。

格式要求：
- 用 ## 作一级小节，### 作二级小节
- 列表用项目符号
- 代码或公式用 `代码块`
- 重点用 **加粗**，术语用 *斜体*
- 重要提示用 > 引用块
- **页码对照（重要）**：输入中每条形如 [第 N 页] …；
  总结中每个重要论点、定义、例子后用中文标注出处，例如（第 12 页）或（第 12–14 页）。
  不要丢掉页码；便于读者打开 PDF 对照。

只输出 Markdown 正文，不要加「以下是摘要」之类前后缀。
不要单独再写「按页索引」大节（程序会自动附上）。""",
            },
            {"role": "user", "content": user_content},
        ],
        extra_body=EXTRA_BODY_NO_THINKING,
    )
    return completion.choices[0].message.content or ""


def analyze_knowledge_base(
    client: OpenAI,
    config: Config,
    knowledge_base: list[KnowledgeItem],
) -> str:
    if not knowledge_base:
        print(colored("\n⚠️  无知识点，跳过摘要", "yellow"))
        return ""

    chunks = chunk_items(knowledge_base, SUMMARY_CHUNK_CHARS)
    print(
        colored(
            f"\n🤔 生成摘要（{len(knowledge_base)} 条知识点，"
            f"{len(chunks)} 块，含页码）…",
            "cyan",
        )
    )

    if len(chunks) == 1:
        lines = [item.as_line() for item in chunks[0]]
        summary = _summarize_block(client, config, lines)
    else:
        partials: list[str] = []
        for i, chunk in enumerate(chunks, start=1):
            print(colored(f"   · 分块摘要 {i}/{len(chunks)}…", "cyan"))
            lines = [item.as_line() for item in chunk]
            partials.append(_summarize_block(client, config, lines))
        print(colored("   · 合并分块摘要…", "cyan"))
        summary = _summarize_block(client, config, partials, meta=True)

    print(colored("✨ 摘要生成完成", "green"))
    return summary


def save_summary(
    config: Config,
    summary: str,
    knowledge: list[KnowledgeItem] | None = None,
    is_final: bool = False,
) -> None:
    if not summary and not knowledge:
        print(colored("⏭️  无内容，跳过保存摘要", "yellow"))
        return

    stem = Path(config.pdf_name).stem
    kind = "final" if is_final else "interval"
    kind_cn = "最终" if is_final else "阶段"
    existing = list(SUMMARIES_DIR.glob(f"{stem}_{kind}_*.md"))
    next_number = len(existing) + 1
    summary_path = SUMMARIES_DIR / f"{stem}_{kind}_{next_number:03d}.md"

    page_index = build_page_index_markdown(knowledge or [])
    body = summary.strip() if summary else "（本轮无模型摘要正文）"
    pages_covered = sorted({i.page for i in (knowledge or []) if i.page > 0})
    page_range = (
        f"{pages_covered[0]}–{pages_covered[-1]}"
        if pages_covered
        else "无"
    )

    markdown_content = f"""# 书籍分析：{config.pdf_name}

- 生成时间：{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
- 摘要模型：{config.analysis_model}
- 覆盖 PDF 页码：{page_range}
- 说明：正文中的（第 N 页）对应 PDF 阅读器页码；文末附按页索引便于对照。

## 综合总结

{body}

{page_index}
---
*由 PDF 书籍分析器（DeepSeek）生成*
"""

    print(colored(f"\n📝 保存{kind_cn}摘要…", "cyan"))
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(markdown_content)
    print(colored(f"✅ 已保存：{summary_path}", "green"))


def print_run_banner(config: Config) -> None:
    interval = (
        f"每 {config.analysis_interval} 页"
        if config.analysis_interval
        else "关闭"
    )
    pages = config.test_pages if config.test_pages is not None else "全书"
    resume = "关闭（--fresh）" if config.fresh else "开启（按 knowledge.next_page）"
    print(
        colored(
            f"""
📚 PDF 书籍分析器（DeepSeek）
--------------------------------
PDF：      {config.pdf_source}
模型：     {config.model} / 摘要：{config.analysis_model}
间隔摘要： {interval}
页数上限： {pages}
续跑：     {resume}
空页：     文字层不足 {MIN_PAGE_CHARS} 字符则本地跳过（无 OCR）
API 密钥： 仅从环境变量 DEEPSEEK_API_KEY 读取
""",
            "cyan",
        )
    )


def main(argv: list[str] | None = None) -> None:
    config = parse_args(argv)
    print_run_banner(config)
    # 先校验密钥，避免未配置时仍建目录、拷 PDF
    client = create_client()

    try:
        setup_directories(config)
    except FileNotFoundError as exc:
        print(colored(f"⚠️  {exc}", "yellow"), file=sys.stderr)
        raise SystemExit(1) from exc

    state = load_knowledge_state(config)

    pdf_document = pymupdf.open(config.pdf_path)
    total_pages = pdf_document.page_count
    end_page = (
        min(config.test_pages, total_pages)
        if config.test_pages is not None
        else total_pages
    )
    start_page = state.next_page

    if start_page >= end_page:
        print(
            colored(
                f"\n✅ 进度已到第 {start_page} 页之后，"
                f"本轮目标为前 {end_page} 页，无需再处理页面。",
                "green",
            )
        )
        if state.knowledge:
            print(colored("📊 基于已有知识点生成最终摘要…", "cyan"))
            try:
                final_summary = analyze_knowledge_base(
                    client, config, state.knowledge
                )
                save_summary(
                    config, final_summary, knowledge=state.knowledge, is_final=True
                )
            except (APIStatusError, APIError, APIConnectionError, APITimeoutError) as exc:
                print(
                    colored(f"\n⚠️  {format_api_error(exc)}", "yellow"),
                    file=sys.stderr,
                )
                pdf_document.close()
                raise SystemExit(1) from None
        pdf_document.close()
        print(colored("\n✨ 处理完成 ✨", "green", attrs=["bold"]))
        return

    if start_page > 0:
        print(
            colored(
                f"\n📚 全书共 {total_pages} 页；续跑第 {start_page + 1}–{end_page} 页"
                f"（已完成前 {start_page} 页）…",
                "cyan",
            )
        )
    else:
        print(
            colored(
                f"\n📚 全书共 {total_pages} 页，将处理第 1–{end_page} 页…",
                "cyan",
            )
        )

    try:
        for page_num in range(start_page, end_page):
            page_text = pdf_document[page_num].get_text()
            state = process_page(client, config, page_text, state, page_num)

            pages_done_in_range = page_num + 1  # 全书 1-based 页码
            is_final_page = page_num + 1 == end_page

            if config.analysis_interval:
                # 按全书页码取模，与续跑一致
                is_interval = pages_done_in_range % config.analysis_interval == 0
                if is_interval and not is_final_page:
                    print(
                        colored(
                            f"\n📊 进度：{pages_done_in_range}/{end_page}",
                            "cyan",
                        )
                    )
                    interval_summary = analyze_knowledge_base(
                        client, config, state.knowledge
                    )
                    save_summary(
                        config,
                        interval_summary,
                        knowledge=state.knowledge,
                        is_final=False,
                    )

            if is_final_page:
                print(
                    colored(
                        f"\n📊 本轮最后一页（{pages_done_in_range}/{end_page}）",
                        "cyan",
                    )
                )
                final_summary = analyze_knowledge_base(
                    client, config, state.knowledge
                )
                save_summary(
                    config,
                    final_summary,
                    knowledge=state.knowledge,
                    is_final=True,
                )
    except (APIStatusError, APIError, APIConnectionError, APITimeoutError) as exc:
        print(colored(f"\n⚠️  {format_api_error(exc)}", "yellow"), file=sys.stderr)
        print(
            colored(
                f"已处理到 next_page={state.next_page}，"
                f"知识点 {len(state.knowledge)} 条已保存。",
                "cyan",
            ),
            file=sys.stderr,
        )
        pdf_document.close()
        raise SystemExit(1) from None

    pdf_document.close()
    print(colored("\n✨ 处理完成 ✨", "green", attrs=["bold"]))


if __name__ == "__main__":
    main()
