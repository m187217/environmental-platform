"""FastAPI routes: register, login, refresh, profile."""
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import EmailStr

from src.user.models import UserCreate, UserLogin, UserResponse, TokenPair, TokenRefresh
from src.user.auth import hash_password, verify_password, create_access_token, create_refresh_token, decode_token
from src.user.dependencies import get_current_user
from src.user.db import UserDB, user_db

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(data: UserCreate):
    """Register a new user."""
    existing = await user_db.get_by_email(data.email)
    if existing:
        raise HTTPException(status_code=409, detail="Email already registered")

    user = await user_db.create(
        email=data.email,
        hashed_password=hash_password(data.password),
        phone=data.phone,
        enterprise_name=data.enterprise_name,
    )
    return UserResponse(
        id=user["id"],
        email=user["email"],
        phone=user.get("phone"),
        enterprise_name=user.get("enterprise_name"),
        is_verified=False,
        created_at=user["created_at"],
    )


@router.post("/login", response_model=TokenPair)
async def login(data: UserLogin):
    """Login and get tokens."""
    user = await user_db.get_by_email(data.email)
    if not user or not verify_password(data.password, user["hashed_password"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    return TokenPair(
        access_token=create_access_token(user["id"]),
        refresh_token=create_refresh_token(user["id"]),
    )


@router.post("/refresh", response_model=TokenPair)
async def refresh(data: TokenRefresh):
    """Refresh access token using refresh token."""
    try:
        from jose import jwt as jose_jwt
        import os
        payload = jose_jwt.decode(
            data.refresh_token,
            os.getenv("JWT_SECRET", "dev-secret-change-in-production"),
            algorithms=["HS256"],
        )
        if payload.get("type") != "refresh":
            raise HTTPException(status_code=401, detail="Invalid token type")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    return TokenPair(
        access_token=create_access_token(payload["sub"]),
        refresh_token=create_refresh_token(payload["sub"]),
    )


@router.get("/me", response_model=UserResponse)
async def me(user=Depends(get_current_user)):
    """Get current user profile."""
    return UserResponse(
        id=user["id"],
        email=user["email"],
        phone=user.get("phone"),
        enterprise_name=user.get("enterprise_name"),
        is_verified=user.get("is_verified", False),
        created_at=user["created_at"],
    )


@router.post("/logout")
async def logout(user=Depends(get_current_user)):
    """Logout (client discards token)."""
    return {"message": "Logged out. Discard tokens on client."}
