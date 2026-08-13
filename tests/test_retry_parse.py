"""解析失败页重访相关单元测试。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from read_books import (
    KnowledgeItem,
    KnowledgeState,
    PageContent,
    parse_failed_page_indices,
    process_page,
    retry_skipped_parse_pages,
    _base_profiles,
)


def test_parse_failed_page_indices_bounds():
    st = KnowledgeState(
        knowledge=[],
        next_page=5,
        skipped_blank=[],
        skipped_model=[],
        skipped_parse=[1, 3, 99, 0, -1],
    )
    assert parse_failed_page_indices(st, total_pages=10) == [0, 2]


def test_process_page_preserve_next_on_retry_success():
    strategy = _base_profiles()["economy"]
    config = MagicMock()
    config.knowledge_path = MagicMock()
    state = KnowledgeState(
        knowledge=[],
        next_page=5,
        skipped_blank=[],
        skipped_model=[],
        skipped_parse=[2],
    )
    pdf = MagicMock()
    pdf.page_count = 10
    page_obj = MagicMock()
    page_obj.get_text.return_value = "lookahead text"
    pdf.__getitem__.return_value = page_obj
    client = MagicMock()

    with patch(
        "read_books._extract_once",
        return_value=PageContent(
            has_content=True,
            knowledge=["这是一条足够长的知识点用于通过长度过滤。"],
        ),
    ), patch("read_books.save_knowledge_state"):
        new_state = process_page(
            client,
            config,
            "x" * 100,
            state,
            page_num=1,
            total_pages=10,
            toc=[],
            pdf_document=pdf,
            strategy=strategy,
            preserve_next_page=True,
        )

    assert new_state.next_page == 5  # 未推进
    assert 2 not in new_state.skipped_parse
    assert any(i.page == 2 for i in new_state.knowledge)


def test_process_page_preserve_next_on_retry_still_fail():
    strategy = _base_profiles()["economy"]
    config = MagicMock()
    state = KnowledgeState(
        knowledge=[],
        next_page=5,
        skipped_blank=[],
        skipped_model=[],
        skipped_parse=[2],
    )

    def boom(*_a, **_k):
        raise ValueError("bad json")  # will be wrong type - need JSONDecodeError

    import json

    def boom2(*_a, **_k):
        raise json.JSONDecodeError("x", "y", 0)

    pdf = MagicMock()
    pdf.page_count = 10
    page_obj = MagicMock()
    page_obj.get_text.return_value = "lookahead text"
    pdf.__getitem__.return_value = page_obj
    with patch("read_books._extract_once", side_effect=boom2), patch(
        "read_books.save_knowledge_state"
    ):
        new_state = process_page(
            MagicMock(),
            config,
            "x" * 100,
            state,
            page_num=1,
            total_pages=10,
            toc=[],
            pdf_document=pdf,
            strategy=strategy,
            preserve_next_page=True,
        )

    assert new_state.next_page == 5
    assert 2 in new_state.skipped_parse


def test_retry_skipped_parse_pages_invokes_process():
    strategy = _base_profiles()["economy"]
    state = KnowledgeState(
        knowledge=[],
        next_page=3,
        skipped_blank=[],
        skipped_model=[],
        skipped_parse=[1, 2],
    )
    pdf = MagicMock()
    pdf.__getitem__ = MagicMock(
        side_effect=lambda i: MagicMock(get_text=lambda: "body " * 20)
    )
    config = MagicMock()

    calls: list[int] = []

    def fake_process(
        client, config, page_text, state, page_num, total_pages, toc, pdf_document, strategy, *, preserve_next_page=False
    ):
        calls.append(page_num)
        # 模拟成功清掉 skip
        return KnowledgeState(
            knowledge=state.knowledge
            + [KnowledgeItem(page=page_num + 1, text="ok " * 10)],
            next_page=state.next_page,
            skipped_blank=[],
            skipped_model=[],
            skipped_parse=[p for p in state.skipped_parse if p != page_num + 1],
        )

    with patch("read_books.process_page", side_effect=fake_process):
        out = retry_skipped_parse_pages(
            MagicMock(),
            config,
            state,
            total_pages=5,
            toc=[],
            pdf_document=pdf,
            strategy=strategy,
        )

    assert calls == [0, 1]
    assert out.skipped_parse == []


def test_retry_skipped_parse_pages_accepts_string_pages():
    strategy = _base_profiles()["economy"]
    state = KnowledgeState(
        knowledge=[],
        next_page=3,
        skipped_blank=[],
        skipped_model=[],
        skipped_parse=["1", "2"],
    )
    pdf = MagicMock()
    pdf.__getitem__ = MagicMock(
        side_effect=lambda i: MagicMock(get_text=lambda: "body " * 20)
    )
    calls: list[int] = []

    def fake_process(
        client,
        config,
        page_text,
        state,
        page_num,
        total_pages,
        toc,
        pdf_document,
        strategy,
        *,
        preserve_next_page=False,
    ):
        calls.append(page_num)
        return KnowledgeState(
            knowledge=state.knowledge,
            next_page=state.next_page,
            skipped_blank=[],
            skipped_model=[],
            skipped_parse=[
                p for p in state.skipped_parse if int(p) != page_num + 1
            ],
        )

    with patch("read_books.process_page", side_effect=fake_process):
        retry_skipped_parse_pages(
            MagicMock(),
            MagicMock(),
            state,
            total_pages=5,
            toc=[],
            pdf_document=pdf,
            strategy=strategy,
        )

    assert calls == [0, 1]
