"""mock API 的端到端冒烟：auto -y → 抽页 → 总结。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pymupdf
import pytest

import read_books


def _make_pdf(path: Path, texts: list[str]) -> None:
    doc = pymupdf.open()
    for text in texts:
        page = doc.new_page()
        # 保证超过 MIN_PAGE_CHARS
        body = text if len(text) >= 50 else (text + " " + "内容补充。" * 20)
        page.insert_text((72, 72), body)
    doc.save(path)
    doc.close()


def _msg(content: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def _fake_chat_create(*_args, **kwargs):
    """按 system/user 内容返回预读 / 抽页 / 总结 JSON 或 Markdown。"""
    messages = kwargs.get("messages") or []
    system = ""
    user = ""
    for m in messages:
        if m.get("role") == "system":
            system = m.get("content") or ""
        if m.get("role") == "user":
            user = m.get("content") or ""

    if "文档难度评估器" in system or "difficulty" in system and "need_pro_extract" in system:
        payload = {
            "difficulty": 2,
            "text_noise": 1,
            "term_density": 2,
            "structure_complexity": 2,
            "need_pro_extract": False,
            "need_extract_thinking": False,
            "summary_effort": "high",
            "review_effort": "high",
            "do_review": False,
            "rationale": "e2e mock easy book",
        }
        return _msg(json.dumps(payload, ensure_ascii=False))

    if "has_content" in system or "知识点" in system and "JSON" in system:
        payload = {
            "has_content": True,
            "knowledge": [
                "REST 将资源映射为 URI，并用统一接口操作这些资源。",
                "无状态约束要求每个请求自带全部上下文。",
            ],
        }
        return _msg(json.dumps(payload, ensure_ascii=False))

    # 总结 / 审校：返回带页码的 markdown
    return _msg(
        "## 导读\n\n本书介绍 REST 风格。（第 1 页）\n\n"
        "## 分题详述\n\n### 资源\n\n- URI 标识资源（第 1 页）\n\n"
        "## 主题索引\n\n- REST：1\n"
    )


@pytest.fixture()
def api_key_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-e2e-not-real")


def test_main_auto_yes_end_to_end(tmp_path: Path, api_key_env, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    pdf_path = tmp_path / "sample_book.pdf"
    _make_pdf(
        pdf_path,
        [
            "Chapter One introduces REST architectural style and resources.",
            "Chapter Two covers hypermedia and stateless communication constraints.",
        ],
    )
    out = tmp_path / "out"
    argv = [
        str(pdf_path),
        "--profile",
        "auto",
        "--yes",
        "--out-dir",
        str(out),
    ]

    with patch.object(read_books, "chat_create_with_retry", side_effect=_fake_chat_create):
        read_books.main(argv)

    knowledge = out / "sample_book_knowledge.json"
    summary = out / "sample_book.md"
    preflight = out / "sample_book_preflight.json"
    assert knowledge.is_file()
    assert summary.is_file()
    assert not preflight.is_file()  # 策略已并入 knowledge，不再单独写

    data = json.loads(knowledge.read_text(encoding="utf-8"))
    assert data["next_page"] >= 2
    assert len(data.get("knowledge") or []) >= 1
    meta = data.get("meta") or {}
    assert "strategy_spec" in meta
    assert meta.get("chosen_profile") == "auto"

    md = summary.read_text(encoding="utf-8")
    assert "导读" in md or "REST" in md


def test_main_second_run_skips_when_complete(
    tmp_path: Path, api_key_env, monkeypatch: pytest.MonkeyPatch
):
    """完成后再次运行应早退，不依赖 API。"""
    monkeypatch.chdir(tmp_path)
    pdf_path = tmp_path / "done.pdf"
    _make_pdf(pdf_path, ["Already finished book content for testing skip path."])
    out = tmp_path / "out2"
    argv = [str(pdf_path), "-p", "economy", "--out-dir", str(out)]

    with patch.object(read_books, "chat_create_with_retry", side_effect=_fake_chat_create):
        read_books.main(argv)

    # 第二次：不 patch API 也应成功退出（已完成）
    read_books.main(argv)
    assert (out / "done.md").is_file()


def test_main_blocks_extract_when_leftover_md(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """抽取未完成但 md 已在时，必须在调用 API 前退出。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    pdf_path = tmp_path / "book.pdf"
    _make_pdf(pdf_path, ["Leftover markdown must not trigger a paid extract."])
    out = tmp_path / "out3"
    out.mkdir()
    leftover = out / "book.md"
    leftover.write_text("# old book guide\n", encoding="utf-8")
    argv = [str(pdf_path), "-p", "economy", "--out-dir", str(out)]

    with pytest.raises(SystemExit) as ei:
        read_books.main(argv)
    assert ei.value.code == 1
    assert leftover.read_text(encoding="utf-8") == "# old book guide\n"
    assert not (out / "book_knowledge.json").exists()


def test_main_rejects_stale_md_after_new_knowledge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """抽完但 md 早于 knowledge 且无 summary_sha256：视为旧书残留。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    pdf_path = tmp_path / "stale.pdf"
    _make_pdf(pdf_path, ["New extract finished, leftover markdown is older."])
    out = tmp_path / "out4"
    out.mkdir()
    dest_pdf = out / "stale.pdf"
    dest_pdf.write_bytes(pdf_path.read_bytes())
    knowledge = out / "stale_knowledge.json"
    knowledge.write_text(
        json.dumps(
            {
                "next_page": 1,
                "knowledge": [
                    {"page": 1, "text": "新书已抽完的一条知识点。"}
                ],
                "skipped": {"blank": [], "no_content": [], "parse_error": []},
                "meta": {"pdf_sha256": read_books.file_sha256(dest_pdf)},
            }
        ),
        encoding="utf-8",
    )
    md = out / "stale.md"
    md.write_text("# leftover from previous book\n", encoding="utf-8")
    os.utime(md, (1_000, 1_000))
    os.utime(knowledge, (5_000, 5_000))

    argv = [str(pdf_path), "-p", "economy", "--out-dir", str(out)]
    with pytest.raises(SystemExit) as ei:
        read_books.main(argv)
    assert ei.value.code == 1
    assert md.read_text(encoding="utf-8") == "# leftover from previous book\n"


def test_main_preflight_api_error_is_friendly(
    tmp_path: Path, api_key_env, monkeypatch: pytest.MonkeyPatch, capsys
):
    monkeypatch.chdir(tmp_path)
    pdf_path = tmp_path / "pre.pdf"
    _make_pdf(pdf_path, ["Preflight should use the friendly API error path."])
    out = tmp_path / "out5"
    argv = [str(pdf_path), "--profile", "auto", "-y", "--out-dir", str(out)]

    import httpx

    req = httpx.Request("POST", "https://api.deepseek.com/v1/chat/completions")
    err = read_books.APIError(
        "insufficient balance",
        req,
        body={"error": {"message": "insufficient balance"}},
    )
    err.status_code = 402

    with patch.object(
        read_books, "run_preflight_assessment", side_effect=err
    ):
        with pytest.raises(SystemExit) as ei:
            read_books.main(argv)
    assert ei.value.code == 1
    captured = capsys.readouterr()
    text = captured.err + captured.out
    assert "402" in text or "余额" in text
    assert "Traceback" not in text
