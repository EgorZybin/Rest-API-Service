from __future__ import annotations

from fastapi import APIRouter

from sherlock_api.api.v1.admin import admin_router
from sherlock_api.api.v1.health import router as health_router
from sherlock_api.api.v1.search import router as search_router
from sherlock_api.api.v1.tasks import router as tasks_router

router = APIRouter()
router.include_router(health_router, tags=["health"])
router.include_router(search_router)
router.include_router(tasks_router)
router.include_router(admin_router)
