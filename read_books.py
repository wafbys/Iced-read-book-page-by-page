"""
PDF 书籍分析器 — 逐页提取知识点，生成一篇带页码的总结。

用法：
  python read_books.py book.pdf
  python read_books.py book.pdf --profile auto|economy|balanced|quality
  python read_books.py book.pdf --out-dir ./out
  python read_books.py book.pdf -y          # 跳过 auto 交互确认

  auto     — 预读评估 → 展示结果 → 确认或改选档位（默认）
  economy  — 固定省钱：全 Flash，无审校，总结关 thinking
  balanced — 固定平衡：Flash 抽 + Pro 结/审 high
  quality  — 固定最强：Pro 抽+thinking；结/审 max

未传 --profile 时可读环境变量 READ_BOOKS_PROFILE。
auto 决议写入 knowledge.json 的 meta（strategy_spec 等）；再次运行复用，删 knowledge 可重新预读/选档。
也可选加载项目根目录 .env（不覆盖已有环境变量）。
产出：<out-dir>/<书名>.pdf | _knowledge.json | .md
Ctrl+C 可中断并保留进度。API Key：DEEPSEEK_API_KEY。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, fields, replace
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
from pydantic import BaseModel, Field, ValidationError
from termcolor import colored

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
MODEL_FLASH = "deepseek-v4-flash"
MODEL_PRO = "deepseek-v4-pro"

MIN_PAGE_CHARS = 40
MAX_PAGE_TEXT_CHARS = 24_000
NEIGHBOR_CONTEXT_ITEMS = 10
LOOKAHEAD_CHARS = 1_000
API_MAX_RETRIES = 6
API_RETRY_BASE_SECONDS = 2.0
EXTRACT_TEMPERATURE = 0.15
SUMMARY_TEMPERATURE = 0.2
EXTRACT_MAX_TOKENS = 4_096
# thinking 时 reasoning 与 content 共用预算，需更大 completion 上限
EXTRACT_MAX_TOKENS_THINKING = 16_384
SUMMARY_MAX_TOKENS = 32_768
# 总结/审校 content 为空时抬高 completion 预算再试
SUMMARY_MAX_TOKENS_BUMP = 48_000
GOLD_MAX_CHARS = 40_000
EXTRACT_RETRY_ON_EMPTY = True
# 审校后页码引用过稀时，可自动加一轮 max 审校
CITE_ESCALATE_MIN = 12
# 匹配（第 N 页）与（第 N–M 页）等范围形式（– - − — ~ 〜 至 到）
PAGE_CITE_RE = re.compile(
    r"第\s*\d+\s*(?:[–\-−—~〜至到]\s*\d+\s*)?页"
)
# auto 预读：采样页数（实质性正文页）
PREFLIGHT_SAMPLE_TARGET = 5
PREFLIGHT_CHARS_PER_PAGE = 2_500
DEFAULT_OUT_DIR = "book_analysis"
HASH_CHUNK_SIZE = 1024 * 1024
PREFLIGHT_DECISION_VERSION = 1

EXTRA_BODY_NO_THINKING = {"thinking": {"type": "disabled"}}
EXTRA_BODY_THINKING = {"thinking": {"type": "enabled"}}

PROFILE_ENV = "READ_BOOKS_PROFILE"
VALID_PROFILES = ("auto", "economy", "balanced", "quality")
# 交互确认时可改选的固定档（不含 auto 本身；Enter 表示采用 auto 映射）
CONFIRM_PROFILE_CHOICES = ("economy", "balanced", "quality")


@dataclass(frozen=True)
class PipelineStrategy:
    """流水线策略：档位 / 预读评估 + 运行时微调。"""

    name: str
    extract_model: str
    summary_model: str
    extract_thinking: bool
    extract_effort: str  # 仅 extract_thinking 时有意义
    partial_effort: str
    final_effort: str
    review_effort: str
    do_review: bool
    auto_escalate_review: bool  # 页码稀 → 再 max 审一轮
    summary_chunk_chars: int
    description: str
    summary_thinking: bool = True  # economy 可关，省钱

    def label(self) -> str:
        ext_t = "+thinking" if self.extract_thinking else ""
        sum_t = "" if self.summary_thinking else " no-think"
        rev = f"审校:{self.review_effort}" if self.do_review else "无审校"
        return (
            f"{self.name} | 抽:{self.extract_model}{ext_t} | "
            f"结:{self.summary_model}({self.final_effort}{sum_t}) | {rev}"
        )

    def with_updates(self, **kwargs) -> PipelineStrategy:
        return replace(self, **kwargs)

    def to_spec(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_spec(data: dict) -> PipelineStrategy | None:
        """从 knowledge meta 恢复策略；字段不全或非法则返回 None。"""
        if not isinstance(data, dict):
            return None
        try:
            kwargs = {}
            for f in fields(PipelineStrategy):
                if f.name not in data:
                    if f.name == "summary_thinking":
                        kwargs[f.name] = True  # 旧进度兼容
                        continue
                    return None
                kwargs[f.name] = data[f.name]
            s = PipelineStrategy(**kwargs)
            if s.final_effort not in ("high", "max"):
                return None
            if s.review_effort not in ("high", "max"):
                return None
            return s
        except (TypeError, ValueError, KeyError):
            return None


class PreflightAssessment(BaseModel):
    """预读评估结果（由 Flash 给出，再映射为策略）。"""

    difficulty: int  # 1–5
    text_noise: int  # 1–5 排版/OCR/译文噪声
    term_density: int  # 1–5 术语密度
    structure_complexity: int  # 1–5 论证/结构复杂度
    need_pro_extract: bool
    need_extract_thinking: bool
    summary_effort: str  # high | max
    review_effort: str  # high | max
    do_review: bool
    rationale: str  # 简短中文理由


PREFLIGHT_SYSTEM = """你是文档难度评估器。根据抽样页正文，判断后续流水线该用多强的模型。

只返回 JSON（字段必须齐全）：
{
  "difficulty": 1到5的整数,
  "text_noise": 1到5,
  "term_density": 1到5,
  "structure_complexity": 1到5,
  "need_pro_extract": true/false,
  "need_extract_thinking": true/false,
  "summary_effort": "high" 或 "max",
  "review_effort": "high" 或 "max",
  "do_review": true/false,
  "rationale": "一两句中文理由"
}

判断参考：
- difficulty 1–2：教材式清晰、术语少 → 抽页 Flash 即可；总结 high；审校可关或 high
- difficulty 3：一般学术/技术书 → 抽 Flash；总结 Pro high；要审校
- difficulty 4：高密度术语、译著别扭、跨页论证多 → 可考虑 Pro 抽页；总结 max；要审校
- difficulty 5：极难/高噪声/强形式化 → Pro 抽+thinking；总结与审校 max
- need_pro_extract：仅当 Flash 明显可能漏定义/搞砸术语时为 true（易书请 false）
- need_extract_thinking：仅 difficulty 5 且值得每页多花钱时为 true（通常 false；
  代码侧仅在 difficulty≥5 时才会开启抽页 thinking）
- text_noise / term_density 高：应提高 summary/review effort，不等于一定要 Pro 抽
- 不要因为「看起来重要」就一律 max；按样本难度诚实评估

说明：下游还有确定性映射与硬闸（会覆盖不安全的 true），请仍按样本诚实打分。
"""


def _base_profiles() -> dict[str, PipelineStrategy]:
    return {
        "economy": PipelineStrategy(
            name="economy",
            extract_model=MODEL_FLASH,
            summary_model=MODEL_FLASH,
            extract_thinking=False,
            extract_effort="high",
            partial_effort="high",
            final_effort="high",
            review_effort="high",
            do_review=False,
            auto_escalate_review=False,
            summary_chunk_chars=80_000,
            description="省钱：全 Flash，无审校，总结关闭 thinking",
            summary_thinking=False,
        ),
        "balanced": PipelineStrategy(
            name="balanced",
            extract_model=MODEL_FLASH,
            summary_model=MODEL_PRO,
            extract_thinking=False,
            extract_effort="high",
            partial_effort="high",
            final_effort="high",
            review_effort="high",
            do_review=True,
            auto_escalate_review=True,
            summary_chunk_chars=100_000,
            description="固定平衡：Flash 抽 + Pro 结/审 high",
            summary_thinking=True,
        ),
        "quality": PipelineStrategy(
            name="quality",
            extract_model=MODEL_PRO,
            summary_model=MODEL_PRO,
            extract_thinking=True,
            extract_effort="high",
            partial_effort="high",
            final_effort="max",
            review_effort="max",
            do_review=True,
            auto_escalate_review=True,
            summary_chunk_chars=140_000,
            description="固定最强：Pro 抽+thinking；结/审 max",
            summary_thinking=True,
        ),
        # auto 占位；真正参数由预读评估填充
        "auto": PipelineStrategy(
            name="auto",
            extract_model=MODEL_FLASH,
            summary_model=MODEL_PRO,
            extract_thinking=False,
            extract_effort="high",
            partial_effort="high",
            final_effort="high",
            review_effort="high",
            do_review=True,
            auto_escalate_review=True,
            summary_chunk_chars=100_000,
            description="预读评估后动态决定 Flash/Pro 与 high/max",
            summary_thinking=True,
        ),
    }


def _tune_chunk_size(
    base_chunk: int,
    *,
    total_pages: int | None,
    knowledge_chars: int | None,
) -> int:
    chunk = base_chunk
    if total_pages is not None:
        if total_pages <= 40:
            chunk = max(chunk, 150_000)
        elif total_pages >= 250:
            chunk = min(chunk, 70_000)
    if knowledge_chars is not None:
        if knowledge_chars < 40_000:
            chunk = max(chunk, knowledge_chars + 10_000)
        elif knowledge_chars > 200_000:
            chunk = min(chunk, 90_000)
    return chunk


def _norm_effort(value: str | None) -> str:
    v = (value or "").strip().lower()
    return v if v in ("high", "max") else "high"


def _bump_effort(current: str, minimum: str) -> str:
    """effort 只升不降：high < max。"""
    order = {"high": 0, "max": 1}
    return current if order.get(current, 0) >= order.get(minimum, 0) else minimum


def strategy_from_assessment(
    assessment: PreflightAssessment,
    *,
    total_pages: int | None = None,
) -> tuple[PipelineStrategy, list[str]]:
    """
    把预读评分映射为流水线参数（确定性规则 + 成本硬闸）。

    返回 (strategy, overrides)：overrides 为人读说明，列出相对评估的强制调整。
    """
    overrides: list[str] = []
    d = assessment.difficulty
    noise = assessment.text_noise
    terms = assessment.term_density
    struct = assessment.structure_complexity

    # —— 抽页模型 ——
    # 基准：评估 need_pro 或 difficulty≥4
    want_pro = bool(assessment.need_pro_extract) or d >= 4
    if d <= 2 and assessment.need_pro_extract:
        want_pro = False
        overrides.append(
            "need_pro_extract=true 但 difficulty≤2 → 强制 Flash 抽页（成本硬闸）"
        )
    # 术语极密且已达中等难度：即使模型未要 Pro，也升抽页
    if not want_pro and d >= 3 and terms >= 5:
        want_pro = True
        overrides.append(
            "term_density≥5 且 difficulty≥3 → 升级 Pro 抽页"
        )
    extract_model = MODEL_PRO if want_pro else MODEL_FLASH

    # 抽页 thinking：仅 difficulty≥5 且 Pro 才允许（忽略过宽的 true）
    want_think = bool(assessment.need_extract_thinking)
    if want_think and extract_model != MODEL_PRO:
        want_think = False
        overrides.append(
            "need_extract_thinking=true 但抽页非 Pro → 忽略 thinking"
        )
    if want_think and d < 5:
        want_think = False
        overrides.append(
            f"need_extract_thinking=true 但 difficulty={d}<5 → 关闭抽页 thinking（成本硬闸）"
        )
    # 极难且模型未要 thinking：不自动强开（避免默认账单爆炸）；quality 档另说
    extract_thinking = want_think and extract_model == MODEL_PRO and d >= 5

    # —— 总结侧 ——
    easy = d <= 1 and noise <= 2
    if easy:
        summary_model = MODEL_FLASH
        do_review = False
        auto_esc = False
        summary_thinking = False
    else:
        summary_model = MODEL_PRO
        do_review = bool(assessment.do_review) if d >= 2 else False
        auto_esc = d >= 3
        summary_thinking = True

    final_effort = _norm_effort(assessment.summary_effort)
    review_effort = _norm_effort(assessment.review_effort)

    # 噪声：提高 effort / 强制审校（与预读 prompt 对齐）
    if noise >= 4:
        if not do_review:
            do_review = True
            overrides.append("text_noise≥4 → 强制开启审校")
        if noise >= 5:
            prev_f, prev_r = final_effort, review_effort
            final_effort = _bump_effort(final_effort, "max")
            review_effort = _bump_effort(review_effort, "max")
            if (prev_f, prev_r) != (final_effort, review_effort):
                overrides.append(
                    "text_noise≥5 → summary/review effort 升至 max"
                )
        else:
            # noise=4：至少 high（已是），保持；若评估给了莫名其妙的值已 norm
            pass

    # 术语密度：难一点的书抬总结强度
    if terms >= 4 and d >= 3:
        prev = final_effort
        final_effort = _bump_effort(final_effort, "max")
        if final_effort != prev:
            overrides.append(
                "term_density≥4 且 difficulty≥3 → summary effort max"
            )

    # difficulty 阶梯强制
    if d >= 5:
        if final_effort != "max" or review_effort != "max" or not do_review:
            overrides.append(
                "difficulty≥5 → summary/review max 且强制审校"
            )
        final_effort = "max"
        review_effort = "max"
        do_review = True
        auto_esc = True
    elif d >= 3:
        if not do_review:
            do_review = True
            overrides.append("difficulty≥3 → 强制开启审校")
        elif not assessment.do_review:
            overrides.append(
                "评估 do_review=false 但 difficulty≥3 → 强制开启审校"
            )

    # 结构复杂 → 略小分块
    chunk = 100_000
    if struct >= 4:
        chunk = 90_000
    if total_pages and total_pages <= 40:
        chunk = max(chunk, 140_000)
    chunk = _tune_chunk_size(chunk, total_pages=total_pages, knowledge_chars=None)

    rationale = (assessment.rationale or "").strip()
    desc = (
        f"预读评估 difficulty={d} "
        f"noise={noise} terms={terms} "
        f"struct={struct}"
    )
    if rationale:
        desc += f"；{rationale}"
    if overrides:
        desc += f"；映射调整×{len(overrides)}"

    strategy = PipelineStrategy(
        name="auto",
        extract_model=extract_model,
        summary_model=summary_model,
        extract_thinking=extract_thinking,
        extract_effort="high",
        partial_effort="high",
        final_effort=final_effort,
        review_effort=review_effort,
        do_review=do_review,
        auto_escalate_review=auto_esc,
        summary_chunk_chars=chunk,
        description=desc,
        summary_thinking=summary_thinking,
    )
    return strategy, overrides


def log_assessment_mapping(
    assessment: PreflightAssessment,
    strategy: PipelineStrategy,
    overrides: list[str],
) -> None:
    """打印评估原值与映射后生效策略，便于对照成本。"""
    print(
        colored(
            f"   · 评估原值：diff={assessment.difficulty} "
            f"noise={assessment.text_noise} terms={assessment.term_density} "
            f"struct={assessment.structure_complexity} | "
            f"need_pro={assessment.need_pro_extract} "
            f"need_think={assessment.need_extract_thinking} "
            f"sum={assessment.summary_effort} rev={assessment.review_effort} "
            f"do_review={assessment.do_review}",
            "cyan",
        )
    )
    if overrides:
        for line in overrides:
            print(colored(f"   · 映射调整：{line}", "yellow"))
    else:
        print(colored("   · 映射调整：无（评估与规则一致）", "cyan"))
    print(
        colored(
            f"   · 生效策略：{strategy.label()}",
            "cyan",
        )
    )


def print_auto_analysis_report(
    *,
    total_pages: int,
    sample_pages: list[int],
    assessment: PreflightAssessment,
    proposed: PipelineStrategy,
    overrides: list[str],
    has_toc: bool,
) -> None:
    """向用户展示 auto 预读分析结论（确认前）。"""
    print(colored("\n" + "═" * 56, "cyan"))
    print(colored("📊 auto 预读分析报告", "cyan", attrs=["bold"]))
    print(colored("═" * 56, "cyan"))
    print(colored(f"  全书页数：{total_pages}　书签：{'有' if has_toc else '无'}", "cyan"))
    pages_s = ", ".join(str(p) for p in sample_pages) if sample_pages else "（无实质正文样本）"
    print(colored(f"  抽样页码：{pages_s}", "cyan"))
    print(
        colored(
            f"  难度={assessment.difficulty}/5  "
            f"噪声={assessment.text_noise}/5  "
            f"术语={assessment.term_density}/5  "
            f"结构={assessment.structure_complexity}/5",
            "cyan",
        )
    )
    print(
        colored(
            f"  模型建议：Pro抽={assessment.need_pro_extract}  "
            f"抽thinking={assessment.need_extract_thinking}  "
            f"总结={assessment.summary_effort}  "
            f"审校={assessment.review_effort}  "
            f"做审校={assessment.do_review}",
            "cyan",
        )
    )
    if (assessment.rationale or "").strip():
        print(colored(f"  理由：{assessment.rationale.strip()}", "cyan"))
    if overrides:
        print(colored("  映射硬闸/调整：", "yellow"))
        for line in overrides:
            print(colored(f"    · {line}", "yellow"))
    else:
        print(colored("  映射硬闸/调整：无", "cyan"))
    print(colored(f"  建议策略：{proposed.label()}", "green", attrs=["bold"]))
    print(colored(f"  说明：{proposed.description}", "green"))
    print(colored("═" * 56, "cyan"))


def parse_confirm_choice(raw: str) -> str | None:
    """
    解析用户确认输入。
    返回：'auto' | 'economy' | 'balanced' | 'quality' | 'quit'；无效则 None。
    """
    s = (raw or "").strip().lower()
    if s in ("", "a", "auto", "y", "yes", "是", "确认"):
        return "auto"
    if s in ("1", "e", "economy", "省钱"):
        return "economy"
    if s in ("2", "b", "balanced", "平衡"):
        return "balanced"
    if s in ("3", "quality", "最强"):
        return "quality"
    if s in ("0", "q", "quit", "exit", "n", "no", "取消"):
        return "quit"
    if s in CONFIRM_PROFILE_CHOICES:
        return s
    return None


def confirm_auto_strategy_interactive(
    *,
    proposed: PipelineStrategy,
    yes: bool,
) -> str:
    """
    让用户确认 auto 建议或改选固定档。
    返回 chosen_profile：'auto' | 'economy' | 'balanced' | 'quality'。
    用户退出时 SystemExit(0)。
    """
    if yes:
        print(
            colored(
                "✓ 已指定 --yes：采用 auto 映射策略，跳过确认。",
                "green",
            )
        )
        return "auto"

    if not sys.stdin.isatty():
        print(
            colored(
                "✓ 非交互终端：采用 auto 映射策略（可用 -y 明示；"
                "要改档请用 --profile）。",
                "yellow",
            ),
            file=sys.stderr,
        )
        return "auto"

    print(
        colored(
            "\n请确认策略（写入 knowledge.json，再次运行不再询问；\n"
            "重新预读/改选：删除 knowledge JSON 后重跑）：\n"
            "  [Enter]  采用上方 auto 建议\n"
            "  [1]      economy  省钱（全 Flash，无审校）\n"
            "  [2]      balanced 平衡（Flash 抽 + Pro 结/审）\n"
            "  [3]      quality  最强（Pro 抽+thinking）\n"
            "  [0]      退出，不开始处理",
            "cyan",
        )
    )
    while True:
        try:
            raw = input(colored("你的选择 > ", "cyan"))
        except EOFError:
            print(
                colored("未读到输入，采用 auto 映射策略。", "yellow"),
                file=sys.stderr,
            )
            return "auto"
        choice = parse_confirm_choice(raw)
        if choice == "quit":
            print(colored("已取消。", "yellow"))
            raise SystemExit(0)
        if choice in ("auto",) + CONFIRM_PROFILE_CHOICES:
            if choice == "auto":
                print(colored(f"✓ 采用 auto 建议：{proposed.label()}", "green"))
            else:
                print(colored(f"✓ 改选固定档：{choice}", "green"))
            return choice
        print(
            colored(
                "无效输入。请 Enter / 1 / 2 / 3 / 0。",
                "yellow",
            ),
            file=sys.stderr,
        )


def try_import_legacy_preflight_file(
    config: Config,
    *,
    pdf_sha256: str,
    total_pages: int,
) -> tuple[PipelineStrategy, dict] | None:
    """
    兼容旧版独立的 <stem>_preflight.json：若 knowledge 尚无策略则读入一次。
    之后策略只写在 knowledge.meta 里，不再维护该文件。
    """
    path = config.out_dir / f"{Path(config.pdf_name).stem}_preflight.json"
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    stored_sha = data.get("pdf_sha256")
    if stored_sha and stored_sha != pdf_sha256:
        return None
    stored_pages = data.get("pdf_page_count")
    if stored_pages is not None:
        try:
            if int(stored_pages) != total_pages:
                return None
        except (TypeError, ValueError):
            return None
    strategy = PipelineStrategy.from_spec(data.get("strategy_spec") or {})
    if strategy is None:
        return None
    print(
        colored(
            f"♻️  从旧版采样文件迁入策略：{path.name}（可删除该文件）\n"
            f"   策略：{strategy.label()}",
            "cyan",
        )
    )
    return strategy, data


def resolve_strategy(
    profile_name: str | None = None,
    *,
    total_pages: int | None = None,
    knowledge_chars: int | None = None,
    prebuilt: PipelineStrategy | None = None,
) -> PipelineStrategy:
    """
    解析策略：
    - 固定档 economy/balanced/quality
    - auto 须先预读得到 prebuilt，否则暂回 balanced 骨架再评估
    - 再按页数/知识量微调分块
    """
    profiles = _base_profiles()
    raw = (profile_name or os.environ.get(PROFILE_ENV) or "auto").strip().lower()
    if raw not in VALID_PROFILES:
        print(
            colored(
                f"⚠️  未知 {PROFILE_ENV}={raw!r}，回退 auto。"
                f"可选：{', '.join(VALID_PROFILES)}",
                "yellow",
            ),
            file=sys.stderr,
        )
        raw = "auto"

    if prebuilt is not None and raw == "auto":
        base = prebuilt
    elif raw == "auto":
        base = profiles["balanced"].with_updates(
            name="auto",
            description="auto（待预读评估）",
        )
    else:
        base = profiles[raw]

    chunk = _tune_chunk_size(
        base.summary_chunk_chars,
        total_pages=total_pages,
        knowledge_chars=knowledge_chars,
    )
    if chunk != base.summary_chunk_chars:
        return base.with_updates(
            summary_chunk_chars=chunk,
            description=base.description + f"；分块≈{chunk // 1000}k字(自动)",
        )
    return base


def pick_preflight_pages(total_pages: int, target: int = PREFLIGHT_SAMPLE_TARGET) -> list[int]:
    """选 0-based 页码：靠前、中间、靠后各取，避免只读封面。"""
    if total_pages <= 0:
        return []
    if total_pages <= target:
        return list(range(total_pages))

    # 相对位置：跳过可能的封面，取 5%、20%、40%、65%、85%
    fracs = [0.05, 0.2, 0.4, 0.65, 0.85]
    pages = sorted({min(total_pages - 1, max(0, int(total_pages * f))) for f in fracs})
    # 不足则补均匀页
    i = 0
    while len(pages) < target and i < total_pages:
        if i not in pages:
            pages.append(i)
        i += 1
        pages = sorted(set(pages))
    return pages[:target]


def collect_preflight_samples(
    pdf_document: pymupdf.Document,
    page_indices: list[int],
) -> list[tuple[int, str]]:
    """收集有实质文字的抽样页 (1-based_page, text)。"""
    samples: list[tuple[int, str]] = []
    tried = list(page_indices)
    # 若抽样偏空，向后扫一些页补足
    extra = 0
    idx = 0
    total = pdf_document.page_count
    while len(samples) < PREFLIGHT_SAMPLE_TARGET and (
        idx < len(tried) or extra < total
    ):
        if idx < len(tried):
            p0 = tried[idx]
            idx += 1
        else:
            p0 = extra
            extra += 1
            if p0 in tried:
                continue
        text = clean_page_text(pdf_document[p0].get_text())
        if len(text) < MIN_PAGE_CHARS:
            continue
        samples.append((p0 + 1, text[:PREFLIGHT_CHARS_PER_PAGE]))
    return samples


def run_preflight_assessment(
    client: OpenAI,
    samples: list[tuple[int, str]],
    *,
    total_pages: int,
    has_toc: bool,
) -> PreflightAssessment:
    """用 Flash 预读评估，决定 Flash/Pro 与 high/max。"""
    if not samples:
        # 无样本：保守 balanced
        return PreflightAssessment(
            difficulty=3,
            text_noise=3,
            term_density=3,
            structure_complexity=3,
            need_pro_extract=False,
            need_extract_thinking=False,
            summary_effort="high",
            review_effort="high",
            do_review=True,
            rationale="抽样无正文，回退中等难度假设",
        )

    blocks = []
    for page, text in samples:
        blocks.append(f"### 第 {page} 页样本\n{text}")
    user = (
        f"全书约 {total_pages} 页；PDF 书签：{'有' if has_toc else '无'}。\n"
        f"以下为 {len(samples)} 个抽样页（可能截断）。请评估难度并给出流水线建议。\n\n"
        + "\n\n".join(blocks)
    )

    print(
        colored(
            f"🔍 预读评估：{len(samples)} 页样本 → 决定 Flash/Pro 与 high/max …",
            "cyan",
        )
    )
    completion = chat_create_with_retry(
        client,
        model=MODEL_FLASH,
        messages=[
            {"role": "system", "content": PREFLIGHT_SYSTEM},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        temperature=0.1,
        max_tokens=1_024,
        extra_body=EXTRA_BODY_NO_THINKING,
    )
    raw = completion.choices[0].message.content or "{}"
    try:
        data = json.loads(raw)
        # 规范化 effort 字段
        for key in ("summary_effort", "review_effort"):
            if str(data.get(key, "")).lower() not in ("high", "max"):
                data[key] = "high"
        for key in (
            "difficulty",
            "text_noise",
            "term_density",
            "structure_complexity",
        ):
            try:
                data[key] = max(1, min(5, int(data.get(key, 3))))
            except (TypeError, ValueError):
                data[key] = 3
        assessment = PreflightAssessment.model_validate(data)
    except (json.JSONDecodeError, ValidationError) as exc:
        print(colored(f"⚠️  预读评估解析失败（{exc}），回退 balanced 参数", "yellow"))
        assessment = PreflightAssessment(
            difficulty=3,
            text_noise=3,
            term_density=3,
            structure_complexity=3,
            need_pro_extract=False,
            need_extract_thinking=False,
            summary_effort="high",
            review_effort="high",
            do_review=True,
            rationale="评估解析失败，回退中等配置",
        )

    print(
        colored(
            f"   · difficulty={assessment.difficulty} "
            f"noise={assessment.text_noise} "
            f"terms={assessment.term_density} "
            f"struct={assessment.structure_complexity}\n"
            f"   · 抽页Pro={assessment.need_pro_extract} "
            f"抽thinking={assessment.need_extract_thinking} "
            f"总结={assessment.summary_effort} "
            f"审校={assessment.review_effort} "
            f"做审校={assessment.do_review}\n"
            f"   · 理由：{assessment.rationale}",
            "cyan",
        )
    )
    return assessment

API_KEY_SETUP_HELP = """
请在系统环境中设置 DEEPSEEK_API_KEY，不要把 Key 写进仓库。

【当前会话】
  Linux / macOS (bash/zsh):  export DEEPSEEK_API_KEY="sk-..."
  PowerShell:                $env:DEEPSEEK_API_KEY = "sk-..."
  CMD:                       set DEEPSEEK_API_KEY=sk-...

【可选 .env】
  项目根目录创建 .env（参考 .env.example）：
    DEEPSEEK_API_KEY=sk-...
  启动时会加载，且不覆盖已在 shell 中设置的变量。

【永久·用户级】
  Linux / macOS:  写入 shell 配置后重开终端，例如：
    echo 'export DEEPSEEK_API_KEY="sk-..."' >> ~/.bashrc   # bash
    echo 'export DEEPSEEK_API_KEY="sk-..."' >> ~/.zshrc    # zsh
  PowerShell:
    [System.Environment]::SetEnvironmentVariable(
      "DEEPSEEK_API_KEY", "sk-...", "User")
  CMD:  setx DEEPSEEK_API_KEY "sk-..."  （需新开终端）

详见 README「API Key」。
""".strip()


@dataclass
class Config:
    pdf_name: str
    pdf_source: Path
    pdf_path: Path
    knowledge_path: Path
    summary_path: Path
    gold_path: Path
    profile_name: str
    out_dir: Path
    yes: bool = False  # 跳过 auto 交互确认，直接采用映射策略


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


@dataclass
class ProgressLoad:
    """knowledge JSON 加载结果（含 meta / 可恢复策略）。"""

    state: KnowledgeState
    meta: dict
    stored_strategy: PipelineStrategy | None


class PageContent(BaseModel):
    has_content: bool
    knowledge: list[str] = Field(default_factory=list)


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
2) 原始知识点列表（带页码，权威事实来源，**完整**）
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

# 知识点过长被抽样时：禁止按残缺 KB 做破坏性删改
REVIEW_CITE_ONLY_PROMPT = """你是页码补全编辑。初稿已由**完整**知识点生成；下方知识点仅为**抽样**，不完整。

硬性规则：
- 输出完整修订后 Markdown（仍含 导读 / 分题详述 / 主题索引）
- **禁止删除**初稿中的实质段落或 bullet（抽样不足以判定「无依据」）
- 仅允许：为缺少页码的 bullet 补（第 N 页）；轻微统一术语/润色措辞
- 页码只能来自初稿已有标注或抽样知识点中的页码；禁止编造
- 勿因抽样未见某概念而删改或否定初稿内容
- 勿引入新事实

只输出修订后的 Markdown 全文。
"""

REVIEW_KB_FULL_CHARS = 100_000


def _safe_int(value, default: int | None = None) -> int | None:
    """宽松转 int；失败返回 default。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _unique_sorted_pages(pages: list) -> list[int]:
    out: set[int] = set()
    for p in pages:
        n = _safe_int(p)
        if n is not None and n > 0:
            out.add(n)
    return sorted(out)


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
    bad = 0
    for entry in raw:
        if isinstance(entry, dict) and "text" in entry:
            raw_page = entry.get("page")
            if raw_page is None or raw_page == "":
                page = 0
            else:
                page = _safe_int(raw_page)
                if page is None:
                    bad += 1
                    continue
            text = str(entry["text"]).strip()
            if text:
                items.append(KnowledgeItem(page=page, text=text))
        elif isinstance(entry, str) and entry.strip():
            items.append(KnowledgeItem(page=0, text=entry.strip()))
        else:
            bad += 1
    if bad:
        print(
            colored(
                f"⚠️  knowledge 中跳过 {bad} 条非法条目（page 非整数或结构异常）",
                "yellow",
            ),
            file=sys.stderr,
        )
    return items


def load_dotenv_if_present(path: Path | None = None) -> None:
    """可选加载 .env：不覆盖已有环境变量；依次尝试显式路径、CWD、脚本目录。"""
    candidates: list[Path] = []
    if path is not None:
        candidates.append(path)
    else:
        candidates.append(Path(".env"))
        try:
            candidates.append(Path(__file__).resolve().parent / ".env")
        except NameError:
            pass
    seen: set[Path] = set()
    for env_path in candidates:
        try:
            resolved = env_path.resolve()
        except OSError:
            resolved = env_path
        if resolved in seen:
            continue
        seen.add(resolved)
        if not env_path.is_file():
            continue
        try:
            text = env_path.read_text(encoding="utf-8")
        except OSError:
            continue
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if not key or key in os.environ:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ[key] = value
        return  # 只加载找到的第一份


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(HASH_CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def parse_args(argv: list[str] | None = None) -> Config:
    parser = argparse.ArgumentParser(
        prog="read_books.py",
        description=(
            "逐页抽取 + 总结/审校。参数：PDF，以及可选固定策略档。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "策略说明：\n"
            "  auto      预读若干页后评估，动态选 Flash/Pro 与 high/max（默认）\n"
            "  economy   固定省钱：全 Flash，无审校\n"
            "  balanced  固定平衡：Flash 抽 + Pro 结/审 high\n"
            "  quality   固定最强：Pro 抽+thinking；结/审 max\n"
            f"未指定 --profile 时可读环境变量 {PROFILE_ENV}。\n"
            "输出目录默认 ./book_analysis（相对当前工作目录）；可用 --out-dir 指定。\n"
            "auto 预读后会提示确认；决议写入 knowledge.json 的 meta，"
            "删除 knowledge 可重新预读与选择。\n"
        ),
    )
    parser.add_argument("pdf", help="PDF 路径或文件名")
    parser.add_argument(
        "--profile",
        "-p",
        choices=VALID_PROFILES,
        default=None,
        help="流水线策略档（默认 auto；也可用环境变量 "
        f"{PROFILE_ENV}）",
    )
    parser.add_argument(
        "--out-dir",
        default=DEFAULT_OUT_DIR,
        help=f"产出目录（默认 {DEFAULT_OUT_DIR}，相对当前工作目录）",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="auto 模式下跳过交互确认，直接采用预读映射策略",
    )
    args = parser.parse_args(argv)

    source = Path(args.pdf)
    if source.suffix.lower() != ".pdf":
        parser.error("文件必须是 .pdf")

    # 优先级：--profile > 环境变量 > auto
    if args.profile:
        profile = args.profile
    else:
        profile = (os.environ.get(PROFILE_ENV) or "auto").strip().lower()
        if profile not in VALID_PROFILES:
            print(
                colored(
                    f"⚠️  环境变量 {PROFILE_ENV}={profile!r} 无效，回退 auto",
                    "yellow",
                ),
                file=sys.stderr,
            )
            profile = "auto"

    out_dir = Path(args.out_dir)
    pdf_name = source.name
    stem = source.stem
    return Config(
        pdf_name=pdf_name,
        pdf_source=source,
        pdf_path=out_dir / pdf_name,
        knowledge_path=out_dir / f"{stem}_knowledge.json",
        summary_path=out_dir / f"{stem}.md",
        gold_path=out_dir / f"{stem}_gold.md",
        profile_name=profile,
        out_dir=out_dir,
        yes=bool(args.yes),
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


def _load_knowledge_meta(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        meta = data.get("meta")
        return meta if isinstance(meta, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def setup_directories(config: Config) -> None:
    config.out_dir.mkdir(parents=True, exist_ok=True)

    if not config.pdf_path.exists():
        if config.pdf_source.exists():
            shutil.copy2(config.pdf_source, config.pdf_path)
            print(colored(f"📄 已复制 PDF → {config.pdf_path}", "green"))
        else:
            raise FileNotFoundError(f"找不到 PDF：{config.pdf_source}")
        return

    if (
        not config.pdf_source.exists()
        or config.pdf_source.resolve() == config.pdf_path.resolve()
    ):
        return

    # 源与副本不同路径：更新前校验，防同名换书污染进度
    source_sha = file_sha256(config.pdf_source)
    dest_sha = file_sha256(config.pdf_path)
    if source_sha == dest_sha:
        return  # 内容相同，无需覆盖

    if config.knowledge_path.exists():
        meta = _load_knowledge_meta(config.knowledge_path)
        stored_sha = meta.get("pdf_sha256")
        if stored_sha:
            if source_sha != stored_sha:
                raise SystemExit(
                    "❌ 源 PDF 与进度指纹不一致（可能同名换书）。\n"
                    f"   进度：{config.knowledge_path}\n"
                    f"   源 PDF：{config.pdf_source}\n"
                    f"   副本：  {config.pdf_path}（仍保留，未覆盖）\n"
                    "   新书：删除 knowledge JSON 后重跑；\n"
                    "   同书：请恢复与进度匹配的 PDF。"
                )
            # source == stored 但 dest 不同：用源刷新副本
        elif source_sha != dest_sha:
            raise SystemExit(
                "❌ 已有进度但缺少 PDF 指纹，拒绝用不同内容的源文件覆盖副本。\n"
                f"   进度：{config.knowledge_path}\n"
                f"   源 PDF：{config.pdf_source}\n"
                f"   副本：  {config.pdf_path}\n"
                "   请删除 knowledge JSON 后按当前 PDF 重跑。"
            )

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


def save_knowledge_state(
    config: Config,
    state: KnowledgeState,
    strategy: PipelineStrategy | None = None,
    *,
    pdf_page_count: int | None = None,
    preflight_assessment: PreflightAssessment | dict | None = None,
    mapping_overrides: list[str] | None = None,
    chosen_profile: str | None = None,
    sample_pages: list[int] | None = None,
    proposed_strategy_label: str | None = None,
    confirmed_via: str | None = None,
) -> None:
    print(
        colored(
            f"💾 保存进度（{len(state.knowledge)} 条，"
            f"next_page={state.next_page}）…",
            "blue",
        )
    )
    # 无 strategy 时保留旧 meta 中的策略/指纹，避免 Ctrl+C 二次保存冲掉
    prev_meta: dict = {}
    if config.knowledge_path.exists():
        try:
            with open(config.knowledge_path, "r", encoding="utf-8") as f:
                prev = json.load(f)
            if isinstance(prev.get("meta"), dict):
                prev_meta = prev["meta"]
        except (OSError, json.JSONDecodeError, TypeError):
            prev_meta = {}

    meta: dict = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "profile": config.profile_name,
    }
    for key in (
        "pdf_sha256",
        "pdf_size",
        "pdf_page_count",
        "model_extract",
        "model_summary",
        "strategy",
        "strategy_spec",
        "preflight_assessment",
        "mapping_overrides",
        "chosen_profile",
        "sample_pages",
        "proposed_strategy_label",
        "confirmed_via",
        "decision_version",
    ):
        if key in prev_meta:
            meta[key] = prev_meta[key]

    try:
        if config.pdf_path.exists():
            meta["pdf_sha256"] = file_sha256(config.pdf_path)
            meta["pdf_size"] = config.pdf_path.stat().st_size
    except OSError as exc:
        print(colored(f"⚠️  无法计算 PDF 指纹：{exc}", "yellow"), file=sys.stderr)
    if pdf_page_count is not None:
        meta["pdf_page_count"] = pdf_page_count
    if strategy is not None:
        meta.update(
            {
                "model_extract": strategy.extract_model,
                "model_summary": strategy.summary_model,
                "strategy": strategy.label(),
                "strategy_spec": strategy.to_spec(),
            }
        )
    if preflight_assessment is not None:
        if isinstance(preflight_assessment, PreflightAssessment):
            meta["preflight_assessment"] = preflight_assessment.model_dump()
        elif isinstance(preflight_assessment, dict):
            meta["preflight_assessment"] = preflight_assessment
    if mapping_overrides is not None:
        meta["mapping_overrides"] = list(mapping_overrides)
    if chosen_profile is not None:
        meta["chosen_profile"] = chosen_profile
        meta["profile"] = chosen_profile  # 与 CLI 解耦，记录实际选用
    if sample_pages is not None:
        meta["sample_pages"] = list(sample_pages)
    if proposed_strategy_label is not None:
        meta["proposed_strategy_label"] = proposed_strategy_label
    if confirmed_via is not None:
        meta["confirmed_via"] = confirmed_via
        meta["decision_version"] = PREFLIGHT_DECISION_VERSION
    payload = {
        "knowledge": [item.to_dict() for item in state.knowledge],
        "next_page": state.next_page,
        "skipped": {
            "blank": _unique_sorted_pages(state.skipped_blank),
            "no_content": _unique_sorted_pages(state.skipped_model),
            "parse_error": _unique_sorted_pages(state.skipped_parse),
        },
        "meta": meta,
    }
    atomic_write_json(config.knowledge_path, payload)


def load_knowledge_state(config: Config) -> ProgressLoad:
    if not config.knowledge_path.exists():
        print(colored("🆕 新建进度", "cyan"))
        return ProgressLoad(state=empty_state(), meta={}, stored_strategy=None)

    print(colored("📚 加载进度 JSON…", "cyan"))
    try:
        with open(config.knowledge_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        print(
            colored(
                f"❌ knowledge JSON 损坏，无法解析：{config.knowledge_path}\n"
                f"   {exc}\n"
                "   修复：删除该文件后重跑（将丢失已抽进度）。",
                "yellow",
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    except OSError as exc:
        print(
            colored(
                f"❌ 无法读取 knowledge：{config.knowledge_path}\n   {exc}",
                "yellow",
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from exc

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
    try:
        next_page = int(data["next_page"])
    except (TypeError, ValueError):
        print(
            colored(
                f"❌ knowledge 中 next_page 非法：{data.get('next_page')!r}\n"
                f"   请删除或手工修复：{config.knowledge_path}",
                "yellow",
            ),
            file=sys.stderr,
        )
        raise SystemExit(1)

    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    stored = PipelineStrategy.from_spec(meta.get("strategy_spec") or {})
    print(
        colored(
            f"✅ {len(points)} 条知识点，下一页 = 第 {next_page + 1} 页",
            "green",
        )
    )
    return ProgressLoad(
        state=KnowledgeState(
            knowledge=points,
            next_page=next_page,
            skipped_blank=list(skipped.get("blank") or []),
            skipped_model=list(skipped.get("no_content") or []),
            skipped_parse=list(skipped.get("parse_error") or []),
        ),
        meta=meta,
        stored_strategy=stored,
    )


def validate_progress(
    progress: ProgressLoad,
    config: Config,
    total_pages: int,
) -> None:
    """校验 next_page 边界与 PDF 指纹，防止同名换书或损坏进度。"""
    state = progress.state
    if total_pages < 0:
        print(colored("❌ PDF 页数异常", "yellow"), file=sys.stderr)
        raise SystemExit(1)

    if not (0 <= state.next_page <= total_pages):
        print(
            colored(
                f"❌ 进度 next_page={state.next_page} 非法"
                f"（合法范围 0–{total_pages}）。\n"
                f"   文件：{config.knowledge_path}\n"
                "   请删除 knowledge JSON 后重跑，或手工修正 next_page。",
                "yellow",
            ),
            file=sys.stderr,
        )
        raise SystemExit(1)

    meta = progress.meta
    stored_sha = meta.get("pdf_sha256")
    has_work = state.next_page > 0 or bool(state.knowledge)
    if stored_sha and config.pdf_path.exists():
        try:
            current_sha = file_sha256(config.pdf_path)
        except OSError as exc:
            print(
                colored(f"❌ 无法读取 PDF 以校验指纹：{exc}", "yellow"),
                file=sys.stderr,
            )
            raise SystemExit(1) from exc
        if current_sha != stored_sha:
            print(
                colored(
                    "❌ PDF 内容与进度指纹不一致（可能同名换了文件）。\n"
                    f"   进度：{config.knowledge_path}\n"
                    f"   PDF：  {config.pdf_path}\n"
                    f"   进度指纹：{stored_sha[:16]}…\n"
                    f"   当前指纹：{current_sha[:16]}…\n"
                    "   处理：删除 knowledge JSON 后按新书重抽；"
                    "或恢复与进度匹配的 PDF。",
                    "yellow",
                ),
                file=sys.stderr,
            )
            raise SystemExit(1)
    elif has_work and not stored_sha:
        print(
            colored(
                "❌ 进度已有内容但缺少 pdf_sha256 指纹（旧版或手工 JSON）。\n"
                f"   文件：{config.knowledge_path}\n"
                "   无法验证 PDF 是否与抽取时一致。\n"
                "   请删除 knowledge JSON 后按当前 PDF 重跑。",
                "yellow",
            ),
            file=sys.stderr,
        )
        raise SystemExit(1)

    stored_pages = meta.get("pdf_page_count")
    if stored_pages is not None:
        try:
            stored_n = int(stored_pages)
        except (TypeError, ValueError):
            stored_n = -1
        if stored_n >= 0 and stored_n != total_pages:
            print(
                colored(
                    f"❌ 进度记录页数={stored_n}，当前 PDF 页数={total_pages}，不一致。\n"
                    f"   文件：{config.knowledge_path}\n"
                    "   请删除 knowledge JSON 后重跑。",
                    "yellow",
                ),
                file=sys.stderr,
            )
            raise SystemExit(1)


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


def count_page_citations(text: str) -> int:
    """统计文中页码类引用次数（含「第 N 页」与「第 N–M 页」）。"""
    if not text:
        return 0
    return len(PAGE_CITE_RE.findall(text))


def dedupe_knowledge(items: list[KnowledgeItem]) -> list[KnowledgeItem]:
    """同页同文去重；不同页的相同表述保留（页码对照需要）。"""
    seen: set[tuple[int, str]] = set()
    out: list[KnowledgeItem] = []
    for item in items:
        norm = re.sub(r"\s+", " ", item.text).strip().lower()
        if not norm:
            continue
        key = (int(item.page), norm)
        if key in seen:
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
    strategy: PipelineStrategy,
    *,
    strict_retry: bool = False,
) -> PageContent:
    system = EXTRACT_SYSTEM_PROMPT
    if strict_retry:
        system += (
            "\n\n【加严重试】上一轮未抽出有效知识点，但本页正文较长。"
            "请更仔细扫描定义、命题与例子；仅当确为目录/索引/空白才 has_content=false。"
        )

    use_thinking = strategy.extract_thinking
    max_tokens = (
        EXTRACT_MAX_TOKENS_THINKING if use_thinking else EXTRACT_MAX_TOKENS
    )

    def _call(*, thinking: bool, tokens: int) -> str:
        kwargs: dict = {
            "model": strategy.extract_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            "response_format": {"type": "json_object"},
            "temperature": EXTRACT_TEMPERATURE,
            "max_tokens": tokens,
            "extra_body": (
                EXTRA_BODY_THINKING if thinking else EXTRA_BODY_NO_THINKING
            ),
        }
        if thinking:
            kwargs["reasoning_effort"] = strategy.extract_effort
        completion = chat_create_with_retry(client, **kwargs)
        return (completion.choices[0].message.content or "").strip()

    raw = _call(thinking=use_thinking, tokens=max_tokens)

    # thinking 占满预算时常返回空 content；抬高上限再试，仍空则关 thinking
    if not raw and use_thinking:
        bumped = max(max_tokens * 2, EXTRACT_MAX_TOKENS_THINKING)
        print(
            colored(
                "⚠️  抽页 content 为空（thinking 可能占满 max_tokens），"
                f"提高预算至 {bumped} 重试…",
                "yellow",
            )
        )
        raw = _call(thinking=True, tokens=bumped)
    if not raw and use_thinking:
        print(
            colored(
                "⚠️  仍为空，关闭 thinking 再抽本页…",
                "yellow",
            )
        )
        raw = _call(thinking=False, tokens=EXTRACT_MAX_TOKENS)

    if not raw:
        raw = "{}"
    return PageContent.model_validate(json.loads(raw))


def _drop_page_from_skips(pages: list[int], pdf_page: int) -> list[int]:
    return [p for p in pages if int(p) != int(pdf_page)]


def parse_failed_page_indices(
    state: KnowledgeState, total_pages: int
) -> list[int]:
    """返回仍落在全书范围内的解析失败页（0-based），供重访。"""
    out: list[int] = []
    for p in _unique_sorted_pages(state.skipped_parse):
        if 1 <= p <= total_pages:
            out.append(p - 1)
    return out


def process_page(
    client: OpenAI,
    config: Config,
    page_text: str,
    state: KnowledgeState,
    page_num: int,
    total_pages: int,
    toc: list[tuple[int, str, int]],
    pdf_document: pymupdf.Document,
    strategy: PipelineStrategy,
    *,
    preserve_next_page: bool = False,
) -> KnowledgeState:
    """
    处理单页抽取。

    preserve_next_page=True：重访已跳过页时使用，不推进 next_page，
    并先清除该页旧的 skip 标记以便重新分类。
    """
    pdf_page = page_num + 1
    if preserve_next_page:
        next_state_page = state.next_page
        label = f"🔁 重访第 {pdf_page}/{total_pages} 页（曾解析失败）…"
        skipped_blank = _drop_page_from_skips(state.skipped_blank, pdf_page)
        skipped_model = _drop_page_from_skips(state.skipped_model, pdf_page)
        skipped_parse = _drop_page_from_skips(state.skipped_parse, pdf_page)
    else:
        next_state_page = page_num + 1
        label = f"\n📖 第 {pdf_page}/{total_pages} 页…"
        skipped_blank = list(state.skipped_blank)
        skipped_model = list(state.skipped_model)
        skipped_parse = list(state.skipped_parse)

    print(colored(label, "yellow"))

    cleaned = clean_page_text(page_text)
    if is_blank_page(cleaned):
        print(colored("⏭️  文字过短，跳过（无 OCR）", "yellow"))
        state = KnowledgeState(
            knowledge=state.knowledge,
            next_page=next_state_page,
            skipped_blank=_unique_sorted_pages(skipped_blank + [pdf_page]),
            skipped_model=skipped_model,
            skipped_parse=skipped_parse,
        )
        save_knowledge_state(
            config, state, strategy, pdf_page_count=total_pages
        )
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
        result = _extract_once(client, user_content, strategy)
        if (
            EXTRACT_RETRY_ON_EMPTY
            and (not result.has_content or not result.knowledge)
            and len(cleaned) >= 200
        ):
            print(colored("🔁 空抽取，加严重试本页…", "yellow"))
            result = _extract_once(
                client, user_content, strategy, strict_retry=True
            )
    except (json.JSONDecodeError, ValidationError) as exc:
        print(colored(f"⚠️  解析失败，跳过：{exc}", "yellow"))
        try:
            print(colored("🔁 解析失败，重试本页…", "yellow"))
            result = _extract_once(
                client, user_content, strategy, strict_retry=True
            )
        except (json.JSONDecodeError, ValidationError) as exc2:
            print(colored(f"⚠️  重试仍失败：{exc2}", "yellow"))
            state = KnowledgeState(
                knowledge=state.knowledge,
                next_page=next_state_page,
                skipped_blank=skipped_blank,
                skipped_model=skipped_model,
                skipped_parse=_unique_sorted_pages(skipped_parse + [pdf_page]),
            )
            save_knowledge_state(
                config, state, strategy, pdf_page_count=total_pages
            )
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
        if added == 0:
            skipped_model = _unique_sorted_pages(skipped_model + [pdf_page])
    else:
        print(colored("⏭️  无有效内容", "yellow"))
        knowledge = state.knowledge
        skipped_model = _unique_sorted_pages(skipped_model + [pdf_page])

    state = KnowledgeState(
        knowledge=knowledge,
        next_page=next_state_page,
        skipped_blank=skipped_blank,
        skipped_model=skipped_model,
        skipped_parse=skipped_parse,
    )
    save_knowledge_state(
        config, state, strategy, pdf_page_count=total_pages
    )
    return state


def retry_skipped_parse_pages(
    client: OpenAI,
    config: Config,
    state: KnowledgeState,
    total_pages: int,
    toc: list[tuple[int, str, int]],
    pdf_document: pymupdf.Document,
    strategy: PipelineStrategy,
) -> KnowledgeState:
    """
    对本轮仍记录在 skipped_parse 中的页再抽一次。
    每页每进程最多再试一轮（调用方每 run 调一次即可）。
    """
    indices = parse_failed_page_indices(state, total_pages)
    if not indices:
        return state

    print(
        colored(
            f"\n🔁 重访解析失败页 {len(indices)} 个："
            f"{[i + 1 for i in indices]} …",
            "cyan",
        )
    )
    for page_num in indices:
        # 仍在列表中才重试（前序重访可能已清掉）
        if (page_num + 1) not in set(state.skipped_parse):
            continue
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
            strategy,
            preserve_next_page=True,
        )
    still = parse_failed_page_indices(state, total_pages)
    if still:
        print(
            colored(
                f"   · 仍失败 {len(still)} 页：{[i + 1 for i in still]}",
                "yellow",
            )
        )
    else:
        print(colored("   · 解析失败页已全部重访成功或改判", "green"))
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


def _chat_summary_model(
    client: OpenAI,
    strategy: PipelineStrategy,
    system: str,
    user: str,
    *,
    reasoning_effort: str,
    temperature: float = SUMMARY_TEMPERATURE,
    max_tokens: int = SUMMARY_MAX_TOKENS,
) -> str:
    use_thinking = strategy.summary_thinking
    kwargs: dict = {
        "model": strategy.summary_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "extra_body": (
            EXTRA_BODY_THINKING if use_thinking else EXTRA_BODY_NO_THINKING
        ),
    }
    if use_thinking:
        kwargs["reasoning_effort"] = reasoning_effort

    completion = chat_create_with_retry(client, **kwargs)
    content = completion.choices[0].message.content or ""
    nudge = "\n\n请直接给出完整最终答案正文，确保 content 非空。"
    if not content.strip() and use_thinking:
        bumped = max(max_tokens, SUMMARY_MAX_TOKENS_BUMP)
        print(
            colored(
                "⚠️  模型 content 为空（thinking 可能占满 max_tokens），"
                f"提高预算至 {bumped} 并降 effort 重试…",
                "yellow",
            )
        )
        completion = chat_create_with_retry(
            client,
            model=strategy.summary_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user + nudge},
            ],
            temperature=temperature,
            max_tokens=bumped,
            reasoning_effort="high",
            extra_body=EXTRA_BODY_THINKING,
        )
        content = completion.choices[0].message.content or ""
    if not content.strip() and use_thinking:
        bumped = max(max_tokens, SUMMARY_MAX_TOKENS_BUMP)
        print(
            colored(
                "⚠️  仍为空，关闭 thinking 再生成…",
                "yellow",
            )
        )
        completion = chat_create_with_retry(
            client,
            model=strategy.summary_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user + nudge},
            ],
            temperature=temperature,
            max_tokens=bumped,
            extra_body=EXTRA_BODY_NO_THINKING,
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


def compact_knowledge_index(
    knowledge: list[KnowledgeItem],
    *,
    max_chars: int = 12_000,
    head_per_page: int = 100,
) -> str:
    """
    紧凑页→要点索引，供多块合并终稿时补全「中间稿未覆盖」的线索。
    每页取首条截断 + 条数；超长则截断索引本身。
    """
    if not knowledge:
        return ""
    by_page: dict[int, list[str]] = {}
    for item in knowledge:
        if item.page <= 0:
            continue
        by_page.setdefault(item.page, []).append(item.text)
    lines = [
        "【知识点页索引】（合并时请对照查漏；事实以中间稿与页码为准，勿编造）"
    ]
    size = len(lines[0])
    for p in sorted(by_page):
        texts = by_page[p]
        head = re.sub(r"\s+", " ", texts[0]).strip()
        if len(head) > head_per_page:
            head = head[: head_per_page - 1] + "…"
        extra = f"（共 {len(texts)} 条）" if len(texts) > 1 else ""
        line = f"- 第 {p} 页：{head}{extra}"
        if size + len(line) + 1 > max_chars:
            lines.append(f"- …索引已截断，其余页码：{sorted(by_page.keys())}")
            break
        lines.append(line)
        size += len(line) + 1
    return "\n".join(lines) + "\n\n"


def generate_summary(
    client: OpenAI,
    knowledge: list[KnowledgeItem],
    strategy: PipelineStrategy,
    *,
    gold_notes: str = "",
    toc: list[tuple[int, str, int]] | None = None,
) -> tuple[str, PipelineStrategy]:
    if not knowledge:
        print(colored("\n⚠️  无知识点，无法总结", "yellow"))
        return "", strategy

    knowledge = dedupe_knowledge(knowledge)
    kb_chars = sum(len(i.text) for i in knowledge)
    # 按知识体量再微调分块（保留预读得到的模型/effort）
    tuned_chunk = _tune_chunk_size(
        strategy.summary_chunk_chars,
        total_pages=None,
        knowledge_chars=kb_chars,
    )
    if tuned_chunk != strategy.summary_chunk_chars:
        strategy = strategy.with_updates(
            summary_chunk_chars=tuned_chunk,
            description=strategy.description
            + f"；分块≈{tuned_chunk // 1000}k字(知识量)",
        )
    chunks = chunk_items(knowledge, strategy.summary_chunk_chars)
    total = len(knowledge)
    print(
        colored(
            f"\n🤔 生成总结（{strategy.summary_model}，"
            f"effort={strategy.final_effort}，"
            f"{total} 条 / {len(chunks)} 块，分块≈{strategy.summary_chunk_chars // 1000}k）…",
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
        draft = _chat_summary_model(
            client,
            strategy,
            FINAL_SUMMARY_PROMPT,
            user,
            reasoning_effort=strategy.final_effort,
        )
    else:
        partials: list[str] = []
        for i, chunk in enumerate(chunks, 1):
            pr = chunk_page_range(chunk)
            print(
                colored(
                    f"   · 分块消化 {i}/{len(chunks)}（{pr}）…",
                    "cyan",
                )
            )
            lines = [x.as_line() for x in chunk]
            user = (
                f"全书第 {i}/{len(chunks)} 批，本批约 {pr}，共 {len(chunk)} 条。\n"
                f"只消化本批。\n\n" + "\n".join(lines)
            )
            partials.append(
                _chat_summary_model(
                    client,
                    strategy,
                    PARTIAL_SUMMARY_PROMPT,
                    user,
                    reasoning_effort=strategy.partial_effort,
                )
            )

        print(
            colored(
                f"   · 合并终稿（{strategy.final_effort}）…",
                "cyan",
            )
        )
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
        kb_index = compact_knowledge_index(knowledge)
        user = (
            f"{toc_hint}{gold_hint}{kb_index}"
            f"{len(partials)} 段中间稿；原始 {total} 条；页码跨度约 {span}。\n"
            f"合并为完整终稿：导读 + 分题详述 + 主题索引；"
            f"去重、统一术语、前中后覆盖、页码勿编造；"
            f"若中间稿遗漏索引中的重要页主题，请据索引线索补全（勿编造细节）。\n\n"
            + "\n\n---\n\n".join(merged_parts)
        )
        draft = _chat_summary_model(
            client,
            strategy,
            FINAL_SUMMARY_PROMPT,
            user,
            reasoning_effort=strategy.final_effort,
        )

    summary = draft
    if strategy.do_review:
        kb_lines = [i.as_line() for i in knowledge]
        kb_blob = "\n".join(kb_lines)
        kb_truncated = len(kb_blob) > REVIEW_KB_FULL_CHARS
        if kb_truncated:
            by_page: dict[int, list[str]] = {}
            for item in knowledge:
                by_page.setdefault(item.page, []).append(item.text)
            sample = []
            for p in sorted(by_page):
                for t in by_page[p][:2]:
                    sample.append(f"[第 {p} 页] {t}")
            kb_blob = (
                "（知识点过长，每页最多 2 条抽样；页码集合："
                f"{sorted(by_page.keys())}）\n" + "\n".join(sample)
            )
            # 残缺 KB 下禁止破坏性「对照删除」
            review_system = REVIEW_CITE_ONLY_PROMPT
            print(
                colored(
                    f"   · 编辑审校（{strategy.review_effort}，"
                    "知识点过长→仅补页码/禁止删内容）…",
                    "cyan",
                )
            )
        else:
            review_system = REVIEW_SYSTEM_PROMPT
            print(
                colored(
                    f"   · 编辑审校（{strategy.review_effort}）…",
                    "cyan",
                )
            )

        review_user = (
            f"{toc_hint}{gold_hint}"
            f"## 初稿\n\n{draft}\n\n"
            f"## 原始知识点\n\n{kb_blob}\n"
        )
        reviewed = _chat_summary_model(
            client,
            strategy,
            review_system,
            review_user,
            reasoning_effort=strategy.review_effort,
        )
        if reviewed.strip():
            summary = reviewed
        else:
            print(colored("⚠️  审校输出为空，回退初稿", "yellow"))

        cites = count_page_citations(summary or "")
        need = max(CITE_ESCALATE_MIN, total // 25)
        if (
            strategy.auto_escalate_review
            and cites < need
            and strategy.review_effort != "max"
        ):
            print(
                colored(
                    f"   · 页码引用偏少（约 {cites} < {need}），"
                    f"自动升至 max 再审一轮…",
                    "yellow",
                )
            )
            escalate_extra = (
                "\n\n【加严】上一版页码引用不足：请为每个 bullet 补全"
                "（第 N 页）。"
            )
            if kb_truncated:
                escalate_extra += " 知识点仍为抽样：禁止删除初稿实质内容。"
            else:
                escalate_extra += " 并对照完整知识点查漏。"
            # 以当前审校稿为初稿，保留第一轮结构/术语修订
            escalate_user = (
                f"{toc_hint}{gold_hint}"
                f"## 初稿\n\n{summary}\n\n"
                f"## 原始知识点\n\n{kb_blob}\n"
                f"{escalate_extra}"
            )
            boosted = _chat_summary_model(
                client,
                strategy,
                review_system,
                escalate_user,
                reasoning_effort="max",
            )
            if boosted.strip():
                summary = boosted

    cites = count_page_citations(summary or "")
    print(colored(f"✨ 总结完成（页码类引用约 {cites} 处）", "green"))
    if cites < max(CITE_ESCALATE_MIN, total // 25):
        print(
            colored(
                "⚠️  页码引用仍可能偏少；可换 quality 档或补充 gold 后重跑总结。",
                "yellow",
            )
        )
    return summary, strategy


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
    strategy: PipelineStrategy,
) -> None:
    if not (summary or "").strip():
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
    cite_n = count_page_citations(summary)

    content = f"""# 书籍分析：{config.pdf_name}

- 生成时间：{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
- 策略：{strategy.name} — {strategy.description}
- 抽取模型：{strategy.extract_model}{"（thinking）" if strategy.extract_thinking else ""}
- 总结模型：{strategy.summary_model}（{"thinking " + strategy.final_effort if strategy.summary_thinking else "no-thinking"}{("；审校 " + strategy.review_effort) if strategy.do_review else ""}）
- 覆盖 PDF 页码：{page_range}
- 知识点条数：{len(state.knowledge)}
- 页码类引用（约）：{cite_n} 处
{format_skip_meta(state)}
- 说明：页码为 PDF 阅读器页码（从 1 起）。重写总结请先删除本文件再运行。
- 可选金标准：同目录 `{config.gold_path.name}`
- 策略：`--profile auto|economy|balanced|quality`（或环境变量 `{PROFILE_ENV}`）

{summary.strip()}

---
*由 PDF 书籍分析器（DeepSeek）生成*
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


def _graceful_interrupt(
    config: Config | None,
    state: KnowledgeState | None,
    *,
    phase: str,
) -> None:
    """Ctrl+C：提示已保存进度，干净退出（码 130）。"""
    print(file=sys.stderr)
    print(
        colored(f"⏹️  已中断（{phase}）。正在安全退出…", "yellow"),
        file=sys.stderr,
    )
    if state is not None and config is not None:
        try:
            # 再落盘一次，确保最近内存状态不丢
            save_knowledge_state(config, state)
        except Exception as exc:
            print(
                colored(f"⚠️  退出前保存进度失败：{exc}", "yellow"),
                file=sys.stderr,
            )
        print(
            colored(
                f"   进度已保留：next_page={state.next_page}，"
                f"知识点 {len(state.knowledge)} 条\n"
                f"   文件：{config.knowledge_path}\n"
                f"   下次直接再跑同一命令即可续跑。",
                "cyan",
            ),
            file=sys.stderr,
        )
    else:
        print(
            colored("   （尚未开始写进度，或进度文件未创建）", "cyan"),
            file=sys.stderr,
        )
    raise SystemExit(130)


def main(argv: list[str] | None = None) -> None:
    config: Config | None = None
    state: KnowledgeState | None = None
    pdf_document: pymupdf.Document | None = None
    active_strategy: PipelineStrategy | None = None
    total_pages_for_save: int | None = None

    try:
        load_dotenv_if_present()
        config = parse_args(argv)
        print(
            colored(
                f"""
📚 PDF 书籍分析器
----------------
PDF：    {config.pdf_source}
档位：   {config.profile_name}
产出目录：{config.out_dir}
进度：   {config.knowledge_path}
总结：   {config.summary_path}
金标准： {config.gold_path}（可选）
切换：   --profile auto|economy|balanced|quality
提示：   Ctrl+C 可中断；策略在 knowledge 中，删之可重选
""",
                "cyan",
            )
        )

        try:
            setup_directories(config)
        except FileNotFoundError as exc:
            print(colored(f"⚠️  {exc}", "yellow"), file=sys.stderr)
            raise SystemExit(1) from exc

        progress = load_knowledge_state(config)
        state = progress.state
        try:
            pdf_document = pymupdf.open(config.pdf_path)
        except Exception as exc:
            print(
                colored(
                    f"❌ 无法打开 PDF：{config.pdf_path}\n   {exc}",
                    "yellow",
                ),
                file=sys.stderr,
            )
            raise SystemExit(1) from exc
        total_pages = pdf_document.page_count
        total_pages_for_save = total_pages
        validate_progress(progress, config, total_pages)

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
                    f"   重抽全书 → 删除 knowledge JSON 后再执行\n"
                    f"   重做 auto 预读/改选 → 删除 knowledge 后重跑\n"
                    f"   换总结策略 → --profile … 且已抽完时重跑（仅 md 删后）\n"
                    f"   换抽取模型 → 须删除 knowledge 重抽",
                    "green",
                )
            )
            print(colored("\n✨ 跳过，已退出 ✨", "green", attrs=["bold"]))
            return

        client = create_client()

        # —— 策略：auto 读 knowledge.meta / 预读确认；固定档直接解析 ——
        profile = config.profile_name.strip().lower()
        kb_chars = sum(len(i.text) for i in state.knowledge) or None
        decision_extra: dict = {}
        pdf_sha = file_sha256(config.pdf_path)

        if profile == "auto":
            strategy = None
            # 1) knowledge 已有 strategy_spec → 复用（策略与进度同一文件）
            if progress.stored_strategy is not None:
                strategy = resolve_strategy(
                    progress.stored_strategy.name
                    if progress.stored_strategy.name in VALID_PROFILES
                    else "auto",
                    total_pages=total_pages,
                    knowledge_chars=kb_chars,
                    prebuilt=progress.stored_strategy,
                )
                chosen = progress.meta.get("chosen_profile") or strategy.name
                print(
                    colored(
                        f"♻️  复用 knowledge 中的策略（{chosen}）\n"
                        f"   {strategy.label()}\n"
                        f"   重新预读/改选：删除 {config.knowledge_path.name} 后重跑",
                        "cyan",
                    )
                )
            else:
                # 2) 兼容旧版独立 _preflight.json
                legacy = try_import_legacy_preflight_file(
                    config, pdf_sha256=pdf_sha, total_pages=total_pages
                )
                if legacy is not None:
                    strategy, leg = legacy
                    strategy = resolve_strategy(
                        strategy.name if strategy.name in VALID_PROFILES else "auto",
                        total_pages=total_pages,
                        knowledge_chars=kb_chars,
                        prebuilt=strategy,
                    )
                    decision_extra = {
                        "preflight_assessment": leg.get("assessment"),
                        "mapping_overrides": leg.get("mapping_overrides") or [],
                        "chosen_profile": leg.get("chosen_profile") or strategy.name,
                        "sample_pages": leg.get("sample_pages") or [],
                        "proposed_strategy_label": leg.get(
                            "proposed_strategy_label"
                        )
                        or strategy.label(),
                        "confirmed_via": "legacy_preflight_file",
                    }

            if strategy is None:
                sample_idx = pick_preflight_pages(total_pages)
                samples = collect_preflight_samples(pdf_document, sample_idx)
                sample_pages_1b = [p for p, _ in samples]
                assessment = run_preflight_assessment(
                    client,
                    samples,
                    total_pages=total_pages,
                    has_toc=bool(toc),
                )
                proposed, map_overrides = strategy_from_assessment(
                    assessment, total_pages=total_pages
                )
                proposed = resolve_strategy(
                    "auto",
                    total_pages=total_pages,
                    knowledge_chars=kb_chars,
                    prebuilt=proposed,
                )
                print_auto_analysis_report(
                    total_pages=total_pages,
                    sample_pages=sample_pages_1b,
                    assessment=assessment,
                    proposed=proposed,
                    overrides=map_overrides,
                    has_toc=bool(toc),
                )
                log_assessment_mapping(assessment, proposed, map_overrides)

                chosen_profile = confirm_auto_strategy_interactive(
                    proposed=proposed,
                    yes=config.yes,
                )
                if chosen_profile == "auto":
                    strategy = proposed
                    confirmed_via = (
                        "yes_flag"
                        if config.yes
                        else ("non_tty" if not sys.stdin.isatty() else "interactive")
                    )
                else:
                    strategy = resolve_strategy(
                        chosen_profile,
                        total_pages=total_pages,
                        knowledge_chars=kb_chars,
                    )
                    confirmed_via = "interactive_override"

                decision_extra = {
                    "preflight_assessment": assessment,
                    "mapping_overrides": map_overrides,
                    "chosen_profile": chosen_profile,
                    "sample_pages": sample_pages_1b,
                    "proposed_strategy_label": proposed.label(),
                    "confirmed_via": confirmed_via,
                }
        else:
            strategy = resolve_strategy(
                config.profile_name,
                total_pages=total_pages,
                knowledge_chars=kb_chars,
            )

        # 抽取未完成时：不可更换抽取模型（无 strategy_spec 的中途进度须删 knowledge）
        if 0 < state.next_page < total_pages:
            if progress.stored_strategy is None:
                print(
                    colored(
                        "❌ 进度已开始抽取，但 knowledge 中无 strategy_spec。\n"
                        f"   文件：{config.knowledge_path}\n"
                        "   请删除 knowledge 后重抽。",
                        "yellow",
                    ),
                    file=sys.stderr,
                )
                raise SystemExit(1)
            prev = progress.stored_strategy
            if (
                prev.extract_model != strategy.extract_model
                or prev.extract_thinking != strategy.extract_thinking
            ):
                print(
                    colored(
                        "❌ 抽取未完成，但本次策略的抽取设置与进度不一致：\n"
                        f"   进度：{prev.extract_model}"
                        f"{'+thinking' if prev.extract_thinking else ''}\n"
                        f"   本次：{strategy.extract_model}"
                        f"{'+thinking' if strategy.extract_thinking else ''}\n"
                        "   继续会导致前后页混用不同模型。\n"
                        "   处理：保持原 --profile 续跑；或删除 knowledge 后重抽。",
                        "yellow",
                    ),
                    file=sys.stderr,
                )
                raise SystemExit(1)

        active_strategy = strategy
        print(colored(f"⚙️  生效策略：{strategy.label()}", "cyan"))
        print(colored(f"   {strategy.description}", "cyan"))

        # 尽早写入指纹与 strategy_spec（及 auto 决议），便于中断后续跑
        save_kwargs: dict = {"pdf_page_count": total_pages}
        save_kwargs.update(
            {k: v for k, v in decision_extra.items() if v is not None}
        )
        save_knowledge_state(config, state, strategy, **save_kwargs)

        try:
            if not extract_done:
                start = state.next_page
                print(
                    colored(
                        f"\n📚 共 {total_pages} 页，"
                        f"处理第 {start + 1}–{total_pages} 页"
                        f"（{strategy.extract_model}）…",
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
                        strategy,
                    )
            else:
                print(
                    colored(
                        f"\n✅ 抽取已完成（{state.next_page}/{total_pages}），"
                        f"仅生成总结…",
                        "green",
                    )
                )

            # 解析失败页：每轮运行再访一次（不推进 next_page）
            state = retry_skipped_parse_pages(
                client,
                config,
                state,
                total_pages,
                toc,
                pdf_document,
                strategy,
            )

            if not pages_complete(state, total_pages):
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
                raise SystemExit(1)

            if not state.knowledge:
                print(
                    colored("⚠️  无知识点，无法生成总结", "yellow"),
                    file=sys.stderr,
                )
                print(format_skip_meta(state), file=sys.stderr)
                print(
                    colored(
                        "   提示：若几乎全是「文字过短」，PDF 可能是扫描版，"
                        "需先 OCR。",
                        "cyan",
                    ),
                    file=sys.stderr,
                )
                raise SystemExit(1)

            print(colored("\n📋 跳过统计", "cyan"))
            print(format_skip_meta(state))

            gold_notes = load_gold_notes(config.gold_path)
            summary, strategy = generate_summary(
                client,
                state.knowledge,
                strategy,
                gold_notes=gold_notes,
                toc=toc,
            )
            active_strategy = strategy
            if not (summary or "").strip():
                print(
                    colored(
                        "❌ 总结生成结果为空（模型 content 为空），未写入 md。\n"
                        "   进度 knowledge 已保留；可稍后直接再跑以重试总结。",
                        "yellow",
                    ),
                    file=sys.stderr,
                )
                raise SystemExit(1)
            save_summary(config, summary, state, strategy)

        except (APIStatusError, APIError, APIConnectionError, APITimeoutError) as exc:
            print(
                colored(f"\n⚠️  {format_api_error(exc)}", "yellow"),
                file=sys.stderr,
            )
            print(
                colored(
                    f"进度 next_page={state.next_page}，"
                    f"知识点 {len(state.knowledge)} 条已保存。",
                    "cyan",
                ),
                file=sys.stderr,
            )
            raise SystemExit(1) from None

        print(colored("\n✨ 处理完成 ✨", "green", attrs=["bold"]))

    except KeyboardInterrupt:
        if (
            state is not None
            and config is not None
            and active_strategy is not None
        ):
            try:
                save_knowledge_state(
                    config,
                    state,
                    active_strategy,
                    pdf_page_count=total_pages_for_save,
                )
            except Exception:
                pass
        _graceful_interrupt(config, state, phase="用户 Ctrl+C")
    finally:
        if pdf_document is not None:
            try:
                pdf_document.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
