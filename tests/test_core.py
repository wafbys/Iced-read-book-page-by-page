"""纯函数与进度校验的单元测试（不调用真实 API）。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from read_books import (
    EXTRACT_MAX_TOKENS_THINKING,
    KnowledgeItem,
    KnowledgeState,
    PageContent,
    PipelineStrategy,
    PreflightAssessment,
    ProgressLoad,
    REVIEW_CITE_ONLY_PROMPT,
    REVIEW_SYSTEM_PROMPT,
    atomic_write_json,
    blocking_summary_reason,
    chunk_items,
    compact_knowledge_index,
    count_page_citations,
    dedupe_knowledge,
    detect_skip_genre,
    empty_state,
    file_sha256,
    load_dotenv_if_present,
    load_knowledge_state,
    load_pdf_toc,
    normalize_knowledge_list,
    pages_complete,
    parse_args,
    parse_confirm_choice,
    pick_preflight_pages,
    resolve_strategy,
    save_knowledge_state,
    setup_directories,
    strategy_from_assessment,
    try_import_legacy_preflight_file,
    validate_progress,
    _base_profiles,
    _extract_once,
    _graceful_interrupt,
    _tune_chunk_size,
    process_page,
)


def test_pick_preflight_pages_bounds():
    assert pick_preflight_pages(0) == []
    assert pick_preflight_pages(3) == [0, 1, 2]
    pages = pick_preflight_pages(100)
    assert len(pages) == 5
    assert pages == sorted(pages)
    assert pages[0] >= 0 and pages[-1] < 100


def test_count_page_citations_single_and_range():
    assert count_page_citations("") == 0
    assert count_page_citations("见（第 1 页）说明") == 1
    assert count_page_citations("覆盖（第 12–15 页）") == 1
    assert count_page_citations("覆盖（第 12-15 页）") == 1
    assert count_page_citations("（第 1 页）与（第 2 页）") == 2
    # 范围算 1 次，不要拆成残缺匹配
    text = "论述见（第 3–5 页）与补充（第 8 页）"
    assert count_page_citations(text) == 2


def test_dedupe_keeps_same_text_on_different_pages():
    items = [
        KnowledgeItem(1, "REST 将资源映射为 URI。"),
        KnowledgeItem(1, "REST 将资源映射为 URI。"),  # 同页重复
        KnowledgeItem(5, "REST 将资源映射为 URI。"),  # 后文再出现，应保留
        KnowledgeItem(5, "  REST   将资源映射为 URI。  "),  # 同页空白变体
    ]
    out = dedupe_knowledge(items)
    assert len(out) == 2
    assert {i.page for i in out} == {1, 5}


def test_chunk_items_by_page():
    items = [
        KnowledgeItem(1, "a" * 20),
        KnowledgeItem(1, "b" * 20),
        KnowledgeItem(2, "c" * 20),
    ]
    chunks = chunk_items(items, max_chars=50)
    assert len(chunks) >= 1
    # 同页条目应留在同一块
    for ch in chunks:
        pages = {i.page for i in ch}
        if 1 in pages and len(ch) > 1:
            assert all(i.page == 1 for i in ch) or 2 in pages


def test_pages_complete():
    st = empty_state()
    assert pages_complete(st, 10) is False
    st = KnowledgeState(
        knowledge=[],
        next_page=10,
        skipped_blank=[],
        skipped_model=[],
        skipped_parse=[],
    )
    assert pages_complete(st, 10) is True
    assert pages_complete(st, 11) is False


def test_strategy_spec_roundtrip():
    for name, s in _base_profiles().items():
        restored = PipelineStrategy.from_spec(s.to_spec())
        assert restored is not None, name
        assert restored == s


def test_strategy_from_spec_legacy_missing_summary_thinking():
    s = _base_profiles()["balanced"]
    data = s.to_spec()
    del data["summary_thinking"]
    restored = PipelineStrategy.from_spec(data)
    assert restored is not None
    assert restored.summary_thinking is True


def test_economy_disables_summary_thinking():
    assert _base_profiles()["economy"].summary_thinking is False
    assert _base_profiles()["quality"].extract_thinking is True


def _assessment(**kwargs) -> PreflightAssessment:
    base = dict(
        difficulty=3,
        text_noise=2,
        term_density=2,
        structure_complexity=2,
        need_pro_extract=False,
        need_extract_thinking=False,
        summary_effort="high",
        review_effort="high",
        do_review=True,
        rationale="test",
    )
    base.update(kwargs)
    return PreflightAssessment(**base)


def test_strategy_from_assessment_easy_book():
    a = _assessment(
        difficulty=1,
        text_noise=1,
        term_density=1,
        structure_complexity=1,
        do_review=False,
        rationale="简单",
    )
    s, ov = strategy_from_assessment(a, total_pages=30)
    assert s.name == "suggest"
    assert s.summary_thinking is False
    assert s.do_review is False
    assert s.extract_model.endswith("flash") or "flash" in s.extract_model


def test_hard_gate_blocks_pro_on_easy_book():
    a = _assessment(
        difficulty=2,
        text_noise=2,
        need_pro_extract=True,
        need_extract_thinking=True,
        do_review=False,
    )
    s, ov = strategy_from_assessment(a)
    assert "flash" in s.extract_model
    assert s.extract_thinking is False
    assert any("Flash 抽页" in x for x in ov)
    assert any("thinking" in x for x in ov)


def test_hard_gate_thinking_only_at_diff5():
    a = _assessment(
        difficulty=4,
        need_pro_extract=True,
        need_extract_thinking=True,
    )
    s, ov = strategy_from_assessment(a)
    assert "pro" in s.extract_model
    assert s.extract_thinking is False
    assert any("difficulty=4<5" in x or "difficulty" in x for x in ov)

    a5 = _assessment(
        difficulty=5,
        need_pro_extract=True,
        need_extract_thinking=True,
        text_noise=3,
    )
    s5, _ = strategy_from_assessment(a5)
    assert s5.extract_thinking is True
    assert s5.final_effort == "max"
    assert s5.do_review is True


def test_noise_forces_review_and_effort():
    a = _assessment(
        difficulty=2,
        text_noise=5,
        term_density=2,
        need_pro_extract=False,
        summary_effort="high",
        review_effort="high",
        do_review=False,
    )
    s, ov = strategy_from_assessment(a)
    assert s.do_review is True
    assert s.final_effort == "max"
    assert s.review_effort == "max"
    assert any("text_noise" in x for x in ov)


def test_term_density_upgrades_extract_and_summary():
    a = _assessment(
        difficulty=3,
        term_density=5,
        need_pro_extract=False,
        summary_effort="high",
    )
    s, ov = strategy_from_assessment(a)
    assert "pro" in s.extract_model
    assert s.final_effort == "max"
    assert any("term_density" in x for x in ov)


def test_diff3_forces_review_override_message():
    a = _assessment(difficulty=3, do_review=False, text_noise=2)
    s, ov = strategy_from_assessment(a)
    assert s.do_review is True
    assert any("审校" in x for x in ov)


def test_resolve_strategy_fixed():
    s = resolve_strategy("economy", total_pages=100)
    assert s.name == "economy"
    assert s.summary_thinking is False


def test_tune_chunk_size():
    assert _tune_chunk_size(100_000, total_pages=20, knowledge_chars=None) >= 150_000
    assert _tune_chunk_size(100_000, total_pages=300, knowledge_chars=None) <= 70_000


def test_parse_args_out_dir(tmp_path: Path):
    cfg = parse_args(["demo.pdf", "-p", "balanced", "--out-dir", str(tmp_path)])
    assert cfg.profile_name == "balanced"


def test_parse_args_default_is_economy(tmp_path: Path):
    cfg = parse_args(["demo.pdf", "--out-dir", str(tmp_path)])
    assert cfg.profile_name == "economy"


def test_parse_args_auto_alias_is_suggest(tmp_path: Path):
    cfg = parse_args(["demo.pdf", "-p", "auto", "--out-dir", str(tmp_path)])
    assert cfg.profile_name == "suggest"
    assert cfg.out_dir == tmp_path
    assert cfg.knowledge_path == tmp_path / "demo_knowledge.json"
    assert cfg.summary_path == tmp_path / "demo.md"


def test_file_sha256(tmp_path: Path):
    p = tmp_path / "a.bin"
    p.write_bytes(b"hello")
    assert file_sha256(p) == file_sha256(p)
    q = tmp_path / "b.bin"
    q.write_bytes(b"world")
    assert file_sha256(p) != file_sha256(q)


def test_validate_progress_next_page(tmp_path: Path):
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    cfg = parse_args([str(pdf), "--out-dir", str(tmp_path)])
    bad = ProgressLoad(
        state=KnowledgeState(
            knowledge=[],
            next_page=-1,
            skipped_blank=[],
            skipped_model=[],
            skipped_parse=[],
        ),
        meta={},
        stored_strategy=None,
    )
    with pytest.raises(SystemExit):
        validate_progress(bad, cfg, total_pages=10)

    huge = ProgressLoad(
        state=KnowledgeState(
            knowledge=[],
            next_page=99,
            skipped_blank=[],
            skipped_model=[],
            skipped_parse=[],
        ),
        meta={},
        stored_strategy=None,
    )
    with pytest.raises(SystemExit):
        validate_progress(huge, cfg, total_pages=10)


def test_validate_progress_fingerprint_mismatch(tmp_path: Path):
    src = tmp_path / "src"
    out = tmp_path / "out"
    src.mkdir()
    out.mkdir()
    pdf = src / "book.pdf"
    pdf.write_bytes(b"%PDF-old")
    cfg = parse_args([str(pdf), "--out-dir", str(out)])
    # 副本与进度指纹不一致
    cfg.pdf_path.write_bytes(b"%PDF-new-content!!")
    progress = ProgressLoad(
        state=empty_state(),
        meta={"pdf_sha256": file_sha256(pdf)},
        stored_strategy=None,
    )
    with pytest.raises(SystemExit):
        validate_progress(progress, cfg, total_pages=1)


def test_validate_progress_missing_fingerprint_with_work(tmp_path: Path):
    src = tmp_path / "src"
    out = tmp_path / "out"
    src.mkdir()
    out.mkdir()
    pdf = src / "book.pdf"
    pdf.write_bytes(b"%PDF-1.4 content")
    cfg = parse_args([str(pdf), "--out-dir", str(out)])
    cfg.pdf_path.write_bytes(b"%PDF-1.4 content")
    progress = ProgressLoad(
        state=KnowledgeState(
            knowledge=[KnowledgeItem(1, "point")],
            next_page=3,
            skipped_blank=[],
            skipped_model=[],
            skipped_parse=[],
        ),
        meta={},  # 无指纹
        stored_strategy=None,
    )
    with pytest.raises(SystemExit):
        validate_progress(progress, cfg, total_pages=10)


def test_setup_directories_blocks_overwrite_without_fingerprint(tmp_path: Path):
    src = tmp_path / "src"
    out = tmp_path / "out"
    src.mkdir()
    out.mkdir()
    pdf = src / "book.pdf"
    pdf.write_bytes(b"%PDF-source-v1")
    cfg = parse_args([str(pdf), "--out-dir", str(out)])
    cfg.pdf_path.write_bytes(b"%PDF-dest-old!!")
    cfg.knowledge_path.write_text(
        json.dumps({"next_page": 2, "knowledge": [], "meta": {}}),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit):
        setup_directories(cfg)
    # 副本保持原样
    assert cfg.pdf_path.read_bytes() == b"%PDF-dest-old!!"


def test_setup_directories_blocks_overwrite_on_fingerprint_mismatch(
    tmp_path: Path,
):
    """有指纹且源≠进度时不得覆盖与进度匹配的副本。"""
    src = tmp_path / "src"
    out = tmp_path / "out"
    src.mkdir()
    out.mkdir()
    pdf = src / "book.pdf"
    pdf.write_bytes(b"%PDF-NEW-BOOK!!!!")
    dest = out / "book.pdf"
    old = b"%PDF-OLD-MATCHES-KNOWLEDGE"
    dest.write_bytes(old)
    knowledge = out / "book_knowledge.json"
    knowledge.write_text(
        json.dumps(
            {
                "next_page": 2,
                "knowledge": [],
                "meta": {"pdf_sha256": file_sha256(dest)},
            }
        ),
        encoding="utf-8",
    )
    cfg = parse_args([str(pdf), "--out-dir", str(out)])
    with pytest.raises(SystemExit):
        setup_directories(cfg)
    assert dest.read_bytes() == old


def test_page_content_missing_knowledge_ok():
    pc = PageContent.model_validate({"has_content": False})
    assert pc.has_content is False
    assert pc.knowledge == []


def test_review_prompts_differ_on_deletion():
    assert "禁止删除" in REVIEW_CITE_ONLY_PROMPT
    assert "删除初稿" in REVIEW_SYSTEM_PROMPT or "删除" in REVIEW_SYSTEM_PROMPT


def test_summary_prompts_are_reading_route():
    from read_books import FINAL_SUMMARY_PROMPT, PARTIAL_SUMMARY_PROMPT

    assert "阅读路线" in FINAL_SUMMARY_PROMPT
    assert "（第 N 页）" in FINAL_SUMMARY_PROMPT
    assert "分题详述" not in FINAL_SUMMARY_PROMPT
    assert "- [ ]" not in FINAL_SUMMARY_PROMPT
    assert "（第 N 页）" in PARTIAL_SUMMARY_PROMPT
    assert "不要" in PARTIAL_SUMMARY_PROMPT and "论证写完" in PARTIAL_SUMMARY_PROMPT


def test_parse_args_yes(tmp_path: Path):
    cfg = parse_args(["demo.pdf", "-y", "--out-dir", str(tmp_path)])
    assert cfg.yes is True
    assert cfg.knowledge_path == tmp_path / "demo_knowledge.json"


def test_parse_confirm_choice():
    assert parse_confirm_choice("") == "suggest"
    assert parse_confirm_choice("yes") == "suggest"
    assert parse_confirm_choice("auto") == "suggest"
    assert parse_confirm_choice("1") == "economy"
    assert parse_confirm_choice("2") == "balanced"
    assert parse_confirm_choice("3") == "quality"
    assert parse_confirm_choice("0") == "quit"
    assert parse_confirm_choice("q") == "quit"
    assert parse_confirm_choice("nope") is None


def test_legacy_preflight_file_import(tmp_path: Path):
    """旧版 _preflight.json 可被读入；指纹不符则忽略。"""
    from read_books import Config, PipelineStrategy

    s = _base_profiles()["balanced"]
    out = tmp_path / "out"
    out.mkdir()
    legacy = out / "book_preflight.json"
    legacy.write_text(
        json.dumps(
            {
                "pdf_sha256": "abc123",
                "pdf_page_count": 50,
                "strategy_spec": s.to_spec(),
                "chosen_profile": "balanced",
            }
        ),
        encoding="utf-8",
    )
    cfg = Config(
        pdf_name="book.pdf",
        pdf_source=tmp_path / "book.pdf",
        pdf_path=out / "book.pdf",
        knowledge_path=out / "book_knowledge.json",
        summary_path=out / "book.md",
        gold_path=out / "book_gold.md",
        profile_name="suggest",
        out_dir=out,
    )
    got = try_import_legacy_preflight_file(
        cfg, pdf_sha256="abc123", total_pages=50
    )
    assert got is not None
    strategy, data = got
    assert strategy.extract_model == s.extract_model
    assert data["chosen_profile"] == "balanced"
    assert (
        try_import_legacy_preflight_file(
            cfg, pdf_sha256="other", total_pages=50
        )
        is None
    )


def test_normalize_knowledge_skips_bad_pages():
    items = normalize_knowledge_list(
        [
            {"page": 1, "text": "ok item long enough"},
            {"page": "x", "text": "bad page"},
            {"page": 2, "text": "another ok"},
            "plain string note",
        ]
    )
    assert len(items) == 3
    assert items[0].page == 1
    assert items[1].page == 2
    assert items[2].page == 0


def test_compact_knowledge_index_includes_pages():
    items = [
        KnowledgeItem(1, "第一页定义 REST 与资源模型。"),
        KnowledgeItem(1, "第一页第二条"),
        KnowledgeItem(5, "第五页超媒体约束。"),
    ]
    idx = compact_knowledge_index(items)
    assert "第 1 页" in idx
    assert "第 5 页" in idx
    assert "共 2 条" in idx


def test_load_dotenv_if_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    env = tmp_path / ".env"
    env.write_text('FOO_TEST_KEY="from-dotenv"\n', encoding="utf-8")
    monkeypatch.delenv("FOO_TEST_KEY", raising=False)
    load_dotenv_if_present(env)
    assert os.environ.get("FOO_TEST_KEY") == "from-dotenv"
    monkeypatch.setenv("FOO_TEST_KEY", "already")
    env.write_text('FOO_TEST_KEY="ignored"\n', encoding="utf-8")
    load_dotenv_if_present(env)
    assert os.environ.get("FOO_TEST_KEY") == "already"
    monkeypatch.setenv("FOO_TEST_KEY", "")
    env.write_text('FOO_TEST_KEY="from-dotenv"\n', encoding="utf-8")
    load_dotenv_if_present(env)
    assert os.environ.get("FOO_TEST_KEY") == "from-dotenv"


def _cfg(tmp_path: Path, *, profile: str = "suggest"):
    from read_books import Config

    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    return Config(
        pdf_name="book.pdf",
        pdf_source=tmp_path / "book.pdf",
        pdf_path=out / "book.pdf",
        knowledge_path=out / "book_knowledge.json",
        summary_path=out / "book.md",
        gold_path=out / "book_gold.md",
        profile_name=profile,
        out_dir=out,
    )


def test_save_keeps_chosen_profile_on_auto_incremental(tmp_path: Path):
    cfg = _cfg(tmp_path, profile="suggest")
    state = empty_state()
    economy = _base_profiles()["economy"]
    save_knowledge_state(
        cfg, state, economy, chosen_profile="economy"
    )
    save_knowledge_state(cfg, state, economy)
    meta = json.loads(cfg.knowledge_path.read_text(encoding="utf-8"))["meta"]
    assert meta["profile"] == "economy"
    assert meta["chosen_profile"] == "economy"


def test_load_knowledge_normalizes_string_skip_pages(tmp_path: Path):
    cfg = _cfg(tmp_path)
    cfg.knowledge_path.write_text(
        json.dumps(
            {
                "next_page": 4,
                "knowledge": [],
                "skipped": {
                    "blank": ["1"],
                    "no_content": ["2"],
                    "parse_error": ["3"],
                },
                "meta": {},
            }
        ),
        encoding="utf-8",
    )
    progress = load_knowledge_state(cfg)
    assert progress.state.skipped_blank == [1]
    assert progress.state.skipped_model == [2]
    assert progress.state.skipped_parse == [3]


def test_load_pdf_toc_skips_malformed_entries():
    pdf = MagicMock()
    pdf.get_toc.return_value = [
        [1, "Good", 3],
        [1, "Bad page", None],
        ["x", "Bad level", 4],
        [2, "Also good", 8],
        None,
        [1, "", 2],
    ]
    assert load_pdf_toc(pdf) == [(1, "Good", 3), (2, "Also good", 8)]


def test_blocking_summary_reason(tmp_path: Path):
    cfg = _cfg(tmp_path)
    assert blocking_summary_reason(cfg, extract_done=False) is None

    cfg.summary_path.write_text("old leftover", encoding="utf-8")
    reason = blocking_summary_reason(cfg, extract_done=False)
    assert reason is not None and "尚未完成" in reason

    cfg.knowledge_path.write_text("{}", encoding="utf-8")
    os.utime(cfg.summary_path, (1_000, 1_000))
    os.utime(cfg.knowledge_path, (2_000, 2_000))
    reason = blocking_summary_reason(cfg, extract_done=True, meta={})
    assert reason is not None and "早于" in reason

    assert (
        blocking_summary_reason(
            cfg, extract_done=True, meta={"summary_sha256": "abc"}
        )
        is None
    )

    os.utime(cfg.summary_path, (3_000, 3_000))
    os.utime(cfg.knowledge_path, (2_000, 2_000))
    assert blocking_summary_reason(cfg, extract_done=True, meta={}) is None


def test_graceful_interrupt_does_not_rewrite_knowledge():
    """中断提示不得改写 knowledge，以免抬高 mtime 误判旧 md。"""
    cfg = MagicMock()
    cfg.knowledge_path = Path("unused.json")
    with patch("read_books.save_knowledge_state") as save:
        with pytest.raises(SystemExit) as ei:
            _graceful_interrupt(cfg, empty_state(), phase="test")
    assert ei.value.code == 130
    save.assert_not_called()


def test_atomic_write_json_cleans_tmp_on_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    dest = tmp_path / "x.json"

    def boom(*_a, **_k):
        raise KeyboardInterrupt

    monkeypatch.setattr("read_books.os.fsync", boom)
    with pytest.raises(KeyboardInterrupt):
        atomic_write_json(dest, {"a": 1})
    assert list(tmp_path.glob("*.tmp")) == []
    assert not dest.exists()


def _completion(content: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def test_extract_once_empty_returns_no_content():
    strategy = _base_profiles()["economy"]
    with patch(
        "read_books.chat_create_with_retry",
        return_value=_completion(""),
    ):
        result = _extract_once(MagicMock(), "page body", strategy)
    assert result.has_content is False
    assert result.knowledge == []


def test_extract_once_thinking_retries_truncated_json():
    strategy = _base_profiles()["quality"]
    calls: list[dict] = []

    def fake_create(*_a, **kwargs):
        calls.append(
            {
                "tokens": kwargs.get("max_tokens"),
                "thinking": (kwargs.get("extra_body") or {}).get("thinking"),
            }
        )
        if len(calls) < 3:
            return _completion("{")
        return _completion(
            json.dumps(
                {
                    "has_content": True,
                    "knowledge": ["这是一条足够长的知识点用于通过过滤。"],
                },
                ensure_ascii=False,
            )
        )

    with patch("read_books.chat_create_with_retry", side_effect=fake_create):
        result = _extract_once(MagicMock(), "page body", strategy)
    assert result.has_content is True
    assert len(calls) == 3
    assert calls[0]["thinking"] == {"type": "enabled"}
    assert calls[1]["tokens"] >= EXTRACT_MAX_TOKENS_THINKING
    assert calls[2]["thinking"] == {"type": "disabled"}


def test_detect_skip_genre_dotted_toc():
    lines = [
        f"第{i} 章 标题{i}" + "." * 40 + f"{20 + i}" for i in range(1, 12)
    ]
    assert detect_skip_genre("\n".join(lines)) == "toc"


def test_detect_skip_genre_split_english_contents():
    parts = ["v", "contents", "preface", "ix", "acknowledgments", "xi"]
    for i in range(1, 12):
        parts.extend([str(i), "■", f"Chapter title {i}", str(i * 10)])
    assert detect_skip_genre("\n".join(parts)) == "toc"


def test_detect_skip_genre_does_not_treat_narrative_toc_metaphor():
    text = (
        "26\nCHAPTER 2\nNarrative code\n"
        "Table of contents as a narrative level is an overview of a very complex story.\n"
        "Actions are the simplest narrative elements. They represent small, individual "
        "steps such as executing a query or sending a message. Extract methods so that "
        "each action holds a single chunk the reader can keep in working memory.\n"
        "Chapters orchestrate several scenes to complete a business goal."
    )
    assert detect_skip_genre(text) is None


def test_detect_skip_genre_index_and_references():
    index_lines = [f"term{i} about something {i+20}" for i in range(20)]
    assert detect_skip_genre("index\n" + "\n".join(index_lines)) == "index"
    refs = ["参考文献 75", "参考文献"]
    for i in range(1, 8):
        refs.append(
            f"{i}. A. Author. Some paper title here. Journal, {1990 + i}."
        )
    assert detect_skip_genre("\n".join(refs)) == "references"


def test_detect_skip_genre_front_matter_not_about_this_book():
    author = (
        "xvii\nabout the author\n"
        "SANDRINE BANAS is a senior Java expert with over 25 years of experience.\n"
        "She speaks at Devoxx and other conferences."
    )
    assert detect_skip_genre(author) == "front_matter"
    back = (
        "ISBN-13: 978-1-63343-492-9\n"
        "Software development is an inherently creative activity, and yet we "
        "regularly reduce it to the formulaic or mechanical."
    )
    assert detect_skip_genre(back) == "front_matter"
    about_book = (
        "xiii\nabout this book\n"
        "The Art of Code is a book for developers who aspire to create software "
        "that is both beautiful and enduring. In an era where AI is reshaping "
        "programming, it refocuses attention on skills, creativity, and beauty.\n"
        "Chapter 1 establishes the rosette model of eight quality dimensions.\n"
        "Chapter 2 treats programs as stories with five fundamental plots."
    )
    assert detect_skip_genre(about_book) is None


def test_process_page_skips_toc_without_api():
    strategy = _base_profiles()["economy"]
    config = MagicMock()
    state = empty_state()
    toc_text = "\n".join(
        f"第{i} 章 主题{i}" + "." * 36 + f"{10 + i}" for i in range(1, 12)
    )
    with patch("read_books._extract_once") as extract, patch(
        "read_books.save_knowledge_state"
    ):
        new_state = process_page(
            MagicMock(),
            config,
            toc_text,
            state,
            page_num=3,
            total_pages=20,
            toc=[],
            pdf_document=MagicMock(),
            strategy=strategy,
        )
    extract.assert_not_called()
    assert 4 in new_state.skipped_model
    assert new_state.knowledge == []
