from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, File, Header, Query, UploadFile
from fastapi.responses import JSONResponse

from app.api.deps import AppSettings, CurrentUser, DbSession
from app.core.errors import AppError
from app.domain.states import ImportRowStatus
from app.models import ProspectImport, ProspectImportRow
from app.schemas.common import ERROR_RESPONSES, Envelope, PageMeta
from app.schemas.prospects import ImportDetailOut, ImportOut, ImportRowOut, MappingIn, TargetOption
from app.services import idempotency
from app.services.imports import service
from app.services.imports.csv_parser import CsvError
from app.services.imports.mapping import TARGET_LABELS

router = APIRouter(prefix="/imports", tags=["imports"], responses=ERROR_RESPONSES)

_ALLOWED_EXTENSIONS = (".csv", ".tsv", ".txt")
_TARGETS = [TargetOption(value=t.value, label=label) for t, label in TARGET_LABELS.items()]


class UploadRejected(AppError):
    status_code = 422
    code = "upload_rejected"


def _detail(db: DbSession, imp: ProspectImport) -> ImportDetailOut:
    samples = db.query(ProspectImportRow.raw).filter(ProspectImportRow.import_id == imp.id) \
        .order_by(ProspectImportRow.row_number).limit(5).all()
    return ImportDetailOut(**ImportOut.model_validate(imp).model_dump(), targets=_TARGETS,
                           sample_rows=[s[0] for s in samples])


@router.post("", status_code=201, response_model=Envelope[ImportDetailOut], summary="Upload a CSV for import")
def upload(
    user: CurrentUser,
    db: DbSession,
    settings: AppSettings,
    file: Annotated[UploadFile, File(description="CSV (UTF-8 preferred; Windows-1252 accepted)")],
    idempotency_key: Annotated[str | None, Header(alias=idempotency.HEADER)] = None,
) -> JSONResponse:
    """Parses and stores every row verbatim, then suggests a column mapping. Nothing is imported yet."""
    filename = (file.filename or "upload.csv").replace("\\", "/").rsplit("/", 1)[-1]
    if filename.lower().endswith((".xlsx", ".xls", ".numbers", ".ods")):
        raise UploadRejected("Spreadsheet files aren't supported. Save the sheet as CSV (UTF-8) and upload that.")
    if not filename.lower().endswith(_ALLOWED_EXTENSIONS):
        raise UploadRejected("Upload a .csv file.")
    max_bytes = settings.import_max_file_mb * 1024 * 1024
    data = file.file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise UploadRejected(f"The file is larger than {settings.import_max_file_mb} MB.")

    def action() -> tuple[int, Envelope[ImportDetailOut]]:
        try:
            imp = service.create_import(db, user, filename=filename, data=data, settings=settings)
        except CsvError as exc:
            raise UploadRejected(str(exc)) from exc
        return 201, Envelope(data=_detail(db, imp))

    return idempotency.run_idempotent(db, user=user, key=idempotency_key, scope="POST /imports",
                                      fingerprint=idempotency.request_hash(filename, data), action=action)


@router.get("", response_model=Envelope[list[ImportOut]], summary="Recent imports")
def list_imports(user: CurrentUser, db: DbSession) -> Envelope[list[ImportOut]]:
    return Envelope(data=[ImportOut.model_validate(i) for i in service.list_imports(db, user)])


@router.get("/{import_id}", response_model=Envelope[ImportDetailOut], summary="Import status, mapping and summary")
def get_import(import_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Envelope[ImportDetailOut]:
    return Envelope(data=_detail(db, service.get_import(db, user, import_id)))


@router.put("/{import_id}/mapping", response_model=Envelope[ImportDetailOut],
            summary="Apply a column mapping and validate every row")
def apply_mapping(import_id: uuid.UUID, body: MappingIn, user: CurrentUser, db: DbSession) -> Envelope[ImportDetailOut]:
    imp = service.apply_mapping(db, user, import_id, body.mapping)
    db.commit()
    return Envelope(data=_detail(db, imp))


@router.get("/{import_id}/rows", response_model=Envelope[list[ImportRowOut]], summary="Row-level preview")
def list_rows(
    import_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    status: ImportRowStatus | None = None,
    with_issues: bool = False,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Envelope[list[ImportRowOut]]:
    rows, total = service.list_rows(db, user, import_id, status=status, with_issues=with_issues,
                                    page=page, page_size=page_size)
    return Envelope(data=[ImportRowOut.model_validate(r) for r in rows],
                    meta=PageMeta(page=page, page_size=page_size, total=total).model_dump())


@router.post("/{import_id}/commit", response_model=Envelope[ImportDetailOut],
             summary="Create prospects and companies from valid rows (idempotent)")
def commit(import_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Envelope[ImportDetailOut]:
    imp = service.commit_import(db, user, import_id)
    db.commit()
    return Envelope(data=_detail(db, imp))


@router.post("/{import_id}/cancel", response_model=Envelope[ImportDetailOut], summary="Discard an import")
def cancel(import_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Envelope[ImportDetailOut]:
    imp = service.cancel_import(db, user, import_id)
    db.commit()
    return Envelope(data=_detail(db, imp))
