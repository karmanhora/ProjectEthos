# Project Ethos

A research project: can an AI learn the structure and behaviour of an unfamiliar
digital environment well enough to predict the consequences of actions within it?

Phase 1 is a small, internally consistent project-management environment with a
pure, deterministic domain core. Simulation runs entirely in memory on immutable
state, so a counterfactual can never mutate the environment it was derived from.

## Status

Stage 2 — the world can be driven end to end in memory. No API and no database
connection yet; the migrations are authored and awaiting review.

| Module | Purpose |
|---|---|
| `ethos/runtime.py` | Deeply immutable primitives, canonical serialization, hashing, clock and ID injection |
| `ethos/errors.py` | Error taxonomy, HTTP mapping, retry classification |
| `ethos/config.py` | Process settings from the environment; secrets never logged |
| `ethos/models.py` | Ten entity models, `Environment`, `EnvironmentState`, `ChangeSet`, event and snapshot |
| `ethos/state.py` | State hashing, diffing, delta application, replay, chain verification |
| `ethos/permissions.py` | Roles to capabilities, resolved org-first then project |
| `ethos/actions.py` | Registry of twelve pure handlers; the only path that changes state |
| `ethos/repository.py` | Snapshot-plus-replay protocol and the in-memory implementation |
| `ethos/service.py` | Validate, authorize, apply, diff, record and append, in one commit |

## Immutability

`pydantic`'s `frozen=True` does not protect nested collections: validation coerces
`Mapping[K, V]` fields back to a mutable `dict`. Every domain model inherits
`FrozenModel`, which re-freezes each field in `model_post_init` into a
`FrozenMapping` (backed by `types.MappingProxyType`) or a `tuple`. `model_dump()`
cannot serialize these and is never used; `to_plain()` is the single serializer,
and it is also the only input to state hashing.

## Local setup

```bash
python -m venv .venv
source .venv/Scripts/activate   # Windows Git Bash
pip install -r requirements.txt
pytest tests/unit -v
```

No database or credentials are required for the unit tests.

## Integrity model

Three independent checks, each answering a different question:

1. **Digest chain** — was any recorded event altered? Covers every event, including failures, over all metadata.
2. **State replay** — do the recorded transitions reproduce the recorded hashes?
3. **Reconciliation** — does the reconstruction equal the live state?

A stored head on `environments` (`last_event_sequence`, `last_event_digest`)
detects deletion of trailing events, which state reconciliation alone cannot: a
deleted trailing *failure* leaves state unchanged. The head is stored beside the
log and is deliberately **not** part of the hashed state: every event records the
hash of the state it produced, so a head inside that state would make each event
digest depend on itself and no commit could ever be computed.

This is tamper-evidence against partial modification, not tamper-proofness. An
administrator who rewrites the history and recomputes the head is undetectable
without an external anchor, which is deferred.
