from pydantic import BaseModel, Field


class ProductListItem(BaseModel):
    id: int
    name: str
    price: float
    old_price: float | None = None
    image: str | None = None
    average_rating: float = 0.0
    is_favorite: bool = False


class ProductListResponse(BaseModel):
    total: int
    skip: int
    limit: int
    items: list[ProductListItem]


class ProductCreate(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    description: str | None = None
    price: float = Field(ge=0)
    old_price: float | None = Field(default=None, ge=0)
    image_url: str | None = Field(default=None, max_length=500)
    is_active: bool = True


class ProductRead(BaseModel):
    id: int
    name: str
    description: str | None = None
    price: float
    old_price: float | None = None
    image: str | None = None
    is_active: bool = True
    average_rating: float = 0.0
    is_favorite: bool = False


class ProductUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=200)
    description: str | None = None
    price: float | None = Field(default=None, ge=0)
    old_price: float | None = Field(default=None, ge=0)
    image_url: str | None = Field(default=None, max_length=500)
    is_active: bool | None = None
