"""Real concurrent transactions (separate connections). Data is committed, so the test cleans up after itself."""

from __future__ import annotations

import threading
import uuid

import pytest
from sqlalchemy import Engine, delete, func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import hash_password
from app.domain.states import ImportStatus, UserRole
from app.models import Prospect, ProspectImport, User, Workspace
from app.services.imports import service

pytestmark = pytest.mark.integration


def test_concurrent_commits_serialize_without_duplicates(engine: Engine) -> None:
    rows = "\n".join(f"Person {i},p{i}@race{i % 3}.example" for i in range(30))
    with Session(engine) as db:
        workspace = Workspace(name=f"race-{uuid.uuid4()}")
        db.add(workspace)
        db.commit()
        workspace_id = workspace.id
    try:
        with Session(engine, expire_on_commit=False) as db:
            user = User(workspace_id=workspace_id, email=f"race-{uuid.uuid4()}@example.com", display_name="Race",
                        password_hash=hash_password("correct horse battery staple"), role=UserRole.ADMIN)
            db.add(user)
            db.commit()
            imp = service.create_import(db, user, filename="race.csv", data=f"Name,Email\n{rows}\n".encode(),
                                        settings=get_settings())
            service.apply_mapping(db, user, imp.id, imp.column_mapping)
            db.commit()
            import_id = imp.id

        barrier = threading.Barrier(3)
        results: list[object] = []

        def commit() -> None:
            with Session(engine, expire_on_commit=False) as s:
                u = s.merge(user)
                barrier.wait()
                try:
                    results.append(service.commit_import(s, u, import_id).status)
                    s.commit()
                except Exception as exc:  # recorded and asserted below
                    results.append(exc)

        threads = [threading.Thread(target=commit) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert results == [ImportStatus.COMPLETED] * 3, results
        with Session(engine) as db:
            count = db.scalar(select(func.count()).select_from(Prospect).where(Prospect.workspace_id == workspace_id))
            status = db.scalar(select(ProspectImport.status).where(ProspectImport.id == import_id))
        assert count == 30
        assert status is ImportStatus.COMPLETED
    finally:
        with Session(engine) as db:
            db.execute(delete(Workspace).where(Workspace.id == workspace_id))
            db.commit()
