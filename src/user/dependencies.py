"""FastAPI dependency: extract and validate current user from Bearer token."""
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from src.user.auth import decode_token
from src.user.db import user_db

security = HTTPBearer()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
    """Extract JWT, validate, return user dict or 401."""
    token = credentials.credentials
    payload = decode_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user = await user_db.get_by_id(payload.sub)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    return user
