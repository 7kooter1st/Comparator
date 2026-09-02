from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class AuthUser:
    id: UUID
    username: str
    role: str
    is_active: bool
    session_id: UUID | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"
