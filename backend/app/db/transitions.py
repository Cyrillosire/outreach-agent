"""Race-safe persisted state transitions.

`transition_row` performs `UPDATE ... SET state = :target WHERE id = :id AND state IN (:allowed)`
in a single statement. If a concurrent writer (e.g. reply ingestion) has already moved the row,
zero rows match and the caller receives `StaleTransition` instead of silently overwriting.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.domain.states import InvalidTransition, StateMachine


class StaleTransition(Exception):
    """The row was not in an allowed source state when the update ran (or does not exist)."""


def transition_row[S: StrEnum](
    session: Session,
    model: Any,
    row_id: uuid.UUID,
    machine: StateMachine[S],
    target: S,
    *,
    column: str = "state",
    expected_from: S | None = None,
    by_human: bool = False,
    extra_values: dict[str, Any] | None = None,
) -> None:
    if expected_from is not None:
        machine.assert_transition(expected_from, target, by_human=by_human)
        allowed = frozenset({expected_from})
    else:
        allowed = machine.predecessors(target, by_human=by_human)
        if not allowed:
            raise InvalidTransition(machine.name, "*", target.value)

    col = getattr(model, column)
    stmt = (
        update(model)
        .where(model.id == row_id, col.in_([s.value for s in allowed]))
        .values({column: target, **(extra_values or {})})
        .execution_options(synchronize_session=False)
    )
    result = session.execute(stmt)
    if result.rowcount != 1:  # type: ignore[attr-defined]
        raise StaleTransition(
            f"{machine.name}: {model.__tablename__}({row_id}) not in {sorted(s.value for s in allowed)}"
        )
