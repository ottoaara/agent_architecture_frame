import pytest
from pydantic import ValidationError

from agent_factory.contracts import Blueprint, FieldSpec, InputRef, build_model

from helpers import blueprint, task


def test_input_ref_parsing():
    ref = InputRef(name="x", source="task:research.summary")
    assert (ref.kind, ref.target, ref.field) == ("task", "research", "summary")
    run = InputRef(name="doc", source="run:document")
    assert (run.kind, run.target, run.field) == ("run", "document", None)


@pytest.mark.parametrize("bad", ["research", "task:", "run:Bad", "task:a.b.c", "file:/etc/passwd"])
def test_input_ref_rejects_bad_sources(bad):
    with pytest.raises(ValidationError):
        InputRef(name="x", source=bad)


def test_build_model_enforces_required_and_types():
    model = build_model(
        "Out",
        [FieldSpec(name="title"), FieldSpec(name="count", type="integer"),
         FieldSpec(name="tags", type="string_list", required=False)],
    )
    ok = model.model_validate({"title": "t", "count": 3})
    assert ok.tags is None
    with pytest.raises(ValidationError):
        model.model_validate({"count": 3})
    with pytest.raises(ValidationError):
        model.model_validate({"title": "t", "count": 1, "extra": 1})


def test_blueprint_round_trip_and_hash_stability():
    bp = blueprint([task("a"), task("b", ["a"])])
    again = Blueprint.model_validate_json(bp.model_dump_json())
    assert again == bp
    assert again.content_hash() == bp.content_hash()
    bumped = again.model_copy(update={"version": 2, "blueprint_id": "bp_other"})
    assert bumped.content_hash() == bp.content_hash()
    changed = again.model_copy(update={"objective": "different"})
    assert changed.content_hash() != bp.content_hash()


def test_task_ids_must_be_identifiers():
    with pytest.raises(ValidationError):
        task("Bad-Id")
