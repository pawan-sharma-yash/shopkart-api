from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field


class OrderCreate(BaseModel):
    address_id: int = Field(gt=0)


class BuyNowItemCreate(BaseModel):
    parent_id: int = Field(gt=0)
    variant_id: str = Field(min_length=1, max_length=255)
    quantity: int = Field(ge=1, le=100)


class BuyNowOrderCreate(BaseModel):
    address_id: int = Field(gt=0)
    items: list[BuyNowItemCreate] = Field(min_length=1, max_length=100)


class OrderItemRead(BaseModel):
    id: int
    product_id: int | None
    product_name: str
    variant_id: str
    variant: dict[str, Any]
    unit_price: Decimal
    quantity: int
    line_total: Decimal


class OrderRead(BaseModel):
    id: int
    public_order_id: str
    status: str
    total_amount: Decimal
    shipping_address_id: int | None
    shipping_address: str | None
    shipping_pin: str | None
    created_at: datetime
    items: list[OrderItemRead]


class OrderHistoryRead(BaseModel):
    count: int
    page: int
    page_size: int = Field(serialization_alias="pageSize")
    total_pages: int = Field(serialization_alias="totalPages")
    orders: list[OrderRead]

    model_config = {"populate_by_name": True}
