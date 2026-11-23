from __future__ import annotations

from pathlib import Path

import pytest

from nanobot.personal_agent.memory_store import (
    EntityType,
    GraphState,
    MemoryStore,
    RelationshipType,
)


def test_explicit_memory_is_durable_searchable_and_provenanced(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.sqlite3")
    memory_id, reply, created = store.remember(
        100, "Uses FEM and MD", project_name="Mechanochemistry", kind="fact"
    )
    assert created and memory_id and "Saved fact" in reply

    reopened = MemoryStore(tmp_path / "memory.sqlite3")
    project_context, records = reopened.project_context("mechanochemistry")
    assert "Uses FEM and MD" in project_context
    assert len(records) == 1
    assert records[0].source_type == "telegram_owner"
    assert records[0].source_id == "100"
    assert reopened.search("FEM MD")[0].memory_id == memory_id


def test_memory_update_id_is_idempotent_and_correction_supersedes_old_fact(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.sqlite3")
    old_id, _, _ = store.remember(101, "Uses density 7.2 g/cm3", project_name="Materials")
    new_id, _, created = store.correct(102, old_id or "", "Uses density 7.8 g/cm3")
    assert created and new_id != old_id
    duplicate_id, _, duplicate_created = store.correct(102, old_id or "", "Different duplicate payload")
    assert not duplicate_created and duplicate_id == new_id
    matches = store.search("density materials")
    assert [record.memory_id for record in matches] == [new_id]
    assert matches[0].content == "Uses density 7.8 g/cm3"
    assert store.get(old_id or "").superseded_by == new_id


def test_forget_removes_content_and_retrieval_context(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.sqlite3")
    memory_id, _, _ = store.remember(103, "Prefers concise summaries", kind="preference")
    store.bind_message(42, 800, (memory_id or "",))
    reply, changed = store.forget(104, memory_id or "")
    assert changed and "Forgot memory" in reply
    assert store.get(memory_id or "") is None
    assert store.memory_for_message(42, 800) == ()
    assert store.search("concise summaries") == ()
    repeated, created = store.forget(104, memory_id or "")
    assert not created and repeated == reply


def test_task_result_reference_stores_task_and_file_refs_without_model_text(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.sqlite3")
    memory_id, reply, created = store.remember_task(
        105, "Mechanochemistry", task_id="task-" + "a" * 32,
        summary="Completed task; final result remains in TaskStore; model text not copied.",
        references=(("file", "inputs/task-a/data.csv", "data.csv"), ("artifact", "exports/task-a/chart.png", "chart.png")),
    )
    assert created and memory_id and "Mechanochemistry" in reply
    assert store.references(memory_id or "") == (
        ("artifact", "exports/task-a/chart.png", "chart.png"),
        ("file", "inputs/task-a/data.csv", "data.csv"),
        ("task", "task-" + "a" * 32, "Saved task result"),
    )
    assert "model text" in (store.get(memory_id or "") or ()).content


def test_memory_rejects_secret_like_content_and_bounds_size(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.sqlite3")
    with pytest.raises(ValueError, match="credentials"):
        store.remember(106, "The API key is abc")
    with pytest.raises(ValueError, match="2000 characters"):
        store.remember(107, "x" * 2001)


def test_project_context_association_is_durable_and_idempotent(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.sqlite3")
    memory_id, _, _ = store.remember(108, "Uses finite elements", project_name="Aster")
    task_id = "task-" + "b" * 32
    store.link_memories_to_task(109, task_id, (memory_id or "",))
    store.link_memories_to_task(109, task_id, (memory_id or "",))
    with store._connect() as connection:
        assert connection.execute("select count(*) from memory_task_context where task_id=?", (task_id,)).fetchone()[0] == 1


def test_graph_migration_preserves_existing_memory_and_backfills_relationships(tmp_path: Path) -> None:
    path = tmp_path / "memory.sqlite3"
    original = MemoryStore(path)
    memory_id, _, _ = original.remember(110, "Uses finite element modeling", project_name="Aster")
    task_id = "task-" + "c" * 32
    original.remember_task(
        111, "Aster", task_id=task_id, summary="Existing saved task reference.",
        references=(("file", f"inputs/{task_id}/mesh.csv", "mesh.csv"),
                    ("artifact", f"exports/{task_id}/plot.png", "plot.png")),
    )
    with original._connect() as connection:
        connection.execute("DELETE FROM graph_relationships")
        connection.execute("DELETE FROM graph_entities")

    migrated = MemoryStore(path)
    assert migrated.get(memory_id or "") is not None
    graph = migrated.graph_search("Aster")
    assert any(entity.entity_type is EntityType.PROJECT for entity in graph.matched)
    assert any(entity.entity_type is EntityType.FILE and entity.label == "mesh.csv" for entity in graph.connected)
    assert any(entity.entity_type is EntityType.ARTIFACT and entity.label == "plot.png" for entity in graph.connected)
    assert RelationshipType.PROJECT_HAS_MEMORY in {edge.relationship_type for edge in graph.relationships}
    assert RelationshipType.TASK_BELONGS_TO_PROJECT in {edge.relationship_type for edge in graph.relationships}
    assert RelationshipType.TASK_USES_FILE in {edge.relationship_type for edge in graph.relationships}
    assert RelationshipType.TASK_PRODUCES_ARTIFACT in {edge.relationship_type for edge in graph.relationships}
    assert graph.source_ids


def test_graph_syncs_continuations_and_bounds_traversal(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.sqlite3")
    parent = "task-" + "d" * 32
    child = "task-" + "e" * 32
    store.sync_task(parent, parent_task_id=None, files=((f"inputs/{parent}/source.csv", "source.csv"),), artifacts=(), created_at="2026-11-18T00:00:00+00:00")
    store.sync_task(child, parent_task_id=parent, files=(), artifacts=((f"exports/{child}/summary.csv", "summary.csv"),), created_at="2026-11-18T00:01:00+00:00")
    store.sync_task(child, parent_task_id=parent, files=(), artifacts=((f"exports/{child}/summary.csv", "summary.csv"),), created_at="2026-11-18T00:01:00+00:00")
    result = store.graph_search("source.csv", max_depth=2, max_nodes=8)
    relationships = {edge.relationship_type for edge in result.relationships}
    assert RelationshipType.TASK_CONTINUES_TASK in relationships
    assert RelationshipType.TASK_PRODUCES_ARTIFACT in relationships
    assert len(result.matched) + len(result.connected) <= 8
    with store._connect() as connection:
        assert connection.execute(
            "select count(*) from graph_relationships where source_entity_id=? and relationship_type=?",
            (f"task:{child}", RelationshipType.TASK_CONTINUES_TASK.value),
        ).fetchone()[0] == 1
        assert connection.execute(
            "select count(*) from graph_relationships where source_entity_id=? and relationship_type=?",
            (f"task:{child}", RelationshipType.TASK_PRODUCES_ARTIFACT.value),
        ).fetchone()[0] == 1
    with pytest.raises(ValueError, match="bounds"):
        store.graph_search("source.csv", max_depth=4)


def test_correction_graph_marks_old_node_superseded_and_forget_removes_it(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.sqlite3")
    old_id, _, _ = store.remember(120, "Uses method FEM-A", project_name="Mechanochemistry")
    new_id, _, _ = store.correct(121, old_id or "", "Uses method FEM-B")
    graph = store.graph_search("FEM-B")
    assert RelationshipType.MEMORY_SUPERSEDES_MEMORY in {edge.relationship_type for edge in graph.relationships}
    old_node = next(entity for entity in graph.connected if entity.external_id == old_id)
    assert old_node.state is GraphState.SUPERSEDED
    assert store.supersession_for(new_id or "") is not None

    store.forget(122, new_id or "")
    graph_after_forget = store.graph_search("FEM-B")
    assert not any(entity.external_id in {old_id, new_id} for entity in (*graph_after_forget.matched, *graph_after_forget.connected))
    assert store.supersession_for(new_id or "") is None
