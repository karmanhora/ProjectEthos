import copy
from datetime import date, datetime, timedelta, timezone
from types import MappingProxyType
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from ethos.runtime import (
    DeterministicIdFactory,
    FixedClock,
    FrozenMapping,
    FrozenModel,
    canonical_json,
    freeze,
    sha256_hex,
    to_plain,
)


class Child(FrozenModel):
    id: UUID
    name: str


class Parent(FrozenModel):
    version: int
    children: dict[UUID, Child]
    tags: list[str] = []
    metadata: dict[str, dict[str, int]] = {}
    note: str | None = None


def build() -> Parent:
    kid = uuid4()
    return Parent(version=1, children={kid: Child(id=kid, name="alpha")}, tags=["x"])


# --- Pydantic integration ---------------------------------------------------


def test_frozen_model_blocks_attribute_assignment():
    p = build()
    with pytest.raises(ValidationError):
        p.version = 2


def test_nested_mapping_is_frozen():
    p = build()
    assert isinstance(p.children, FrozenMapping)
    key = next(iter(p.children))
    with pytest.raises((TypeError, AttributeError)):
        p.children[key] = None


def test_nested_model_is_frozen():
    p = build()
    key = next(iter(p.children))
    with pytest.raises(ValidationError):
        p.children[key].name = "beta"


def test_list_field_becomes_tuple():
    p = build()
    assert p.tags == ("x",)
    with pytest.raises(AttributeError):
        p.tags.append("y")


def test_deepcopy_preserves_frozen_type():
    p = build()
    assert isinstance(copy.deepcopy(p).children, FrozenMapping)
    assert isinstance(p.model_copy(deep=True).children, FrozenMapping)


def test_unknown_fields_rejected():
    with pytest.raises(ValidationError):
        Parent(version=1, children={}, surprise=True)


# --- FrozenMapping internals ------------------------------------------------


def test_mapping_item_assignment_blocked():
    fm = FrozenMapping({"a": 1})
    with pytest.raises((TypeError, AttributeError)):
        fm["a"] = 2


def test_internal_storage_is_read_only():
    fm = FrozenMapping({"a": 1})
    assert isinstance(fm._proxy, MappingProxyType)
    with pytest.raises(TypeError):
        fm._proxy["a"] = 2


def test_internal_storage_attribute_cannot_be_reassigned():
    fm = FrozenMapping({"a": 1})
    with pytest.raises(TypeError):
        fm._proxy = MappingProxyType({})


def test_source_dict_mutation_does_not_affect_frozen_mapping():
    source = {"a": 1}
    fm = FrozenMapping(source)
    source["a"] = 99
    source["b"] = 2
    assert fm["a"] == 1
    assert "b" not in fm


def test_nested_mutable_dict_cannot_be_modified_through_frozen_mapping():
    fm = FrozenMapping({"outer": {"inner": {"a": 1}}})
    assert isinstance(fm["outer"], FrozenMapping)
    assert isinstance(fm["outer"]["inner"], FrozenMapping)
    with pytest.raises(TypeError):
        fm["outer"]["inner"]["a"] = 2
    with pytest.raises(TypeError):
        fm["outer"]["inner"]._proxy["a"] = 2


def test_nested_mutable_dict_cannot_be_modified_through_model():
    p = Parent(version=1, children={}, metadata={"a": {"b": 1}})
    assert isinstance(p.metadata, FrozenMapping)
    assert isinstance(p.metadata["a"], FrozenMapping)
    with pytest.raises(TypeError):
        p.metadata["a"]["b"] = 2


def test_freeze_is_recursive():
    frozen = freeze({"a": {"b": [{"c": 1}]}, "d": (1, 2), "e": {3, 4}})
    assert isinstance(frozen, FrozenMapping)
    assert isinstance(frozen["a"], FrozenMapping)
    assert isinstance(frozen["a"]["b"], tuple)
    assert isinstance(frozen["a"]["b"][0], FrozenMapping)
    assert frozen["d"] == (1, 2)
    assert isinstance(frozen["e"], frozenset)


# --- Serialization ----------------------------------------------------------


def test_to_plain_is_detached():
    p = build()
    plain = to_plain(p)
    before = sha256_hex(canonical_json(p))
    plain["version"] = 999
    plain["tags"].append("mutated")
    assert p.version == 1
    assert sha256_hex(canonical_json(p)) == before


def test_canonical_json_ignores_insertion_order():
    a, b = uuid4(), uuid4()
    left = Parent(version=1, children={a: Child(id=a, name="a"), b: Child(id=b, name="b")})
    right = Parent(version=1, children={b: Child(id=b, name="b"), a: Child(id=a, name="a")})
    assert canonical_json(left) == canonical_json(right)


def test_naive_datetime_rejected():
    with pytest.raises(ValueError):
        to_plain(datetime(2026, 11, 11, 12, 0, 0))


def test_datetime_normalised_to_utc_microseconds():
    value = datetime(2026, 11, 11, 12, 0, 0, 123456, tzinfo=timezone.utc)
    assert to_plain(value) == "2026-11-11T12:00:00.123456+00:00"
    offset = datetime(2026, 11, 11, 14, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    assert to_plain(offset) == "2026-11-11T12:00:00.000000+00:00"


def test_uuid_and_date_serialisation():
    u = uuid4()
    assert to_plain(u) == str(u)
    assert to_plain(date(2026, 1, 2)) == "2026-01-02"


# --- Determinism ------------------------------------------------------------


def test_fixed_clock_is_deterministic_and_tz_aware():
    instant = datetime(2026, 11, 11, tzinfo=timezone.utc)
    clock = FixedClock(instant, step=timedelta(seconds=1))
    assert clock.now() == instant
    assert clock.now() == instant + timedelta(seconds=1)
    with pytest.raises(ValueError):
        FixedClock(datetime(2026, 11, 11))


def test_deterministic_id_factory_is_reproducible():
    namespace = uuid4()
    a = DeterministicIdFactory(namespace)
    b = DeterministicIdFactory(namespace)
    assert [a.new_id() for _ in range(5)] == [b.new_id() for _ in range(5)]
    assert isinstance(a.new_id(), UUID)
