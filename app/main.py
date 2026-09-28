from dotenv import load_dotenv
from fastapi import FastAPI

from . import models
from .api.v1.api import api_router
from .db.base import Base
from .db.session import engine

load_dotenv()

# models is imported for its side effect: registering SQLAlchemy tables with
# Base.metadata before the tables are created.
assert models is not None

Base.metadata.create_all(bind=engine)

app = FastAPI(title="ShopKart Authentication API", version="1.0.0")
app.include_router(api_router)
