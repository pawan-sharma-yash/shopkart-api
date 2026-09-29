from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ....db.session import get_db
from ....models.auth import User
from ....models.product import Favorite, Product, ProductRating, ProductVariant
from ....schemas.product import (
    ProductCreate,
    ProductListItem,
    ProductListResponse,
    ProductRead,
    ProductUpdate,
    VariantCreate,
    VariantRead,
)
from ...deps import get_current_user, get_optional_current_user

router = APIRouter(prefix="/products", tags=["products"])


def _variant_to_schema(variant: ProductVariant) -> VariantRead:
    return VariantRead(
        id=variant.id,
        sku=variant.sku,
        options=list(variant.options or []),
        price=float(variant.price),
        old_price=float(variant.old_price) if variant.old_price is not None else None,
        stock=variant.stock,
        images=list(variant.images or []),
        is_active=variant.is_active,
    )


def _to_product_read(
    product: Product,
    rating: float,
    reviews: int,
    is_favorite: bool,
    variants: list[ProductVariant],
) -> ProductRead:
    return ProductRead(
        id=product.id,
        name=product.name,
        description=product.description,
        category=product.category,
        sub_category=product.sub_category,
        brand=product.brand,
        price=float(product.price),
        old_price=float(product.old_price) if product.old_price is not None else None,
        images=list(product.image_urls or []),
        offers=list(product.offers or []),
        variants=[_variant_to_schema(v) for v in variants],
        rating=rating,
        reviews=reviews,
        is_favorite=is_favorite,
        is_active=product.is_active,
    )


def _rating_stats(db: Session, product_ids: list[int]) -> dict[int, tuple[float, int]]:
    """Batched (average rating, review count) per product."""
    return {
        row[0]: (float(row[1] or 0.0), int(row[2]))
        for row in db.execute(
            select(
                ProductRating.product_id,
                func.coalesce(func.avg(ProductRating.rating), 0.0),
                func.count(ProductRating.id),
            )
            .where(ProductRating.product_id.in_(product_ids))
            .group_by(ProductRating.product_id)
        ).all()
    }


@router.get("", response_model=ProductListResponse)
def list_products(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_current_user),
):
    """Public product listing.

    Open for everyone (no auth required). Each item carries images,
    category/sub_category/brand, current price (`price`), pre-discount
    price (`old_price`, null when there is no discount), average `rating`,
    `reviews` count and per-user favorite flag.
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
    stats = _rating_stats(db, product_ids)

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
            images=list(product.image_urls or []),
            category=product.category,
            sub_category=product.sub_category,
            brand=product.brand,
            rating=round(stats.get(product.id, (0.0, 0))[0], 1),
            reviews=stats.get(product.id, (0.0, 0))[1],
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
    """Insert a new product, with optional offers and variants.

    Requires authentication (unlike the open listing). Variant SKUs must
    be unique; a duplicate returns 409. The new product has no ratings
    yet and is not favorited, so `rating` is 0.0, `reviews` is 0 and
    `is_favorite` is False in the response.
    """
    product = Product(
        name=payload.name.strip(),
        description=payload.description,
        category=payload.category,
        sub_category=payload.sub_category,
        brand=payload.brand,
        price=payload.price,
        old_price=payload.old_price,
        image_urls=list(payload.images),
        offers=list(payload.offers),
        is_active=payload.is_active,
        variants=[
            ProductVariant(
                sku=item.sku.strip(),
                options=[dict(option) for option in item.options],
                price=item.price,
                old_price=item.old_price,
                stock=item.stock,
                images=list(item.images),
                is_active=item.is_active,
            )
            for item in payload.variants
        ],
    )
    db.add(product)
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="Variant SKU already exists") from error
    db.refresh(product)

    return _to_product_read(product, 0.0, 0, False, list(product.variants))


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
    `old_price: null` explicitly to remove a discount, `images: []`
    (or null) to remove all images, and `offers: []` (or null) to remove
    all offers. Sending `variants` replaces the entire variant set
    (send `[]` to remove all variants; null is rejected). Returns 404
    when the product does not exist, 409 on duplicate variant SKU.
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
    if "variants" in updates and updates["variants"] is None:
        raise HTTPException(status_code=422, detail="variants cannot be null")

    if "name" in updates:
        product.name = updates["name"].strip()
    if "description" in updates:
        product.description = updates["description"]
    if "category" in updates:
        product.category = updates["category"]
    if "sub_category" in updates:
        product.sub_category = updates["sub_category"]
    if "brand" in updates:
        product.brand = updates["brand"]
    if "price" in updates:
        product.price = updates["price"]
    if "old_price" in updates:
        product.old_price = updates["old_price"]
    if "images" in updates:
        product.image_urls = list(updates["images"] or [])
    if "offers" in updates:
        product.offers = list(updates["offers"] or [])
    if "is_active" in updates:
        product.is_active = updates["is_active"]
    if "variants" in updates:
        # model_dump(exclude_unset=True) drops nested defaults, so
        # re-parse to refill them (input already passed request validation).
        replacements = [VariantCreate(**item) for item in updates["variants"]]
        product.variants = [
            ProductVariant(
                sku=item.sku.strip(),
                options=[dict(option) for option in item.options],
                price=item.price,
                old_price=item.old_price,
                stock=item.stock,
                images=list(item.images),
                is_active=item.is_active,
            )
            for item in replacements
        ]

    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="Variant SKU already exists") from error
    db.refresh(product)

    stats = _rating_stats(db, [product.id])
    rating, reviews = stats.get(product.id, (0.0, 0))
    return _to_product_read(product, round(rating, 1), reviews, False, list(product.variants))


@router.get("/{product_id}", response_model=ProductRead)
def get_product(
    product_id: int,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_current_user),
):
    """Fetch a single product by its ID.

    Open for everyone (no auth required), like the listing. Returns the
    full detail including description, category/sub_category/brand,
    offers, variants, average `rating`, `reviews` count and the per-user
    favorite flag (`is_favorite` is True only with a valid access token
    from a user who favorited it). Returns 404 when the ID does not exist.
    """
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")

    stats = _rating_stats(db, [product.id])
    rating, reviews = stats.get(product.id, (0.0, 0))

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

    variants = db.scalars(
        select(ProductVariant)
        .where(ProductVariant.product_id == product.id)
        .order_by(ProductVariant.id)
    ).all()

    return _to_product_read(
        product, round(rating, 1), reviews, is_favorite, list(variants)
    )
