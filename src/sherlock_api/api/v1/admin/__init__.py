from fastapi import APIRouter

from sherlock_api.api.v1.admin.accounts import router as accounts_router

admin_router = APIRouter(prefix="/admin")
admin_router.include_router(accounts_router, tags=["admin-accounts"])

__all__ = ["admin_router"]
