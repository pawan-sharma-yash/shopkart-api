from fastapi import APIRouter

from .endpoints.auth import router as auth_router
from .endpoints.products import router as products_router

api_router = APIRouter()
api_router.include_router(auth_router)
api_router.include_router(products_router)
