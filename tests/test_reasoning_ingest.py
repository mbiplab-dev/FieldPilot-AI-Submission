"""Blueprint ingest pure functions: filename metadata, chunking and clause extraction.

No Qdrant, no Ollama, no embeddings — `ingest_directory` is deliberately untouched.
"""

from __future__ import annotations

from pathlib import Path

from fieldpilot.reasoning.ingest import (
    MAX_CHARS,
    MIN_CHARS,
    build_chunks,
    chunk_text,
    extract_clause,
    parse_metadata,
)

# --------------------------------------------------------------------------- parse_metadata


def test_parse_metadata_full_convention():
    meta = parse_metadata(Path("/data/riverside__zone-a__structural__rebar cover.pdf"))
    assert meta == {
        "project_id": "riverside",
        "zone": "zone-a",
        "category": "structural",
        "source": "riverside__zone-a__structural__rebar cover.pdf",
    }


def test_parse_metadata_ignores_extra_title_segments():
    meta = parse_metadata(Path("riverside__zone-b__mep__hvac__rev__c.md"))
    assert meta["project_id"] == "riverside"
    assert meta["zone"] == "zone-b"
    assert meta["category"] == "mep"


def test_parse_metadata_treats_project_wide_zones_as_none():
    for token in ("all", "any", "project", ""):
        meta = parse_metadata(Path(f"riverside__{token}__general__spec.md"))
        assert meta["zone"] is None, token
        assert meta["project_id"] == "riverside"


def test_parse_metadata_two_part_fallback():
    meta = parse_metadata(Path("riverside__zone-c.txt"))
    assert meta["project_id"] == "riverside"
    assert meta["zone"] == "zone-c"
    assert meta["category"] == "general"
    assert meta["source"] == "riverside__zone-c.txt"


def test_parse_metadata_single_part_fallback_is_project_wide():
    meta = parse_metadata(Path("/data/blueprints/site notes.md"))
    assert meta == {
        "project_id": "default",
        "zone": None,
        "category": "general",
        "source": "site notes.md",
    }


def test_parse_metadata_empty_project_and_category_get_defaults():
    meta = parse_metadata(Path("__zone-a____title.md"))
    assert meta["project_id"] == "default"
    assert meta["zone"] == "zone-a"
    assert meta["category"] == "general"


# --------------------------------------------------------------------------- chunk_text


P1 = "A" * 60
P2 = "B" * 60


def test_chunk_text_packs_paragraphs_under_the_budget():
    assert chunk_text(f"{P1}\n\n{P2}", max_chars=900) == [f"{P1}\n\n{P2}"]


def test_chunk_text_starts_a_new_chunk_when_the_budget_is_reached():
    assert chunk_text(f"{P1}\n\n{P2}", max_chars=100) == [P1, P2]


def test_chunk_text_splits_on_whitespace_only_blank_lines():
    assert chunk_text(f"{P1}\n   \n{P2}", max_chars=100) == [P1, P2]


def test_chunk_text_splits_an_oversized_paragraph_and_drops_the_short_tail():
    chunks = chunk_text("C" * 250, max_chars=100)
    # 100 + 100 + 50, and the 50-char tail is under MIN_CHARS
    assert chunks == ["C" * 100, "C" * 100]


def test_chunk_text_flushes_the_buffer_before_an_oversized_paragraph():
    chunks = chunk_text(f"{P1}\n\n{'D' * 250}", max_chars=100)
    assert chunks[0] == P1
    assert chunks[1] == "D" * 100
    assert chunks[2] == "D" * 100
    assert len(chunks) == 3


def test_chunk_text_handles_a_paragraph_exactly_at_the_budget():
    # len(para) == max_chars: it fits, but only as a chunk of its own
    assert chunk_text("G" * 100, max_chars=100) == ["G" * 100]
    assert chunk_text(f"{P1}\n\n{'G' * 100}", max_chars=100) == [P1, "G" * 100]


def test_chunk_text_drops_sub_minimum_fragments():
    assert chunk_text("too short") == []
    assert chunk_text("x" * (MIN_CHARS - 1)) == []
    assert chunk_text("x" * MIN_CHARS) == ["x" * MIN_CHARS]


def test_chunk_text_on_empty_or_whitespace_input():
    assert chunk_text("") == []
    assert chunk_text("\n\n   \n\n") == []


def test_chunk_text_collapses_runs_of_spaces_and_tabs():
    body = "Concrete" + " " * 8 + "cover\tshall be " + "x" * 60
    chunk = chunk_text(body)[0]
    assert chunk.startswith("Concrete cover shall be ")
    assert "  " not in chunk
    assert "\t" not in chunk


def test_chunk_text_default_budget_is_respected():
    text = "\n\n".join("E" * 200 for _ in range(10))
    chunks = chunk_text(text)
    assert chunks, "expected at least one chunk"
    assert all(len(c) <= MAX_CHARS for c in chunks)
    assert sum(c.count("E") for c in chunks) == 2000      # no content lost


# --------------------------------------------------------------------------- extract_clause


def test_extract_clause_from_the_word_clause():
    assert extract_clause("Clause 3.4.2 requires 40mm cover.") == "3.4.2"
    assert extract_clause("see clause 12: general provisions") == "12"
    assert extract_clause("CLAUSE 7.1.3.9 applies") == "7.1.3.9"


def test_extract_clause_from_a_section_sign():
    assert extract_clause("§7.1 Formwork shall remain in place.") == "7.1"
    assert extract_clause("§ 4.2.1 Anchorage") == "4.2.1"


def test_extract_clause_from_a_leading_number():
    assert extract_clause("3.4.2 Concrete cover shall be 40mm.") == "3.4.2"
    assert extract_clause("  2.1 Scope of works\n") == "2.1"
    assert extract_clause("Preamble.\n5.6.7 Reinforcement details follow.") == "5.6.7"


def test_extract_clause_returns_none_without_a_clause():
    assert extract_clause("No clause numbers appear in this paragraph at all.") is None
    assert extract_clause("") is None
    # a bare integer at the start of a line is a list marker, not a clause number
    assert extract_clause("12 bags of cement were delivered.") is None


def test_extract_clause_takes_the_first_match():
    assert extract_clause("Clause 1.1 refers to clause 9.9.") == "1.1"


# --------------------------------------------------------------------------- build_chunks


def _para(prefix: str, sentence: str) -> str:
    return prefix + sentence * 10


def test_build_chunks_from_a_markdown_file(tmp_path):
    path = tmp_path / "riverside__zone-a__structural__rebar.md"
    first = _para("Clause 3.4.2 ", "Rebar cover shall be 40mm minimum in foundation pours. ")
    second = _para("§7.1 ", "Formwork must remain in place for 72 hours after the pour. ")
    path.write_text(f"{first}\n\n{second}\n")

    chunks = build_chunks(path)
    assert len(chunks) == 2

    a, b = chunks
    assert a.chunk_id == "riverside__zone-a__structural__rebar.md:0:0"
    assert b.chunk_id == "riverside__zone-a__structural__rebar.md:0:1"
    assert a.project_id == b.project_id == "riverside"
    assert a.zone == b.zone == "zone-a"
    assert a.category == b.category == "structural"
    assert a.source == b.source == path.name
    assert a.page is None and b.page is None
    assert a.clause == "3.4.2"
    assert b.clause == "7.1"
    assert a.text.startswith("Clause 3.4.2 Rebar cover")
    assert "Formwork" in b.text


def test_build_chunks_marks_a_project_wide_document(tmp_path):
    path = tmp_path / "riverside__all__general__site rules.txt"
    path.write_text("Clause 1.2 " + "All personnel shall wear a hard hat on site. " * 3)

    chunks = build_chunks(path)
    assert len(chunks) == 1
    assert chunks[0].zone is None
    assert chunks[0].project_id == "riverside"
    assert chunks[0].category == "general"
    assert chunks[0].clause == "1.2"


def test_build_chunks_without_a_clause_leaves_it_none(tmp_path):
    path = tmp_path / "riverside__zone-a__safety__toolbox.md"
    path.write_text("Keep walkways clear of debris at the end of every shift please. " * 3)
    chunks = build_chunks(path)
    assert len(chunks) == 1
    assert chunks[0].clause is None


def test_build_chunks_on_a_document_with_too_little_text(tmp_path):
    path = tmp_path / "riverside__zone-a__safety__stub.md"
    path.write_text("TBD\n")
    assert build_chunks(path) == []


def test_build_chunks_ids_are_unique_per_chunk(tmp_path):
    path = tmp_path / "riverside__zone-a__structural__long.md"
    path.write_text("\n\n".join("F" * 800 for _ in range(4)))
    chunks = build_chunks(path)
    assert len(chunks) == 4
    assert len({c.chunk_id for c in chunks}) == 4
    assert [c.chunk_id.rsplit(":", 1)[1] for c in chunks] == ["0", "1", "2", "3"]
