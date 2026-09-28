from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ....db.session import get_db
from ....models.auth import User
from ....models.product import Favorite, Product, ProductRating
from ....schemas.product import (
    ProductCreate,
    ProductListItem,
    ProductListResponse,
    ProductRead,
    ProductUpdate,
)
from ...deps import get_current_user, get_optional_current_user

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


@router.post("", response_model=ProductRead, status_code=status.HTTP_201_CREATED)
def create_product(
    payload: ProductCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Insert a new product.

    Requires authentication (unlike the open listing). The new product
    has no ratings yet and is not favorited, so `average_rating` is 0.0
    and `is_favorite` is False in the response.
    """
    product = Product(
        name=payload.name.strip(),
        description=payload.description,
        price=payload.price,
        old_price=payload.old_price,
        image_url=payload.image_url,
        is_active=payload.is_active,
    )
    db.add(product)
    db.commit()
    db.refresh(product)

    return ProductRead(
        id=product.id,
        name=product.name,
        description=product.description,
        price=float(product.price),
        old_price=float(product.old_price) if product.old_price is not None else None,
        image=product.image_url,
        is_active=product.is_active,
    )


@router.patch("/{product_id}", response_model=ProductRead)
def update_product(
    product_id: int,
    payload: ProductUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Edit product details (partial update).

    Requires authentication. Only the fields sent in the request are
    changed; omitted fields keep their current values. Send
    `old_price: null` explicitly to remove a discount. Returns 404 when
    the product does not exist.
    """
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")

    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=422, detail="No fields to update")

    for field in ("name", "price", "is_active"):
        if field in updates and updates[field] is None:
            raise HTTPException(status_code=422, detail=f"{field} cannot be null")

    if "name" in updates:
        product.name = updates["name"].strip()
    if "description" in updates:
        product.description = updates["description"]
    if "price" in updates:
        product.price = updates["price"]
    if "old_price" in updates:
        product.old_price = updates["old_price"]
    if "image_url" in updates:
        product.image_url = updates["image_url"]
    if "is_active" in updates:
        product.is_active = updates["is_active"]

    db.commit()
    db.refresh(product)

    return ProductRead(
        id=product.id,
        name=product.name,
        description=product.description,
        price=float(product.price),
        old_price=float(product.old_price) if product.old_price is not None else None,
        image=product.image_url,
        is_active=product.is_active,
    )


@router.get("/{product_id}", response_model=ProductRead)
def get_product(
    product_id: int,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_current_user),
):
    """Fetch a single product by its ID.

    Open for everyone (no auth required), like the listing. Returns the
    full detail including description, average rating and the per-user
    favorite flag (`is_favorite` is True only with a valid access token
    from a user who favorited it). Returns 404 when the ID does not exist.
    """
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")

    average_rating = db.scalar(
        select(func.coalesce(func.avg(ProductRating.rating), 0.0)).where(
            ProductRating.product_id == product.id
        )
    ) or 0.0

    is_favorite = False
    if current_user is not None:
        is_favorite = (
            db.scalar(
                select(Favorite.id).where(
                    Favorite.user_id == current_user.id,
                    Favorite.product_id == product.id,
                )
            )
            is not None
        )

    return ProductRead(
        id=product.id,
        name=product.name,
        description=product.description,
        price=float(product.price),
        old_price=float(product.old_price) if product.old_price is not None else None,
        image=product.image_url,
        is_active=product.is_active,
        average_rating=round(float(average_rating), 1),
        is_favorite=is_favorite,
    )
