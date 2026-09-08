from fastapi import APIRouter

from app.api.routes import integrations, login, users, utils
from app.modules.accounts.router import router as accounts_router
from app.modules.tenants.router import router as tenants_router

api_router = APIRouter()
api_router.include_router(login.router)
api_router.include_router(users.router)
api_router.include_router(utils.router)
api_router.include_router(integrations.router)
api_router.include_router(tenants_router)
api_router.include_router(accounts_router)
