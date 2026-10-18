from fastapi import APIRouter

from app.api.v1 import auth, dashboard, imports, prospects, records, research, system

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(dashboard.router)
api_router.include_router(imports.router)
api_router.include_router(records.router)
api_router.include_router(prospects.router)
api_router.include_router(research.router)
api_router.include_router(system.router)
