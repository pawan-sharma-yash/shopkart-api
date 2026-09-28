from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ....db.session import get_db
from ....models.auth import User
from ....models.product import Favorite, Product, ProductRating
from ....schemas.product import ProductListItem, ProductListResponse
from ...deps import get_optional_current_user

router = APIRouter(prefix="/products", tags=["products"])


@router.get("", response_model=ProductListResponse)
def list_products(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_current_user),
):
    """Public product listing.

    Open for everyone (no auth required). Returns image, name, current
    price (`price`), pre-discount price (`old_price`, null when there is
    no discount), average rating and per-user favorite flag.
    `is_favorite` is True only when the request carries a valid access
    token for a user who favorited the product; otherwise False.
    """
    total = db.scalar(
        select(func.count()).select_from(Product).where(Product.is_active.is_(True))
    ) or 0

    products = db.scalars(
        select(Product)
        .where(Product.is_active.is_(True))
        .order_by(Product.id)
        .offset(skip)
        .limit(limit)
    ).all()

    if not products:
        return ProductListResponse(total=total, skip=skip, limit=limit, items=[])

    product_ids = [p.id for p in products]

    avg_by_product: dict[int, float] = {
        row[0]: float(row[1] or 0.0)
        for row in db.execute(
            select(
                ProductRating.product_id,
                func.coalesce(func.avg(ProductRating.rating), 0.0),
            )
            .where(ProductRating.product_id.in_(product_ids))
            .group_by(ProductRating.product_id)
        ).all()
    }

    favorite_ids: set[int] = set()
    if current_user is not None:
        favorite_ids = set(
            db.scalars(
                select(Favorite.product_id).where(
                    Favorite.user_id == current_user.id,
                    Favorite.product_id.in_(product_ids),
                )
            ).all()
        )

    items = [
        ProductListItem(
            id=product.id,
            name=product.name,
            price=float(product.price),
            old_price=float(product.old_price) if product.old_price is not None else None,
            image=product.image_url,
            average_rating=round(avg_by_product.get(product.id, 0.0), 1),
            is_favorite=product.id in favorite_ids,
        )
        for product in products
    ]

    return ProductListResponse(total=total, skip=skip, limit=limit, items=items)
