"""In-memory user database (replace with SQLAlchemy in production)."""
import uuid
from datetime import datetime, timezone
from typing import Optional, Dict


class UserDB:
    """Simple in-memory DB for development. Replace with SQLAlchemy+PostgreSQL."""

    def __init__(self):
        self._users: Dict[str, dict] = {}
        self._email_index: Dict[str, str] = {}

    async def create(
        self,
        email: str,
        hashed_password: str,
        phone: Optional[str] = None,
        enterprise_name: Optional[str] = None,
    ) -> dict:
        user_id = str(uuid.uuid4())
        user = {
            "id": user_id,
            "email": email,
            "hashed_password": hashed_password,
            "phone": phone,
            "enterprise_name": enterprise_name,
            "is_verified": False,
            "created_at": datetime.now(timezone.utc),
        }
        self._users[user_id] = user
        self._email_index[email] = user_id
        return user

    async def get_by_email(self, email: str) -> Optional[dict]:
        uid = self._email_index.get(email)
        return self._users.get(uid) if uid else None

    async def get_by_id(self, user_id: str) -> Optional[dict]:
        return self._users.get(user_id)


user_db = UserDB()
