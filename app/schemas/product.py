from pydantic import BaseModel, Field


class VariantCreate(BaseModel):
    sku: str = Field(min_length=1, max_length=100)
    options: list[dict[str, str]] = Field(default_factory=list, max_length=10)
    price: float = Field(ge=0)
    old_price: float | None = Field(default=None, ge=0)
    stock: int = Field(default=0, ge=0)
    images: list[str] = Field(default_factory=list, max_length=10)
    is_active: bool = True


class VariantRead(VariantCreate):
    id: int


class ProductListItem(BaseModel):
    id: int
    name: str
    price: float
    old_price: float | None = None
    images: list[str] = Field(default_factory=list)
    category: str | None = None
    sub_category: str | None = None
    brand: str | None = None
    rating: float = 0.0
    reviews: int = 0
    is_favorite: bool = False


class ProductListResponse(BaseModel):
    total: int
    skip: int
    limit: int
    items: list[ProductListItem]


class ProductCreate(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    description: str | None = None
    category: str | None = Field(default=None, max_length=100)
    sub_category: str | None = Field(default=None, max_length=100)
    brand: str | None = Field(default=None, max_length=100)
    price: float = Field(ge=0)
    old_price: float | None = Field(default=None, ge=0)
    images: list[str] = Field(default_factory=list, max_length=10)
    offers: list[str] = Field(default_factory=list, max_length=10)
    variants: list[VariantCreate] = Field(default_factory=list, max_length=50)
    is_active: bool = True


class ProductRead(BaseModel):
    id: int
    name: str
    description: str | None = None
    category: str | None = None
    sub_category: str | None = None
    brand: str | None = None
    price: float
    old_price: float | None = None
    images: list[str] = Field(default_factory=list)
    offers: list[str] = Field(default_factory=list)
    variants: list[VariantRead] = Field(default_factory=list)
    rating: float = 0.0
    reviews: int = 0
    is_favorite: bool = False
    is_active: bool = True


class ProductUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=200)
    description: str | None = None
    category: str | None = Field(default=None, max_length=100)
    sub_category: str | None = Field(default=None, max_length=100)
    brand: str | None = Field(default=None, max_length=100)
    price: float | None = Field(default=None, ge=0)
    old_price: float | None = Field(default=None, ge=0)
    images: list[str] | None = Field(default=None, max_length=10)
    offers: list[str] | None = Field(default=None, max_length=10)
    variants: list[VariantCreate] | None = Field(default=None, max_length=50)
    is_active: bool | None = None
