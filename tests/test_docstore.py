"""DocStore: schema split/merge, replace-vs-patch semantics and the WHERE vocabulary.

SQLite only — a temp file per test. Postgres shares the same code paths for `split`/`merge`/
`_where_sql`, which is where the interesting behaviour lives.
"""

from __future__ import annotations

import json
import time

import pytest

from fieldpilot.storage import Column, DocStore, TableSpec

DEMO = TableSpec(
    "demo",
    key="doc_id",
    columns=(
        Column("name"),
        Column("owner", indexed=True),
        Column("count", "int"),
        Column("score", "real"),
        Column("flag", "bool", indexed=True),
        Column("created_at", "real"),
    ),
)


@pytest.fixture
async def table(tmp_path):
    store = DocStore("sqlite", str(tmp_path / "nested" / "demo.db"))
    await store.start([DEMO])
    try:
        yield store.table(DEMO)
    finally:
        await store.stop()


# --------------------------------------------------------------------------- spec split/merge


def test_split_folds_undeclared_keys_into_payload_and_coerces_declared():
    row = DEMO.split(
        {
            "doc_id": "d1",
            "name": 42,                 # declared text -> str
            "count": "3",               # declared int -> int
            "score": "1.5",             # declared real -> float
            "flag": 1,                  # declared bool -> 0/1
            "geojson": {"type": "Polygon"},
            "tags": ["a", "b"],
        }
    )
    assert row["doc_id"] == "d1"
    assert row["name"] == "42"
    assert row["count"] == 3 and type(row["count"]) is int
    assert row["score"] == 1.5 and type(row["score"]) is float
    assert row["flag"] == 1
    # every declared column is present even when the record omitted it
    assert row["owner"] is None and row["created_at"] is None
    assert json.loads(row["payload"]) == {"geojson": {"type": "Polygon"}, "tags": ["a", "b"]}
    assert set(row) == set(DEMO.column_names)


def test_split_keeps_none_as_none_for_typed_columns():
    row = DEMO.split({"doc_id": "d1", "count": None, "score": None, "flag": None, "name": None})
    assert row["count"] is None
    assert row["score"] is None
    assert row["flag"] is None
    assert row["name"] is None


def test_split_merges_an_explicit_payload_dict_without_shadowing_top_level():
    row = DEMO.split({"doc_id": "d1", "payload": {"a": 1, "b": 2}, "b": 99})
    assert json.loads(row["payload"]) == {"a": 1, "b": 99}


def test_merge_is_the_inverse_of_split():
    record = {"doc_id": "d1", "name": "n", "flag": True, "count": 2, "extra": {"k": "v"}}
    merged = DEMO.merge(DEMO.split(record))
    assert merged["doc_id"] == "d1"
    assert merged["name"] == "n"
    assert merged["flag"] is True
    assert merged["count"] == 2
    assert merged["extra"] == {"k": "v"}
    assert "payload" not in merged


def test_merge_survives_corrupt_payload_json():
    assert DEMO.merge({"doc_id": "d1", "payload": "{not json"}) == {"doc_id": "d1"}


def test_column_names_order_is_key_then_columns_then_payload():
    assert DEMO.column_names == (
        "doc_id", "name", "owner", "count", "score", "flag", "created_at", "payload",
    )


# --------------------------------------------------------------------------- round-trip


async def test_undeclared_keys_round_trip_through_payload(table):
    await table.put(
        {"doc_id": "d1", "name": "Level 3", "geojson": {"coords": [[1, 2], [3, 4]]},
         "tags": ["rebar"], "nested": {"deep": {"n": 1}}}
    )
    got = await table.get("d1")
    assert got["name"] == "Level 3"
    assert got["geojson"] == {"coords": [[1, 2], [3, 4]]}
    assert got["tags"] == ["rebar"]
    assert got["nested"] == {"deep": {"n": 1}}


async def test_declared_columns_are_type_coerced_on_the_way_in_and_out(table):
    await table.put({"doc_id": "d1", "count": "7", "score": "2.5", "flag": 1})
    got = await table.get("d1")
    assert got["count"] == 7 and type(got["count"]) is int
    assert got["score"] == 2.5 and type(got["score"]) is float
    assert got["flag"] is True

    await table.put({"doc_id": "d2", "flag": 0})
    assert (await table.get("d2"))["flag"] is False

    await table.put({"doc_id": "d3", "flag": "non-empty-string"})
    assert (await table.get("d3"))["flag"] is True


async def test_get_missing_key_returns_none(table):
    assert await table.get("nope") is None


async def test_put_without_primary_key_raises(table):
    with pytest.raises(ValueError, match="missing primary key 'doc_id'"):
        await table.put({"name": "orphan"})


# --------------------------------------------------------------------------- put vs patch


async def test_put_is_a_full_replace(table):
    await table.put({"doc_id": "d1", "name": "a", "owner": "o1", "count": 5, "extra": "keep"})
    await table.put({"doc_id": "d1", "name": "b"})
    got = await table.get("d1")
    assert got["name"] == "b"
    assert got["owner"] is None
    assert got["count"] is None
    assert "extra" not in got
    assert await table.count() == 1          # replaced, not duplicated


async def test_patch_merges_instead_of_replacing(table):
    await table.put({"doc_id": "d1", "name": "a", "owner": "o1", "extra": "keep"})
    updated = await table.patch("d1", {"name": "b", "count": 3, "added": True})
    assert updated["name"] == "b" and updated["count"] == 3
    got = await table.get("d1")
    assert got["name"] == "b"
    assert got["owner"] == "o1"
    assert got["count"] == 3
    assert got["extra"] == "keep"
    assert got["added"] is True


async def test_patch_can_null_a_column(table):
    await table.put({"doc_id": "d1", "owner": "o1", "score": 1.0})
    await table.patch("d1", {"owner": None, "score": None})
    got = await table.get("d1")
    assert got["owner"] is None and got["score"] is None


async def test_patch_missing_row_returns_none_and_creates_nothing(table):
    assert await table.patch("ghost", {"name": "x"}) is None
    assert await table.count() == 0


async def test_created_at_is_immutable_across_reputs(table):
    await table.put({"doc_id": "d1", "name": "a", "created_at": 111.0})
    await table.put({"doc_id": "d1", "name": "b", "created_at": 999.0})
    assert (await table.get("d1"))["created_at"] == 111.0
    # patch cannot move it either
    await table.patch("d1", {"created_at": 777.0})
    assert (await table.get("d1"))["created_at"] == 111.0


async def test_put_defaults_created_at_and_keeps_the_first_one(table):
    before = time.time()
    first = await table.put({"doc_id": "d1", "name": "a"})
    after = time.time()
    assert before <= first["created_at"] <= after
    await table.put({"doc_id": "d1", "name": "b"})
    assert (await table.get("d1"))["created_at"] == first["created_at"]


# --------------------------------------------------------------------------- list / count


@pytest.fixture
async def seeded(table):
    await table.put({"doc_id": "d1", "owner": "o1", "score": 1.0, "flag": 1, "created_at": 10.0})
    await table.put({"doc_id": "d2", "owner": "o2", "score": 2.0, "flag": 0, "created_at": 20.0})
    await table.put({"doc_id": "d3", "owner": "o3", "score": 3.0, "flag": 1, "created_at": 30.0})
    await table.put({"doc_id": "d4", "score": 4.0, "flag": 0, "created_at": 40.0})  # owner NULL
    return table


def _ids(rows):
    return [r["doc_id"] for r in rows]


async def test_list_orders_by_order_by_column(seeded):
    assert _ids(await seeded.list()) == ["d4", "d3", "d2", "d1"]
    assert _ids(await seeded.list(descending=False)) == ["d1", "d2", "d3", "d4"]


async def test_list_respects_limit(seeded):
    assert _ids(await seeded.list(limit=2)) == ["d4", "d3"]


async def test_list_scalar_equality(seeded):
    assert _ids(await seeded.list(where={"owner": "o2"})) == ["d2"]
    assert _ids(await seeded.list(where={"flag": 1})) == ["d3", "d1"]
    assert await seeded.list(where={"owner": "nobody"}) == []


async def test_list_none_valued_filter_is_ignored(seeded):
    # a None filter means "caller did not filter", not "column IS NULL"
    assert len(await seeded.list(where={"owner": None})) == 4


async def test_list_in_operator(seeded):
    assert _ids(await seeded.list(where={"owner": ("in", ["o1", "o3"])})) == ["d3", "d1"]


async def test_list_empty_in_matches_no_rows(seeded):
    assert await seeded.list(where={"owner": ("in", [])}) == []
    assert await seeded.count(where={"owner": ("in", [])}) == 0


async def test_list_isnull_operator(seeded):
    assert _ids(await seeded.list(where={"owner": ("isnull", True)})) == ["d4"]
    assert _ids(await seeded.list(where={"owner": ("isnull", False)})) == ["d3", "d2", "d1"]


async def test_list_gte_operator(seeded):
    assert _ids(await seeded.list(where={"score": ("gte", 3.0)})) == ["d4", "d3"]
    assert await seeded.list(where={"score": ("gte", 99.0)}) == []


async def test_list_combines_clauses_with_and(seeded):
    rows = await seeded.list(where={"flag": 1, "score": ("gte", 2.0)})
    assert _ids(rows) == ["d3"]


async def test_unsupported_filter_op_raises(seeded):
    with pytest.raises(ValueError, match="unsupported filter op"):
        await seeded.list(where={"score": ("lte", 1.0)})


async def test_count(seeded):
    assert await seeded.count() == 4
    assert await seeded.count(where={"flag": 1}) == 2
    assert await seeded.count(where={"owner": ("isnull", True)}) == 1
    assert await seeded.count(where={"score": ("gte", 2.0)}) == 3


# --------------------------------------------------------------------------- delete


async def test_delete_reports_whether_a_row_went_away(table):
    await table.put({"doc_id": "d1", "name": "a"})
    assert await table.delete("d1") is True
    assert await table.delete("d1") is False
    assert await table.get("d1") is None
    assert await table.count() == 0


# --------------------------------------------------------------------------- lifecycle


async def test_start_is_idempotent_and_preserves_rows(tmp_path):
    path = str(tmp_path / "demo.db")
    store = DocStore("sqlite", path)
    await store.start([DEMO])
    await store.table(DEMO).put({"doc_id": "d1", "name": "a"})
    await store.stop()

    reopened = DocStore("sqlite", path)
    await reopened.start([DEMO])            # CREATE TABLE IF NOT EXISTS on an existing file
    try:
        assert (await reopened.table(DEMO).get("d1"))["name"] == "a"
    finally:
        await reopened.stop()


def test_backend_falls_back_to_sqlite_for_a_non_postgres_url():
    assert DocStore("postgres", "data/platform.db").backend == "sqlite"
    assert DocStore("postgres", "postgresql+asyncpg://h/db").backend == "postgres"
    assert DocStore("sqlite", "postgresql://h/db").backend == "sqlite"
