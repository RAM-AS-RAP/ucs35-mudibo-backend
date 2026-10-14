"""Administrator routes, mounted at ``/api/v1/admin``.

Every route requires an admin account (role loaded from the database on each request, password-change
guard enforced). Routes that manage other admins additionally require the super administrator.
"""

from fastapi import APIRouter, Depends

from app.core.dependencies import require_admin
from app.routers import admin_listings, admin_orders, admin_reports, admin_users

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])
router.include_router(admin_users.router)
router.include_router(admin_listings.router)
router.include_router(admin_orders.router)
router.include_router(admin_reports.router)
