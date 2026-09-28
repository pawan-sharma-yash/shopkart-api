import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordBearer
from fastapi.security.utils import get_authorization_scheme_param
from jwt import InvalidTokenError
from sqlalchemy.orm import Session

from ..core.security import ALGORITHM, SECRET_KEY
from ..db.session import get_db
from ..models.auth import User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/verify-otp")


def _user_from_token(token: str, db: Session) -> User | None:
    try:
        claims = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if claims.get("type") != "access":
            return None
        user_id = int(claims["sub"])
    except (InvalidTokenError, KeyError, ValueError):
        return None
    user = db.get(User, user_id)
    if not user or not user.is_active:
        return None
    return user


def get_optional_current_user(
    request: Request,
    db: Session = Depends(get_db),
) -> User | None:
    """Return the current user when a valid Bearer token is supplied.

    Returns None for anonymous / missing / invalid tokens so that
    public endpoints (e.g. product listing) stay open for everyone.
    """
    authorization = request.headers.get("Authorization")
    if not authorization:
        return None
    scheme, token = get_authorization_scheme_param(authorization)
    if not token or scheme.lower() != "bearer":
        return None
    return _user_from_token(token, db)


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    error = HTTPException(status_code=401, detail="Invalid access token", headers={"WWW-Authenticate": "Bearer"})
    try:
        claims = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if claims.get("type") != "access":
            raise error
        user_id = int(claims["sub"])
    except (InvalidTokenError, KeyError, ValueError):
        raise error
    user = db.get(User, user_id)
    if not user or not user.is_active:
        raise error
    return user
