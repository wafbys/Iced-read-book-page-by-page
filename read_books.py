"""
PDF 书籍分析器 — 逐页提取知识点，生成一篇带页码的总结。

用法：
  python read_books.py book.pdf

质量优先策略（不计成本）：
  - 逐页抽取与全书总结均用 deepseek-v4-pro
  - 抽取 / 分块消化 / 终稿 / 审校 均开启 thinking
  - 书签定位 + 邻页要点 + 下页预览 增强连贯
  - 终稿后再做一轮编辑审校
  - 产出：book_analysis/<书名>.pdf | _knowledge.json | .md
  - 可选金标准：book_analysis/<书名>_gold.md
  - 抽页完成且总结已存在 → 跳过；重写总结请先删 .md

API Key 仅从环境变量 DEEPSEEK_API_KEY 读取。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
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
# 质量优先：全程 Pro
MODEL_EXTRACT = "deepseek-v4-pro"
MODEL_SUMMARY = "deepseek-v4-pro"

MIN_PAGE_CHARS = 40
# Pro 长上下文：尽量少分块，整书一次吃进更好
SUMMARY_CHUNK_CHARS = 120_000
MAX_PAGE_TEXT_CHARS = 40_000
NEIGHBOR_CONTEXT_ITEMS = 12  # 上页要点条数
LOOKAHEAD_CHARS = 1_200  # 下页正文预览
API_MAX_RETRIES = 6
API_RETRY_BASE_SECONDS = 2.0
EXTRACT_TEMPERATURE = 0.1
SUMMARY_TEMPERATURE = 0.2
EXTRACT_MAX_TOKENS = 8_192
SUMMARY_MAX_TOKENS = 32_768
EXTRACT_REASONING_EFFORT = "high"
SUMMARY_REASONING_EFFORT = "max"
PARTIAL_REASONING_EFFORT = "high"
REVIEW_REASONING_EFFORT = "max"
GOLD_MAX_CHARS = 60_000
EXTRACT_RETRY_ON_EMPTY = True  # 正文充实却抽到 0 条时再试一次

EXTRA_BODY_THINKING = {"thinking": {"type": "enabled"}}

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
    gold_path: Path


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
    next_page: int
    skipped_blank: list[int]
    skipped_model: list[int]
    skipped_parse: list[int]


class PageContent(BaseModel):
    has_content: bool
    knowledge: list[str]


EXTRACT_SYSTEM_PROMPT = """你是顶级学术读书笔记助手。根据**当前页**正文提取可复习、可对照原文的知识点。

## 跳过（has_content=false，knowledge=[]）
仅当本页**几乎全是**下列内容时跳过：
目录/章节列表、书末索引、空白、版权页、纯出版信息、纯参考文献表、纯致谢名单。
若本页在目录之外仍有实质定义、命题、论证或例子，必须提取。

## 提取（has_content=true）
优先：定义、不变量/约束、核心命题、论证步骤、对比与权衡、方法论、关键例子、结论、对误区/反模式的批评、与前后文的逻辑推进。
忽略：无信息页眉页脚、装饰符号、无论点的纯排版。

上下文用法（若提供书签、邻页要点、下页预览）：
- 只用于消解指代、判断章节位置与句子是否跨页
- **知识点必须可由本页正文支撑**；勿把邻页/下页内容写成“本页已证明”
- 勿重复抄写邻页要点，除非本页有实质推进、限定或修正

## 写法（每条独立成句或短段）
- 自洽：单独阅读也能懂
- 具体：写清「是什么 / 为何 / 条件或代价 / 与替代方案差异」
- 保真：术语与缩写严格沿用原文（可中英并列）；重要短引文可保留并加引号
- 粒度：实质页通常 4–10 条；稀少则少提；密集可到 12 条；禁止注水空话
- 不要在条目内写页码；不要把无关要点揉成一条
- 若本页出现章节标题，可在首条用「（本章/本节：…）」点明，但仍需实质内容

只返回 JSON：
{"has_content": true或false, "knowledge": ["……", "……"]}
"""

PARTIAL_SUMMARY_PROMPT = """你在做全书总结的**中间稿**（高质量优先）。输入是带 [第 N 页] 的知识点。

写 Markdown 节选消化（不要主题索引，不要书名级大标题）：
1. 用 ## / ### 按主题或原书逻辑组织
2. **每个 bullet 末尾必须有**（第 N 页）或（第 N–M 页）；页码只能来自输入
3. 重要概念写清定义 + 属性/权衡/适用边界，禁止纯名词清单
4. 保留关键论证链条与反例；术语沿用原文
5. 本批有什么写什么，不臆造未出现章节

只输出本节选消化正文。
"""

FINAL_SUMMARY_PROMPT = """你是技术书导读作者，目标是让读者能对照 PDF 精读。根据输入写成**完整 Markdown 终稿**。

## 必须结构（顺序固定）
1. `## 导读`：2–4 句：问题意识、主线贡献、阅读地图（可含关键页码）
2. `## 分题详述`：### 分主题，名贴近原书；按逻辑递进
3. `## 主题索引`：`- 词条（English）：12, 42–43` 形式；禁止按页倾倒全部原文

## 页码（硬性）
- **分题详述每个 bullet 末尾**必须有（第 N 页）或（第 N–M 页）
- 独立定义/命题在段落中也至少标一次页码
- 禁止编造输入中未出现的页码

## 质量（硬性）
- 前、中、后部均衡；分类/调查类章节必须有「定义 + 属性/权衡」
- 区分定义 / 原则与约束 / 工程后果与反例
- 术语保真；宁可少写不可写错
- 详实、可复习，避免空泛排比

只输出 Markdown 正文。
"""

REVIEW_SYSTEM_PROMPT = """你是严格的技术编辑兼事实核对员。将「初稿总结」修订为更高质量的终稿。

你有：
1) 初稿 Markdown
2) 原始知识点列表（带页码，权威事实来源）
3) 可选：PDF 书签、人工金标准

修订要求：
- 输出**完整**修订后 Markdown（仍含 导读 / 分题详述 / 主题索引）
- 对照知识点：**补全明显遗漏的核心概念**；删除初稿中无页码支撑的断言
- **每个 bullet 必须有页码**；页码不足则从知识点补全
- 统一术语与缩写；理顺章节逻辑；压缩空话
- 若有金标准：吸收其结构与强调点，但事实以知识点为准
- 勿引入知识点中不存在的新事实

只输出修订后的 Markdown 全文。
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
            "质量优先：Pro 逐页抽取 + Pro 总结（thinking）。"
            "只需指定 PDF；产出在 book_analysis/。"
        ),
    )
    parser.add_argument("pdf", help="PDF 路径或文件名")
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
    return OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL, timeout=600.0)


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
            "  进度已保存在 knowledge JSON，充值后直接再跑即可续跑。"
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
            raise FileNotFoundError(f"找不到 PDF：{config.pdf_source}")
    elif (
        config.pdf_source.exists()
        and config.pdf_source.resolve() != config.pdf_path.resolve()
    ):
        shutil.copy2(config.pdf_source, config.pdf_path)
        print(colored(f"📄 已更新 PDF 副本 → {config.pdf_path}", "green"))


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.stem + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


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
        "meta": {
            "model_extract": MODEL_EXTRACT,
            "model_summary": MODEL_SUMMARY,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        },
    }
    atomic_write_json(config.knowledge_path, payload)


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
    if not toc:
        return ""

    active: list[tuple[int, str, int]] = []
    for level, title, page in toc:
        if page > pdf_page:
            break
        while active and active[-1][0] >= level:
            active.pop()
        active.append((level, title, page))

    nxt = None
    for level, title, page in toc:
        if page > pdf_page:
            nxt = (level, title, page)
            break

    if not active and not nxt:
        return ""

    lines = ["【目录/书签定位】（语境参考；知识点须来自本页正文）"]
    if active:
        path = " › ".join(t for _, t, _ in active)
        lines.append(f"- 当前位置：{path}（本节自第 {active[-1][2]} 页）")
    if nxt:
        lines.append(f"- 下一书签：{nxt[1]}（第 {nxt[2]} 页）")
    return "\n".join(lines)


def neighbor_context(
    state: KnowledgeState, pdf_page: int, limit: int = NEIGHBOR_CONTEXT_ITEMS
) -> str:
    if pdf_page <= 1 or not state.knowledge:
        return ""

    # 收集前两页要点
    pages_wanted = [pdf_page - 1]
    if pdf_page > 2:
        pages_wanted.insert(0, pdf_page - 2)

    blocks: list[str] = []
    for p in pages_wanted:
        items = [i for i in state.knowledge if i.page == p]
        if not items:
            continue
        tail = items[-limit:]
        lines = [f"—— 第 {p} 页要点 ——"]
        for item in tail:
            lines.append(f"- {item.text}")
        blocks.append("\n".join(lines))

    if not blocks:
        return ""

    return (
        "【邻页已提取要点】（仅供连贯与指代；勿照抄；"
        "本页无新内容则不要重复）\n" + "\n".join(blocks)
    )


def lookahead_context(pdf_document: pymupdf.Document, page_num: int) -> str:
    """下页正文开头预览，处理跨页句子。"""
    if page_num + 1 >= pdf_document.page_count:
        return ""
    nxt = clean_page_text(pdf_document[page_num + 1].get_text())
    if not nxt:
        return ""
    preview = nxt[:LOOKAHEAD_CHARS]
    if len(nxt) > LOOKAHEAD_CHARS:
        preview += "…"
    return (
        f"【下页正文预览】（第 {page_num + 2} 页开头，仅助理解跨页句；"
        f"勿把下页新论点记作本页）\n{preview}"
    )


def load_gold_notes(gold_path: Path) -> str:
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


def _extract_once(
    client: OpenAI,
    user_content: str,
    *,
    strict_retry: bool = False,
) -> PageContent:
    system = EXTRACT_SYSTEM_PROMPT
    if strict_retry:
        system += (
            "\n\n【加严重试】上一轮未抽出有效知识点，但本页正文较长。"
            "请更仔细扫描定义、命题与例子；仅当确为目录/索引/空白才 has_content=false。"
        )

    completion = chat_create_with_retry(
        client,
        model=MODEL_EXTRACT,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        response_format={"type": "json_object"},
        temperature=EXTRACT_TEMPERATURE,
        max_tokens=EXTRACT_MAX_TOKENS,
        reasoning_effort=EXTRACT_REASONING_EFFORT,
        extra_body=EXTRA_BODY_THINKING,
    )
    raw = completion.choices[0].message.content or "{}"
    return PageContent.model_validate(json.loads(raw))


def process_page(
    client: OpenAI,
    config: Config,
    page_text: str,
    state: KnowledgeState,
    page_num: int,
    total_pages: int,
    toc: list[tuple[int, str, int]],
    pdf_document: pymupdf.Document,
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

    ctx_parts = [f"全书共 {total_pages} 页；当前为第 {pdf_page} 页。"]
    outline = outline_context_for_page(toc, pdf_page)
    if outline:
        ctx_parts.append(outline)
    neighbor = neighbor_context(state, pdf_page)
    if neighbor:
        ctx_parts.append(neighbor)
    look = lookahead_context(pdf_document, page_num)
    if look:
        ctx_parts.append(look)
    ctx_parts.append(f"页面正文：\n{cleaned}")
    user_content = "\n\n".join(ctx_parts)

    try:
        result = _extract_once(client, user_content)
        # 正文充实却空结果 → 加严重试一次
        if (
            EXTRACT_RETRY_ON_EMPTY
            and (not result.has_content or not result.knowledge)
            and len(cleaned) >= 200
        ):
            print(colored("🔁 空抽取，加严重试本页…", "yellow"))
            result = _extract_once(client, user_content, strict_retry=True)
    except (json.JSONDecodeError, ValidationError) as exc:
        print(colored(f"⚠️  解析失败，跳过：{exc}", "yellow"))
        # 解析失败也重试一次
        try:
            print(colored("🔁 解析失败，重试本页…", "yellow"))
            result = _extract_once(client, user_content, strict_retry=True)
        except (json.JSONDecodeError, ValidationError) as exc2:
            print(colored(f"⚠️  重试仍失败：{exc2}", "yellow"))
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
            if t and t.strip() and len(t.strip()) >= 12
        ]
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
    if not items:
        return []

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


def _chat_pro(
    client: OpenAI,
    system: str,
    user: str,
    *,
    reasoning_effort: str,
    temperature: float = SUMMARY_TEMPERATURE,
    max_tokens: int = SUMMARY_MAX_TOKENS,
) -> str:
    completion = chat_create_with_retry(
        client,
        model=MODEL_SUMMARY,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
        reasoning_effort=reasoning_effort,
        extra_body=EXTRA_BODY_THINKING,
    )
    content = completion.choices[0].message.content or ""
    if not content.strip():
        print(
            colored(
                "⚠️  模型 content 为空（thinking 可能占满 max_tokens），重试一次…",
                "yellow",
            )
        )
        completion = chat_create_with_retry(
            client,
            model=MODEL_SUMMARY,
            messages=[
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": user
                    + "\n\n请直接给出完整最终答案正文，确保 content 非空。",
                },
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            reasoning_effort="high",
            extra_body=EXTRA_BODY_THINKING,
        )
        content = completion.choices[0].message.content or ""
    return content


def format_toc_hint(toc: list[tuple[int, str, int]] | None) -> str:
    if not toc:
        return ""
    lines = ["【PDF 书签目录】（分题可对齐；勿编造目录外章节）"]
    for level, title, page in toc:
        indent = "  " * max(0, level - 1)
        lines.append(f"{indent}- {title}（第 {page} 页）")
    return "\n".join(lines) + "\n\n"


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
            f"\n🤔 生成总结（Pro+thinking，{total} 条 / {len(chunks)} 块）…",
            "cyan",
        )
    )

    toc_hint = format_toc_hint(toc)
    gold_hint = ""
    if gold_notes:
        gold_hint = (
            "【人工金标准】（对齐结构与强调；事实以知识点页码为准）\n"
            f"{gold_notes}\n\n"
        )

    if len(chunks) == 1:
        lines = [i.as_line() for i in chunks[0]]
        pages = chunk_page_range(chunks[0])
        user = (
            f"{toc_hint}{gold_hint}"
            f"全书知识点共 {total} 条，覆盖约 {pages}。\n"
            f"请输出完整总结（导读、分题详述、主题索引）。\n\n"
            + "\n".join(lines)
        )
        draft = _chat_pro(
            client,
            FINAL_SUMMARY_PROMPT,
            user,
            reasoning_effort=SUMMARY_REASONING_EFFORT,
        )
    else:
        partials: list[str] = []
        for i, chunk in enumerate(chunks, 1):
            pr = chunk_page_range(chunk)
            print(
                colored(
                    f"   · 分块消化 {i}/{len(chunks)}（{pr}，thinking）…",
                    "cyan",
                )
            )
            lines = [x.as_line() for x in chunk]
            user = (
                f"全书第 {i}/{len(chunks)} 批，本批约 {pr}，共 {len(chunk)} 条。\n"
                f"只消化本批。\n\n" + "\n".join(lines)
            )
            partials.append(
                _chat_pro(
                    client,
                    PARTIAL_SUMMARY_PROMPT,
                    user,
                    reasoning_effort=PARTIAL_REASONING_EFFORT,
                )
            )

        print(colored("   · 合并终稿（thinking max）…", "cyan"))
        merged_parts = []
        for i, (chunk, partial) in enumerate(zip(chunks, partials), 1):
            pr = chunk_page_range(chunk)
            merged_parts.append(
                f"### 中间稿 {i}/{len(chunks)}（约 {pr}）\n\n{partial}"
            )
        all_pages = [i.page for i in knowledge if i.page > 0]
        span = (
            f"{min(all_pages)}–{max(all_pages)}" if all_pages else "未知"
        )
        user = (
            f"{toc_hint}{gold_hint}"
            f"{len(partials)} 段中间稿；原始 {total} 条；页码跨度约 {span}。\n"
            f"合并为完整终稿：导读 + 分题详述 + 主题索引；"
            f"去重、统一术语、前中后覆盖、页码勿编造。\n\n"
            + "\n\n---\n\n".join(merged_parts)
        )
        draft = _chat_pro(
            client,
            FINAL_SUMMARY_PROMPT,
            user,
            reasoning_effort=SUMMARY_REASONING_EFFORT,
        )

    # 第二轮审校（质量优先）
    print(colored("   · 编辑审校（thinking max）…", "cyan"))
    # 审校时附带压缩知识点清单（按页抽样+全量若不太长）
    kb_lines = [i.as_line() for i in knowledge]
    kb_blob = "\n".join(kb_lines)
    if len(kb_blob) > 100_000:
        # 过长则按页取每页前 2 条 + 全部页码列表
        by_page: dict[int, list[str]] = {}
        for item in knowledge:
            by_page.setdefault(item.page, []).append(item.text)
        sample = []
        for p in sorted(by_page):
            for t in by_page[p][:2]:
                sample.append(f"[第 {p} 页] {t}")
        kb_blob = (
            "（知识点过长，以下为每页最多 2 条抽样；页码集合："
            f"{sorted(by_page.keys())}）\n" + "\n".join(sample)
        )

    review_user = (
        f"{toc_hint}{gold_hint}"
        f"## 初稿\n\n{draft}\n\n"
        f"## 原始知识点\n\n{kb_blob}\n"
    )
    summary = _chat_pro(
        client,
        REVIEW_SYSTEM_PROMPT,
        review_user,
        reasoning_effort=REVIEW_REASONING_EFFORT,
    )
    if not summary.strip():
        print(colored("⚠️  审校输出为空，回退使用初稿", "yellow"))
        summary = draft

    cites = len(re.findall(r"第\s*\d+\s*页", summary or ""))
    print(colored(f"✨ 总结完成（页码类引用约 {cites} 处）", "green"))
    if cites < max(12, total // 25):
        print(
            colored(
                "⚠️  页码引用仍可能偏少；可删 md 后重跑或补充 gold。",
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
- 抽取模型：{MODEL_EXTRACT}（thinking）
- 总结模型：{MODEL_SUMMARY}（thinking + 审校）
- 覆盖 PDF 页码：{page_range}
- 知识点条数：{len(state.knowledge)}
- 页码类引用（约）：{cite_n} 处
{format_skip_meta(state)}
- 说明：页码为 PDF 阅读器页码（从 1 起）。重写总结请先删除本文件再运行。
- 可选金标准：同目录 `{config.gold_path.name}`

{summary.strip()}

---
*由 PDF 书籍分析器（DeepSeek · 质量优先）生成*
"""
    print(colored(f"\n📝 写入总结：{config.summary_path}", "cyan"))
    # 原子写 md
    fd, tmp_name = tempfile.mkstemp(
        prefix=config.summary_path.stem + ".",
        suffix=".tmp",
        dir=str(config.summary_path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, config.summary_path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    print(colored("✅ 已保存", "green"))


def pages_complete(state: KnowledgeState, total_pages: int) -> bool:
    return state.next_page >= total_pages


def main(argv: list[str] | None = None) -> None:
    config = parse_args(argv)
    print(
        colored(
            f"""
📚 PDF 书籍分析器（质量优先）
----------------------------
PDF：    {config.pdf_source}
抽取：   {MODEL_EXTRACT} + thinking
总结：   {MODEL_SUMMARY} + thinking + 审校
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
        print(colored("📑 无 PDF 书签（依赖邻页/下页上下文）", "yellow"))

    extract_done = pages_complete(state, total_pages)
    summary_exists = config.summary_path.exists()

    if extract_done and summary_exists:
        print(
            colored(
                f"\n✅ 已完成（抽取 {state.next_page}/{total_pages}，总结已存在）\n"
                f"   总结：{config.summary_path}\n"
                f"   重写总结 → 删除该 md 后再执行\n"
                f"   人工润色 → 另存为 {config.gold_path.name} 供下次总结参考\n"
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
                    f"处理第 {start + 1}–{total_pages} 页（Pro+thinking）…",
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
                    pdf_document,
                )
        else:
            print(
                colored(
                    f"\n✅ 抽取已完成（{state.next_page}/{total_pages}），"
                    f"仅生成总结…",
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
