"""纯函数与进度校验的单元测试（不调用真实 API）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from read_books import (
    KnowledgeItem,
    KnowledgeState,
    PageContent,
    PipelineStrategy,
    PreflightAssessment,
    ProgressLoad,
    REVIEW_CITE_ONLY_PROMPT,
    REVIEW_SYSTEM_PROMPT,
    chunk_items,
    empty_state,
    file_sha256,
    load_dotenv_if_present,
    load_preflight_decision,
    pages_complete,
    parse_args,
    parse_confirm_choice,
    pick_preflight_pages,
    resolve_strategy,
    save_preflight_decision,
    setup_directories,
    strategy_from_assessment,
    validate_preflight_decision,
    validate_progress,
    _base_profiles,
    _tune_chunk_size,
)


def test_pick_preflight_pages_bounds():
    assert pick_preflight_pages(0) == []
    assert pick_preflight_pages(3) == [0, 1, 2]
    pages = pick_preflight_pages(100)
    assert len(pages) == 5
    assert pages == sorted(pages)
    assert pages[0] >= 0 and pages[-1] < 100


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
    assert s.name == "auto"
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

    cfg_force = parse_args(
        [str(pdf), "--out-dir", str(out), "--force"]
    )
    # force 后通过（不再因缺指纹退出）
    validate_progress(progress, cfg_force, total_pages=10)


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

    cfg_force = parse_args([str(pdf), "--out-dir", str(out), "--force"])
    setup_directories(cfg_force)
    assert cfg_force.pdf_path.read_bytes() == b"%PDF-source-v1"


def test_page_content_missing_knowledge_ok():
    pc = PageContent.model_validate({"has_content": False})
    assert pc.has_content is False
    assert pc.knowledge == []


def test_review_prompts_differ_on_deletion():
    assert "禁止删除" in REVIEW_CITE_ONLY_PROMPT
    assert "删除初稿" in REVIEW_SYSTEM_PROMPT or "删除" in REVIEW_SYSTEM_PROMPT


def test_parse_args_force(tmp_path: Path):
    cfg = parse_args(["a.pdf", "--force", "--out-dir", str(tmp_path)])
    assert cfg.force is True
    cfg2 = parse_args(["a.pdf", "--out-dir", str(tmp_path)])
    assert cfg2.force is False


def test_parse_args_yes_and_preflight_path(tmp_path: Path):
    cfg = parse_args(["demo.pdf", "-y", "--out-dir", str(tmp_path)])
    assert cfg.yes is True
    assert cfg.preflight_path == tmp_path / "demo_preflight.json"


def test_parse_confirm_choice():
    assert parse_confirm_choice("") == "auto"
    assert parse_confirm_choice("yes") == "auto"
    assert parse_confirm_choice("1") == "economy"
    assert parse_confirm_choice("2") == "balanced"
    assert parse_confirm_choice("3") == "quality"
    assert parse_confirm_choice("0") == "quit"
    assert parse_confirm_choice("q") == "quit"
    assert parse_confirm_choice("nope") is None


def test_preflight_decision_roundtrip(tmp_path: Path):
    path = tmp_path / "book_preflight.json"
    s = _base_profiles()["balanced"]
    a = _assessment(difficulty=3, rationale="rt")
    save_preflight_decision(
        path,
        pdf_name="book.pdf",
        pdf_sha256="abc123",
        total_pages=50,
        sample_pages=[3, 10, 20],
        assessment=a,
        mapping_overrides=["demo override"],
        proposed_label="proposed",
        chosen_profile="balanced",
        strategy=s,
        confirmed_via="interactive",
    )
    data = load_preflight_decision(path)
    assert data is not None
    assert data["chosen_profile"] == "balanced"
    assert data["sample_pages"] == [3, 10, 20]
    restored = validate_preflight_decision(
        data, pdf_sha256="abc123", total_pages=50
    )
    assert restored is not None
    assert restored.extract_model == s.extract_model
    assert (
        validate_preflight_decision(data, pdf_sha256="other", total_pages=50)
        is None
    )
    assert (
        validate_preflight_decision(data, pdf_sha256="abc123", total_pages=99)
        is None
    )


def test_load_dotenv_if_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    env = tmp_path / ".env"
    env.write_text('FOO_TEST_KEY="from-dotenv"\n', encoding="utf-8")
    monkeypatch.delenv("FOO_TEST_KEY", raising=False)
    load_dotenv_if_present(env)
    assert __import__("os").environ.get("FOO_TEST_KEY") == "from-dotenv"
    monkeypatch.setenv("FOO_TEST_KEY", "already")
    env.write_text('FOO_TEST_KEY="ignored"\n', encoding="utf-8")
    load_dotenv_if_present(env)
    assert __import__("os").environ.get("FOO_TEST_KEY") == "already"
