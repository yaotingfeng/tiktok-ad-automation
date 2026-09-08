from fastapi import APIRouter

from app.api.routes import integrations, login, users, utils
from app.modules.accounts.capability_router import router as capability_router
from app.modules.accounts.router import router as accounts_router
from app.modules.builds.api import router as builds_router
from app.modules.materials.router import router as materials_router
from app.modules.providers.router import router as providers_router
from app.modules.strategies.api import router as strategies_router
from app.modules.tenants.router import router as tenants_router

api_router = APIRouter()
api_router.include_router(login.router)
api_router.include_router(users.router)
api_router.include_router(utils.router)
api_router.include_router(integrations.router)
api_router.include_router(tenants_router)
api_router.include_router(accounts_router)
api_router.include_router(capability_router)
api_router.include_router(materials_router)
api_router.include_router(providers_router)
api_router.include_router(strategies_router)
api_router.include_router(builds_router)
