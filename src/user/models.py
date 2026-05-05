"""User models and authentication schemas."""
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, EmailStr, Field


class UserCreate(BaseModel):
    """Registration request."""
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    phone: Optional[str] = None
    enterprise_name: Optional[str] = None


class UserLogin(BaseModel):
    """Login request."""
    email: EmailStr
    password: str


class UserResponse(BaseModel):
    """Public user profile."""
    model_config = {"from_attributes": True}
    
    id: str
    email: str
    phone: Optional[str] = None
    enterprise_name: Optional[str] = None
    is_verified: bool = False
    created_at: datetime


class TokenPair(BaseModel):
    """JWT token pair."""
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = 900  # 15 minutes


class TokenRefresh(BaseModel):
    """Refresh token request."""
    refresh_token: str
