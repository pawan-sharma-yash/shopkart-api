import math
from calendar import monthrange
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, selectinload

from ....api.deps import get_current_user
from ....db.session import get_db
from ....models.auth import User
from ....models.address import Address
from ....models.cart import CartItem
from ....models.order import Order, OrderItem
from ....models.product import Product
from ....schemas.order import (
    BuyNowOrderCreate,
    OrderCreate,
    OrderHistoryRead,
    OrderRead,
)
from ....services.product_catalog import product_variants
from ....services.order_ids import generate_public_order_id

router = APIRouter(prefix="/orders", tags=["orders"])
MAX_ORDER_TOTAL = Decimal("9999999999.99")
def _subtract_months(value: datetime, months: int) -> datetime:
    """Subtract calendar months while keeping the resulting date valid."""
    month_index = value.year * 12 + value.month - 1 - months
    year, month_index = divmod(month_index, 12)
    month = month_index + 1
    day = min(value.day, monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _order_payload(order: Order) -> dict[str, Any]:
    return {
        "id": order.id,
        "public_order_id": order.public_order_id,
        "status": order.status,
        "total_amount": order.total_amount,
        "shipping_address_id": order.shipping_address_id,
        "shipping_address": order.shipping_address,
        "shipping_pin": order.shipping_pin,
        "created_at": order.created_at,
        "items": [
            {
                "id": item.id,
                "product_id": item.product_id,
                "product_name": item.product_name,
                "variant_id": item.variant_id,
                "variant": item.variant_snapshot,
                "unit_price": item.unit_price,
                "quantity": item.quantity,
                "line_total": item.line_total,
            }
            for item in order.items
        ],
    }


def _new_public_order_id(db: Session) -> str:
    """Generate an ID not currently present, leaving the unique index as final guard."""
    for _ in range(5):
        candidate = generate_public_order_id()
        if db.scalar(select(Order.id).where(Order.public_order_id == candidate)) is None:
            return candidate
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Could not allocate a unique order identifier; please retry",
    )


def _get_variant(product: Product, variant_id: str) -> tuple[dict, Decimal, int]:
    variant = next(
        (
            entry
            for entry in product_variants(product.id, product.variants)
            if str(entry.get("variantid", "")) == variant_id
        ),
        None,
    )
    if variant is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Variant {variant_id} for product {product.id} is no longer available",
        )
    try:
        price = Decimal(str(variant["price"])).quantize(Decimal("0.01"))
        stock = int(variant.get("stock", 0))
    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Variant {variant_id} for product {product.id} has invalid price or stock",
        )
    if not price.is_finite() or price < 0 or stock < 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Variant {variant_id} for product {product.id} has invalid price or stock",
        )
    return variant, price, stock


@router.post("", response_model=OrderRead, status_code=status.HTTP_201_CREATED)
def place_order(
    payload: OrderCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Create an order from the current cart and a selected saved address."""
    cart_items = db.scalars(
        select(CartItem)
        .where(CartItem.user_id == current_user.id)
        .order_by(CartItem.id)
    ).all()
    if not cart_items:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot place an order with an empty cart",
        )

    requested_items = [
        (item.product_id, item.variant_id, item.quantity) for item in cart_items
    ]
    return _create_order(payload.address_id, requested_items, current_user, db, cart_items)


@router.post(
    "/buy-now",
    response_model=OrderRead,
    status_code=status.HTTP_201_CREATED,
)
def buy_now(
    payload: BuyNowOrderCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Create an order directly from selected product variants, without using the cart."""
    # Combine duplicate product/variant lines before checking stock or calculating totals.
    quantities: dict[tuple[int, str], int] = {}
    for item in payload.items:
        key = (item.parent_id, item.variant_id)
        quantities[key] = quantities.get(key, 0) + item.quantity
    requested_items = [
        (product_id, variant_id, quantity)
        for (product_id, variant_id), quantity in quantities.items()
    ]
    return _create_order(payload.address_id, requested_items, current_user, db)


def _create_order(
    address_id: int,
    requested_items: list[tuple[int, str, int]],
    current_user: User,
    db: Session,
    cart_items: list[CartItem] | None = None,
) -> dict:
    """Validate and persist an order; only cart checkout removes cart rows."""
    saved_address = db.scalar(
        select(Address).where(
            Address.id == address_id,
            Address.user_id == current_user.id,
        )
    )
    if saved_address is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Address not found",
        )

    address = (saved_address.address or "").strip()
    pin = (saved_address.pin or "").strip()
    if not address or len(pin) != 6 or not pin.isdigit():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Selected address must have a delivery address and six-digit PIN",
        )

    product_ids = sorted({product_id for product_id, _, _ in requested_items})
    products = db.scalars(
        select(Product)
        .where(Product.id.in_(product_ids))
        .order_by(Product.id)
        .with_for_update()
    ).all()
    products_by_id = {product.id: product for product in products}

    order = Order(
        public_order_id=_new_public_order_id(db),
        user_id=current_user.id,
        status="placed",
        total_amount=Decimal("0.00"),
        shipping_address_id=saved_address.id,
        shipping_address=address,
        shipping_pin=pin,
    )
    total = Decimal("0.00")
    for product_id, variant_id, quantity in requested_items:
        product = products_by_id.get(product_id)
        if product is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Product {product_id} is no longer available",
            )
        variant, unit_price, stock = _get_variant(product, variant_id)
        if quantity < 1 or quantity > stock:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Requested quantity for {product.name} is no longer available",
            )
        updated_variants = [
            dict(entry) for entry in product_variants(product.id, product.variants)
        ]
        for index, entry in enumerate(updated_variants):
            if str(entry.get("variantid", "")) == variant_id:
                entry["stock"] = stock - quantity
                break
        product.variants = updated_variants
        line_total = unit_price * quantity
        total += line_total
        order.items.append(
            OrderItem(
                product_id=product.id,
                product_name=product.name,
                variant_id=variant_id,
                variant_snapshot=dict(variant),
                unit_price=unit_price,
                quantity=quantity,
                line_total=line_total,
            )
        )

    if not total.is_finite() or total > MAX_ORDER_TOTAL:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Order total exceeds the supported amount",
        )
    order.total_amount = total
    db.add(order)
    if cart_items is not None:
        for cart_item in cart_items:
            db.delete(cart_item)

    try:
        db.commit()
    except (IntegrityError, OperationalError) as error:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Order could not be placed because the cart or catalog changed",
        ) from error

    db.refresh(order)
    return _order_payload(order)


@router.get("", response_model=OrderHistoryRead)
def get_order_history(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100, alias="pageSize"),
    duration: int | None = Query(
        default=None,
        ge=1,
        le=36,
        description="Rolling date window in months (1–36)",
    ),
    year: int | None = Query(default=None, ge=1970, le=9998),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Return the authenticated user's paginated order history with optional date filtering."""
    if duration is not None and year is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Use either duration or year, not both",
        )

    filters = [Order.user_id == current_user.id]
    if duration is not None:
        cutoff = _subtract_months(
            datetime.now(timezone.utc),
            duration,
        )
        filters.append(Order.created_at >= cutoff)
    elif year is not None:
        year_start = datetime(year, 1, 1, tzinfo=timezone.utc)
        next_year_start = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
        filters.extend(
            [Order.created_at >= year_start, Order.created_at < next_year_start]
        )

    count = (
        db.scalar(select(func.count()).select_from(Order).where(*filters)) or 0
    )
    orders = db.scalars(
        select(Order)
        .options(selectinload(Order.items))
        .where(*filters)
        .order_by(Order.created_at.desc(), Order.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "count": count,
        "page": page,
        "page_size": page_size,
        "total_pages": math.ceil(count / page_size) if count else 0,
        "orders": [_order_payload(order) for order in orders],
    }
