"""
PDF 书籍分析器 — 逐页提取知识点，生成一篇带页码的总结。

用法：
  python read_books.py book.pdf

固定策略：
  - 逐页抽取：deepseek-v4-flash
  - 全书总结：deepseek-v4-pro
  - JSON 只作进度与知识点仓库
  - 产出均在 book_analysis/ 同一目录：
      <书名>.pdf / <书名>_knowledge.json / <书名>.md
  - 可选人工金标准：book_analysis/<书名>_gold.md
    （总结时作结构/要点参考；重写总结前仍须删除 <书名>.md）
  - 抽页已完成且总结已存在 → 直接跳过
  - 总结已存在又要重写 → 请先删除该 md 再执行

API Key 仅从环境变量 DEEPSEEK_API_KEY 读取，切勿写入项目。
"""

from __future__ import annotations

import argparse
import json
import os
import re
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

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
MODEL_EXTRACT = "deepseek-v4-flash"  # 逐页「摘要/抽取」
MODEL_SUMMARY = "deepseek-v4-pro"  # 全书「总结」

MIN_PAGE_CHARS = 40
# 分块宜略小，便于分块消化时仍能带上足够页码上下文
SUMMARY_CHUNK_CHARS = 36_000
MAX_PAGE_TEXT_CHARS = 12_000  # 单页过长时截断，避免噪声淹没要点
NEIGHBOR_CONTEXT_ITEMS = 6  # 抽取时附带上页要点条数
API_MAX_RETRIES = 5
API_RETRY_BASE_SECONDS = 2.0
# 抽取关 thinking 省钱；总结开 thinking 提质（DeepSeek V4）
EXTRA_BODY_NO_THINKING = {"thinking": {"type": "disabled"}}
EXTRA_BODY_THINKING = {"thinking": {"type": "enabled"}}
EXTRACT_TEMPERATURE = 0.2
SUMMARY_TEMPERATURE = 0.3
# thinking 会占 completion 额度，总结需足够大的 max_tokens
SUMMARY_MAX_TOKENS = 16_384
SUMMARY_REASONING_EFFORT = "high"
GOLD_MAX_CHARS = 20_000  # 注入终稿的人工金标准上限

# 所有产出平铺在同一目录，不再分子目录
BASE_DIR = Path("book_analysis")

API_KEY_SETUP_HELP = """
请在系统环境中设置 DEEPSEEK_API_KEY，不要把 Key 写进项目。

【当前会话】
  PowerShell:  $env:DEEPSEEK_API_KEY = "sk-..."
  CMD:         set DEEPSEEK_API_KEY=sk-...

【永久·用户级】
  PowerShell:
    [System.Environment]::SetEnvironmentVariable(
      "DEEPSEEK_API_KEY", "sk-...", "User")
  CMD:  setx DEEPSEEK_API_KEY "sk-..."  （需新开终端）

详见 README「配置 API Key」。
""".strip()


@dataclass
class Config:
    pdf_name: str
    pdf_source: Path
    pdf_path: Path
    knowledge_path: Path
    summary_path: Path
    gold_path: Path  # 可选：人工修订金标准 md


@dataclass
class KnowledgeItem:
    page: int
    text: str

    def to_dict(self) -> dict:
        return {"page": self.page, "text": self.text}

    def as_line(self) -> str:
        return f"[第 {self.page} 页] {self.text}"


@dataclass
class KnowledgeState:
    knowledge: list[KnowledgeItem]
    next_page: int  # 下一待处理页，0-based
    skipped_blank: list[int]
    skipped_model: list[int]
    skipped_parse: list[int]


class PageContent(BaseModel):
    has_content: bool
    knowledge: list[str]


EXTRACT_SYSTEM_PROMPT = """你是严谨的读书笔记助手。根据**单页**正文提取可复习的知识点。

## 跳过（has_content=false，knowledge=[]）
仅当本页**几乎全是**下列内容时跳过：
目录/章节列表、书末索引、空白、版权页、纯出版信息、纯参考文献表、纯致谢名单。
若本页在目录之外仍有实质定义或论述，必须提取。

## 提取（has_content=true）
关注：定义、核心命题、论证步骤、对比/权衡、方法论、重要例子、结论、对常见误区的批评。
忽略：无信息页眉页脚、重复排版符号、纯装饰性编号（除非构成论点）。

若提供「目录/书签定位」或「邻页已提取要点」，仅作**连贯性参考**：
- 用它们理解本章语境与指代，但知识点必须来自**本页正文**
- 不要重复抄写邻页要点，除非本页有实质推进或新表述

## 写法（每条一条完整句子或短段落）
- 自洽：不看上下文也能懂；必要时补全主语
- 具体：写清「是什么 / 为什么 / 代价或条件」，避免「作者讨论了 X」这种空话
- 保真：术语与缩写沿用原文（可中英并列，如 无状态（Stateless））；重要短引文可保留
- 粒度：实质页通常 3–8 条；信息少则少提，勿注水；信息密可到 10 条
- 不要在条目里写页码（系统会标注）
- 不要合并无关要点成一条

只返回 JSON：
{"has_content": true或false, "knowledge": ["……", "……"]}
"""

# 分块阶段：只要「带页码的节选消化」，不要主题索引（终稿再统一做）
PARTIAL_SUMMARY_PROMPT = """你在做全书总结的**中间稿**。输入是带 [第 N 页] 的知识点片段。

写 Markdown 节选消化（不要主题索引，不要书名级大标题）：
1. 用 ## / ### 按主题或原书逻辑组织本批内容
2. **每个 bullet 末尾必须有**（第 N 页）或（第 N–M 页）；页码只能来自输入
3. 重要概念写清定义 + 关键属性/权衡，勿写成纯名词清单
4. 术语沿用原文；可中英并列
5. 本批有什么写什么，不要臆造未出现的章节

只输出本节选消化正文。
"""

FINAL_SUMMARY_PROMPT = """你是技术书导读作者。根据输入（知识点或中间稿）写成**一篇完整、可对照 PDF 阅读**的 Markdown 总结。

## 必须结构（按此顺序，不要额外书名级重复标题）
1. `## 导读`：用 1 短段说明全书在解决什么问题、主线结论（可含 1–3 个关键页码）
2. `## 分题详述`：用 ### 分主题；主题名尽量贴近原书概念，按逻辑递进而非随意罗列
3. `## 主题索引`：词条 → 页码，如 `- 无状态（Stateless）：42–43`  
   只写词条与页码，按主题或拼音/字母大致排序；**禁止**按页倾倒全部原文知识点

## 页码（硬性）
- 输入含 [第 N 页] 或（第 N 页）；**分题详述中每个 bullet 末尾必须有出处页码**
- 连续论述的每个独立定义/命题至少标注一次
- 禁止编造未在输入中出现的页码；可用页码范围（第 12–14 页）

## 质量
- 前、中、后部内容都要有足够比重；中间「分类/调查」类章节须有「是什么 + 属性/权衡」，禁止纯目录体
- 区分「定义」「约束/原则」「工程后果/反例」（若输入有）
- 术语与缩写保真；宁可少写不可写错
- 详实但克制：宁可结构清晰，不要空洞排比

只输出 Markdown 正文，不要「以下是总结」等套话。
"""


def _unique_sorted_pages(pages: list[int]) -> list[int]:
    return sorted({int(p) for p in pages if int(p) > 0})


def empty_state() -> KnowledgeState:
    return KnowledgeState(
        knowledge=[],
        next_page=0,
        skipped_blank=[],
        skipped_model=[],
        skipped_parse=[],
    )


def normalize_knowledge_list(raw: list) -> list[KnowledgeItem]:
    items: list[KnowledgeItem] = []
    for entry in raw:
        if isinstance(entry, dict) and "text" in entry:
            page = int(entry.get("page") or 0)
            text = str(entry["text"]).strip()
            if text:
                items.append(KnowledgeItem(page=page, text=text))
        elif isinstance(entry, str) and entry.strip():
            items.append(KnowledgeItem(page=0, text=entry.strip()))
    return items


def parse_args(argv: list[str] | None = None) -> Config:
    parser = argparse.ArgumentParser(
        prog="read_books.py",
        description=(
            "逐页用 Flash 提取知识点，全书用 Pro 生成总结。"
            "只需指定 PDF；产出在 book_analysis/ 下同目录。"
        ),
    )
    parser.add_argument(
        "pdf",
        help="PDF 路径或文件名",
    )
    args = parser.parse_args(argv)

    source = Path(args.pdf)
    if source.suffix.lower() != ".pdf":
        parser.error("文件必须是 .pdf")

    pdf_name = source.name
    stem = source.stem
    return Config(
        pdf_name=pdf_name,
        pdf_source=source,
        pdf_path=BASE_DIR / pdf_name,
        knowledge_path=BASE_DIR / f"{stem}_knowledge.json",
        summary_path=BASE_DIR / f"{stem}.md",
        gold_path=BASE_DIR / f"{stem}_gold.md",
    )


def create_client() -> OpenAI:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        print(colored("❌ 未找到环境变量 DEEPSEEK_API_KEY", "yellow"), file=sys.stderr)
        print(API_KEY_SETUP_HELP, file=sys.stderr)
        raise SystemExit(1)
    return OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)


def format_api_error(exc: Exception) -> str:
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
            "  请到 https://platform.deepseek.com/ 充值。\n"
            "  进度已保存在 knowledge JSON，充值后直接再跑同一命令即可续跑。"
        )
    if status == 401:
        return "API 密钥无效（HTTP 401）。请检查 DEEPSEEK_API_KEY。"
    if status == 429:
        return f"请求过于频繁（HTTP 429）。\n  详情：{message}"
    if status is not None:
        return f"API 返回错误（HTTP {status}）：{message}"
    return f"API 调用失败：{message}"


def chat_create_with_retry(client: OpenAI, **kwargs):
    last_error: Exception | None = None
    for attempt in range(1, API_MAX_RETRIES + 1):
        try:
            return client.chat.completions.create(**kwargs)
        except (RateLimitError, APIConnectionError, APITimeoutError) as exc:
            last_error = exc
        except APIError as exc:
            status = getattr(exc, "status_code", None)
            if status is None or status < 500:
                raise
            last_error = exc

        if attempt >= API_MAX_RETRIES:
            break
        delay = API_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
        print(
            colored(
                f"⚠️  API 失败（{attempt}/{API_MAX_RETRIES}）："
                f"{last_error}；{delay:.0f}s 后重试…",
                "yellow",
            )
        )
        time.sleep(delay)

    assert last_error is not None
    raise last_error


def setup_directories(config: Config) -> None:
    BASE_DIR.mkdir(parents=True, exist_ok=True)

    if not config.pdf_path.exists():
        if config.pdf_source.exists():
            shutil.copy2(config.pdf_source, config.pdf_path)
            print(colored(f"📄 已复制 PDF → {config.pdf_path}", "green"))
        else:
            raise FileNotFoundError(
                f"找不到 PDF：{config.pdf_source}"
            )
    elif (
        config.pdf_source.exists()
        and config.pdf_source.resolve() != config.pdf_path.resolve()
    ):
        shutil.copy2(config.pdf_source, config.pdf_path)
        print(colored(f"📄 已更新 PDF 副本 → {config.pdf_path}", "green"))


def save_knowledge_state(config: Config, state: KnowledgeState) -> None:
    print(
        colored(
            f"💾 保存进度（{len(state.knowledge)} 条，"
            f"next_page={state.next_page}）…",
            "blue",
        )
    )
    payload = {
        "knowledge": [item.to_dict() for item in state.knowledge],
        "next_page": state.next_page,
        "skipped": {
            "blank": _unique_sorted_pages(state.skipped_blank),
            "no_content": _unique_sorted_pages(state.skipped_model),
            "parse_error": _unique_sorted_pages(state.skipped_parse),
        },
    }
    with open(config.knowledge_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def load_knowledge_state(config: Config) -> KnowledgeState:
    if not config.knowledge_path.exists():
        print(colored("🆕 新建进度", "cyan"))
        return empty_state()

    print(colored("📚 加载进度 JSON…", "cyan"))
    with open(config.knowledge_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if "next_page" not in data:
        print(
            colored(
                "⚠️  旧 knowledge 无 next_page，请删除该 JSON 后重跑。",
                "yellow",
            ),
            file=sys.stderr,
        )
        raise SystemExit(1)

    skipped = data.get("skipped") or {}
    points = normalize_knowledge_list(data.get("knowledge") or [])
    next_page = int(data["next_page"])
    print(
        colored(
            f"✅ {len(points)} 条知识点，下一页 = 第 {next_page + 1} 页",
            "green",
        )
    )
    return KnowledgeState(
        knowledge=points,
        next_page=next_page,
        skipped_blank=list(skipped.get("blank") or []),
        skipped_model=list(skipped.get("no_content") or []),
        skipped_parse=list(skipped.get("parse_error") or []),
    )


def is_blank_page(page_text: str) -> bool:
    return len(page_text.strip()) < MIN_PAGE_CHARS


def clean_page_text(page_text: str) -> str:
    """归一空白并限制极端长页，减少抽取噪声。"""
    text = page_text.replace("\x00", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    if len(text) > MAX_PAGE_TEXT_CHARS:
        head = MAX_PAGE_TEXT_CHARS // 2
        tail = MAX_PAGE_TEXT_CHARS - head
        text = (
            text[:head]
            + "\n\n…[中间原文过长已省略]…\n\n"
            + text[-tail:]
        )
    return text


def dedupe_knowledge(items: list[KnowledgeItem]) -> list[KnowledgeItem]:
    """去掉完全相同的条目（同页或跨页逐字重复）。"""
    seen: set[str] = set()
    out: list[KnowledgeItem] = []
    for item in items:
        key = re.sub(r"\s+", " ", item.text).strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def load_pdf_toc(pdf_document: pymupdf.Document) -> list[tuple[int, str, int]]:
    """返回 [(level, title, page_1based), ...]。无书签则空列表。"""
    try:
        raw = pdf_document.get_toc(simple=True) or []
    except Exception:
        return []
    toc: list[tuple[int, str, int]] = []
    for entry in raw:
        if not entry or len(entry) < 3:
            continue
        level, title, page = entry[0], str(entry[1]).strip(), int(entry[2])
        if title and page > 0:
            toc.append((int(level), title, page))
    return toc


def outline_context_for_page(
    toc: list[tuple[int, str, int]], pdf_page: int
) -> str:
    """根据书签推断当前页所在章节路径。"""
    if not toc:
        return ""

    # 不晚于当前页的最近书签链
    active: list[tuple[int, str, int]] = []
    for level, title, page in toc:
        if page > pdf_page:
            break
        while active and active[-1][0] >= level:
            active.pop()
        active.append((level, title, page))

    # 即将到来的下一书签（可选）
    nxt = None
    for level, title, page in toc:
        if page > pdf_page:
            nxt = (level, title, page)
            break

    if not active and not nxt:
        return ""

    lines = ["【目录/书签定位】（仅供理解章节语境，知识点须来自本页正文）"]
    if active:
        path = " › ".join(t for _, t, _ in active)
        lines.append(f"- 当前位置：{path}（本节自第 {active[-1][2]} 页）")
    if nxt:
        lines.append(f"- 下一书签：{nxt[1]}（第 {nxt[2]} 页）")
    return "\n".join(lines)


def neighbor_context(
    state: KnowledgeState, pdf_page: int, limit: int = NEIGHBOR_CONTEXT_ITEMS
) -> str:
    """上一页（及紧邻）已提取要点，帮助连贯，禁止照抄。"""
    if pdf_page <= 1 or not state.knowledge:
        return ""
    prev_page = pdf_page - 1
    prev_items = [i for i in state.knowledge if i.page == prev_page]
    if not prev_items:
        # 若上页被跳过，取更早最近一页
        earlier = [i for i in state.knowledge if 0 < i.page < pdf_page]
        if not earlier:
            return ""
        prev_page = max(i.page for i in earlier)
        prev_items = [i for i in earlier if i.page == prev_page]

    tail = prev_items[-limit:]
    lines = [
        f"【邻页已提取要点】（来自第 {prev_page} 页，仅供连贯；"
        f"勿重复抄写，除非本页有新推进）"
    ]
    for item in tail:
        lines.append(f"- {item.text}")
    return "\n".join(lines)


def load_gold_notes(gold_path: Path) -> str:
    """可选人工金标准 / 修订笔记，注入终稿提示。"""
    if not gold_path.exists():
        return ""
    try:
        text = gold_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        print(colored(f"⚠️  无法读取金标准 {gold_path}：{exc}", "yellow"))
        return ""
    if not text:
        return ""
    if len(text) > GOLD_MAX_CHARS:
        text = text[:GOLD_MAX_CHARS] + "\n\n…[金标准过长已截断]…"
    print(colored(f"📌 已加载人工金标准：{gold_path}", "cyan"))
    return text


def process_page(
    client: OpenAI,
    config: Config,
    page_text: str,
    state: KnowledgeState,
    page_num: int,
    total_pages: int,
    toc: list[tuple[int, str, int]],
) -> KnowledgeState:
    pdf_page = page_num + 1
    next_state_page = page_num + 1
    print(colored(f"\n📖 第 {pdf_page}/{total_pages} 页…", "yellow"))

    cleaned = clean_page_text(page_text)
    if is_blank_page(cleaned):
        print(colored("⏭️  文字过短，跳过（无 OCR）", "yellow"))
        state = KnowledgeState(
            knowledge=state.knowledge,
            next_page=next_state_page,
            skipped_blank=state.skipped_blank + [pdf_page],
            skipped_model=state.skipped_model,
            skipped_parse=state.skipped_parse,
        )
        save_knowledge_state(config, state)
        return state

    ctx_parts = [
        f"全书共 {total_pages} 页；当前为第 {pdf_page} 页。",
    ]
    outline = outline_context_for_page(toc, pdf_page)
    if outline:
        ctx_parts.append(outline)
    neighbor = neighbor_context(state, pdf_page)
    if neighbor:
        ctx_parts.append(neighbor)
    ctx_parts.append(f"页面正文：\n{cleaned}")

    completion = chat_create_with_retry(
        client,
        model=MODEL_EXTRACT,
        messages=[
            {"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
            {"role": "user", "content": "\n\n".join(ctx_parts)},
        ],
        response_format={"type": "json_object"},
        temperature=EXTRACT_TEMPERATURE,
        extra_body=EXTRA_BODY_NO_THINKING,
    )

    raw = completion.choices[0].message.content or "{}"
    try:
        result = PageContent.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError) as exc:
        print(colored(f"⚠️  解析失败，跳过：{exc}", "yellow"))
        state = KnowledgeState(
            knowledge=state.knowledge,
            next_page=next_state_page,
            skipped_blank=state.skipped_blank,
            skipped_model=state.skipped_model,
            skipped_parse=state.skipped_parse + [pdf_page],
        )
        save_knowledge_state(config, state)
        return state

    if result.has_content and result.knowledge:
        new_items = [
            KnowledgeItem(page=pdf_page, text=t.strip())
            for t in result.knowledge
            if t and t.strip()
        ]
        # 过滤过短噪声
        new_items = [i for i in new_items if len(i.text) >= 12]
        before = len(state.knowledge)
        knowledge = dedupe_knowledge(state.knowledge + new_items)
        added = len(knowledge) - before
        print(colored(f"✅ +{added} 条（本页候选 {len(new_items)}）", "green"))
        skipped_model = state.skipped_model
        if added == 0:
            skipped_model = state.skipped_model + [pdf_page]
    else:
        print(colored("⏭️  无有效内容", "yellow"))
        knowledge = state.knowledge
        skipped_model = state.skipped_model + [pdf_page]

    state = KnowledgeState(
        knowledge=knowledge,
        next_page=next_state_page,
        skipped_blank=state.skipped_blank,
        skipped_model=skipped_model,
        skipped_parse=state.skipped_parse,
    )
    save_knowledge_state(config, state)
    return state


def chunk_items(
    items: list[KnowledgeItem], max_chars: int
) -> list[list[KnowledgeItem]]:
    """按页序累计字符分块，尽量在页边界切开。"""
    if not items:
        return []

    # 先按页聚合，再装桶，避免同一页被拆到两块
    by_page: dict[int, list[KnowledgeItem]] = {}
    for item in items:
        by_page.setdefault(item.page, []).append(item)

    chunks: list[list[KnowledgeItem]] = []
    current: list[KnowledgeItem] = []
    size = 0
    for page in sorted(by_page.keys()):
        page_items = by_page[page]
        page_size = sum(len(i.as_line()) + 1 for i in page_items)
        if current and size + page_size > max_chars:
            chunks.append(current)
            current = []
            size = 0
        current.extend(page_items)
        size += page_size
    if current:
        chunks.append(current)
    return chunks


def chunk_page_range(chunk: list[KnowledgeItem]) -> str:
    pages = [i.page for i in chunk if i.page > 0]
    if not pages:
        return "页码未知"
    return f"第 {min(pages)}–{max(pages)} 页"


def _chat_summary(
    client: OpenAI,
    system: str,
    user: str,
    *,
    use_thinking: bool,
) -> str:
    kwargs: dict = {
        "model": MODEL_SUMMARY,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": SUMMARY_TEMPERATURE,
        "max_tokens": SUMMARY_MAX_TOKENS,
        "extra_body": (
            EXTRA_BODY_THINKING if use_thinking else EXTRA_BODY_NO_THINKING
        ),
    }
    if use_thinking:
        # DeepSeek：thinking 模式下用 reasoning_effort 控制思考强度
        kwargs["reasoning_effort"] = SUMMARY_REASONING_EFFORT

    completion = chat_create_with_retry(client, **kwargs)
    msg = completion.choices[0].message
    content = msg.content or ""
    if not content.strip():
        # 极端情况：额度被 reasoning 占满
        print(
            colored(
                "⚠️  总结 content 为空（可能 max_tokens 被 thinking 占满），"
                "请重试或提高 SUMMARY_MAX_TOKENS。",
                "yellow",
            )
        )
    return content


def generate_summary(
    client: OpenAI,
    knowledge: list[KnowledgeItem],
    *,
    gold_notes: str = "",
    toc: list[tuple[int, str, int]] | None = None,
) -> str:
    if not knowledge:
        print(colored("\n⚠️  无知识点，无法总结", "yellow"))
        return ""

    knowledge = dedupe_knowledge(knowledge)
    chunks = chunk_items(knowledge, SUMMARY_CHUNK_CHARS)
    total = len(knowledge)
    print(
        colored(
            f"\n🤔 生成总结（Pro + thinking，{total} 条 / {len(chunks)} 块）…",
            "cyan",
        )
    )

    toc_hint = ""
    if toc:
        # 精简目录树给终稿，帮助分题命名
        lines = ["【PDF 书签目录】（分题标题可对齐，勿编造目录外章节）"]
        for level, title, page in toc[:80]:
            indent = "  " * max(0, level - 1)
            lines.append(f"{indent}- {title}（第 {page} 页）")
        if len(toc) > 80:
            lines.append(f"- …共 {len(toc)} 条书签，已截断显示")
        toc_hint = "\n".join(lines) + "\n\n"

    gold_hint = ""
    if gold_notes:
        gold_hint = (
            "【人工金标准 / 修订笔记】（优先对齐其结构、术语与遗漏点；"
            "事实仍以知识点页码为准，勿照抄过时错误）\n"
            f"{gold_notes}\n\n"
        )

    if len(chunks) == 1:
        lines = [i.as_line() for i in chunks[0]]
        pages = chunk_page_range(chunks[0])
        user = (
            f"{toc_hint}{gold_hint}"
            f"全书知识点共 {total} 条，覆盖约 {pages}。\n"
            f"请直接输出完整总结（含导读、分题详述、主题索引）。\n\n"
            + "\n".join(lines)
        )
        # 终稿开 thinking
        summary = _chat_summary(
            client, FINAL_SUMMARY_PROMPT, user, use_thinking=True
        )
    else:
        partials: list[str] = []
        for i, chunk in enumerate(chunks, 1):
            pr = chunk_page_range(chunk)
            print(colored(f"   · 分块消化 {i}/{len(chunks)}（{pr}）…", "cyan"))
            lines = [x.as_line() for x in chunk]
            user = (
                f"这是全书第 {i}/{len(chunks)} 批知识点，本批约 {pr}，"
                f"共 {len(chunk)} 条。\n"
                f"请只消化本批，勿假装覆盖全书。\n\n"
                + "\n".join(lines)
            )
            # 中间稿可关 thinking 控成本；终稿再开
            partials.append(
                _chat_summary(
                    client,
                    PARTIAL_SUMMARY_PROMPT,
                    user,
                    use_thinking=False,
                )
            )

        print(colored("   · 合并为终稿（thinking）…", "cyan"))
        merged_parts = []
        for i, (chunk, partial) in enumerate(zip(chunks, partials), 1):
            pr = chunk_page_range(chunk)
            merged_parts.append(
                f"### 中间稿 {i}/{len(chunks)}（原知识点约 {pr}）\n\n{partial}"
            )
        all_pages = [i.page for i in knowledge if i.page > 0]
        span = (
            f"{min(all_pages)}–{max(all_pages)}"
            if all_pages
            else "未知"
        )
        user = (
            f"{toc_hint}{gold_hint}"
            f"下面是 {len(partials)} 段按页序的中间稿，原始知识点共 {total} 条，"
            f"页码跨度约 {span}。\n"
            f"请合并为一篇**完整终稿**：导读 + 分题详述 + 主题索引；\n"
            f"消除重复，统一术语，**前中后都要覆盖**，页码全部保留且勿编造。\n\n"
            + "\n\n---\n\n".join(merged_parts)
        )
        summary = _chat_summary(
            client, FINAL_SUMMARY_PROMPT, user, use_thinking=True
        )

    cites = len(re.findall(r"第\s*\d+\s*页", summary or ""))
    print(colored(f"✨ 总结完成（页码类引用约 {cites} 处）", "green"))
    if cites < max(10, total // 30):
        print(
            colored(
                "⚠️  页码引用仍偏少；可删 md 后重跑总结，或检查知识点是否过稀。",
                "yellow",
            )
        )
    return summary


def format_skip_meta(state: KnowledgeState) -> str:
    parts = []
    if state.skipped_blank:
        parts.append(
            f"文字过短 {len(state.skipped_blank)} 页："
            f"{_unique_sorted_pages(state.skipped_blank)}"
        )
    if state.skipped_model:
        parts.append(
            f"模型跳过 {len(state.skipped_model)} 页："
            f"{_unique_sorted_pages(state.skipped_model)}"
        )
    if state.skipped_parse:
        parts.append(
            f"解析失败 {len(state.skipped_parse)} 页："
            f"{_unique_sorted_pages(state.skipped_parse)}"
        )
    if not parts:
        return "- 跳过页：无"
    return "- 跳过页：\n" + "\n".join(f"  - {p}" for p in parts)


def save_summary(
    config: Config,
    summary: str,
    state: KnowledgeState,
) -> None:
    if not summary:
        print(colored("⏭️  无总结内容，未写入", "yellow"))
        return

    if config.summary_path.exists():
        print(
            colored(
                f"⚠️  总结文件已存在：{config.summary_path}\n"
                f"   请先删除该文件，再重新执行本程序。",
                "yellow",
            ),
            file=sys.stderr,
        )
        raise SystemExit(1)

    pages = sorted({i.page for i in state.knowledge if i.page > 0})
    page_range = f"{pages[0]}–{pages[-1]}" if pages else "无"
    cite_n = len(re.findall(r"第\s*\d+\s*页", summary))

    content = f"""# 书籍分析：{config.pdf_name}

- 生成时间：{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
- 抽取模型：{MODEL_EXTRACT}
- 总结模型：{MODEL_SUMMARY}
- 覆盖 PDF 页码：{page_range}
- 知识点条数：{len(state.knowledge)}
- 页码类引用（约）：{cite_n} 处
{format_skip_meta(state)}
- 说明：页码为 PDF 阅读器页码（从 1 起）。重写总结请先删除本文件再运行。

{summary.strip()}

---
*由 PDF 书籍分析器（DeepSeek）生成*
"""
    print(colored(f"\n📝 写入总结：{config.summary_path}", "cyan"))
    with open(config.summary_path, "w", encoding="utf-8") as f:
        f.write(content)
    print(colored("✅ 已保存", "green"))


def pages_complete(state: KnowledgeState, total_pages: int) -> bool:
    return state.next_page >= total_pages


def main(argv: list[str] | None = None) -> None:
    config = parse_args(argv)
    print(
        colored(
            f"""
📚 PDF 书籍分析器
----------------
PDF：    {config.pdf_source}
抽取：   {MODEL_EXTRACT}
总结：   {MODEL_SUMMARY}
进度：   {config.knowledge_path}
总结：   {config.summary_path}
金标准： {config.gold_path}（可选）
""",
            "cyan",
        )
    )

    try:
        setup_directories(config)
    except FileNotFoundError as exc:
        print(colored(f"⚠️  {exc}", "yellow"), file=sys.stderr)
        raise SystemExit(1) from exc

    state = load_knowledge_state(config)
    pdf_document = pymupdf.open(config.pdf_path)
    total_pages = pdf_document.page_count
    toc = load_pdf_toc(pdf_document)
    if toc:
        print(colored(f"📑 读到 PDF 书签 {len(toc)} 条", "cyan"))
    else:
        print(colored("📑 无 PDF 书签（章节定位较弱）", "yellow"))

    extract_done = pages_complete(state, total_pages)
    summary_exists = config.summary_path.exists()

    # 抽页完整 + 总结已有 → 跳过（不调 API）
    if extract_done and summary_exists:
        print(
            colored(
                f"\n✅ 已完成（抽取 {state.next_page}/{total_pages}，总结已存在）\n"
                f"   总结：{config.summary_path}\n"
                f"   重写总结 → 删除该 md 后再执行\n"
                f"   人工润色 → 可另存为 {config.gold_path.name} 供下次总结参考\n"
                f"   重抽全书 → 删除 knowledge JSON 后再执行",
                "green",
            )
        )
        pdf_document.close()
        print(colored("\n✨ 跳过，已退出 ✨", "green", attrs=["bold"]))
        return

    client = create_client()

    try:
        if not extract_done:
            start = state.next_page
            print(
                colored(
                    f"\n📚 共 {total_pages} 页，"
                    f"处理第 {start + 1}–{total_pages} 页（Flash）…",
                    "cyan",
                )
            )
            for page_num in range(start, total_pages):
                page_text = pdf_document[page_num].get_text()
                state = process_page(
                    client,
                    config,
                    page_text,
                    state,
                    page_num,
                    total_pages,
                    toc,
                )
        else:
            print(
                colored(
                    f"\n✅ 抽取已完成（{state.next_page}/{total_pages}），"
                    f"仅生成总结（Pro + thinking）…",
                    "green",
                )
            )

        if not pages_complete(state, total_pages):
            pdf_document.close()
            print(colored("⚠️  进度异常，未完成抽取", "yellow"), file=sys.stderr)
            raise SystemExit(1)

        if config.summary_path.exists():
            print(
                colored(
                    f"\n⚠️  总结文件已存在，请删除后再执行：\n"
                    f"   {config.summary_path}",
                    "yellow",
                ),
                file=sys.stderr,
            )
            pdf_document.close()
            raise SystemExit(1)

        if not state.knowledge:
            print(colored("⚠️  无知识点，无法生成总结", "yellow"), file=sys.stderr)
            pdf_document.close()
            raise SystemExit(1)

        print(colored("\n📋 跳过统计", "cyan"))
        print(format_skip_meta(state))

        gold_notes = load_gold_notes(config.gold_path)
        summary = generate_summary(
            client,
            state.knowledge,
            gold_notes=gold_notes,
            toc=toc,
        )
        save_summary(config, summary, state)

    except (APIStatusError, APIError, APIConnectionError, APITimeoutError) as exc:
        print(colored(f"\n⚠️  {format_api_error(exc)}", "yellow"), file=sys.stderr)
        print(
            colored(
                f"进度 next_page={state.next_page}，"
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
