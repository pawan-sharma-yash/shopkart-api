from pydantic import BaseModel


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
