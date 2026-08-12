"""Staff authentication and role-based access control.

DEVELOPMENT MECHANISM. API keys from configuration, compared in constant time.
It is sufficient to enforce the authorisation *boundaries* now — which is the
point, since those boundaries are what the rest of the code depends on — but a
production deployment must replace ``authenticate`` with a real identity
provider (OIDC) without changing ``require_role``.

The boundary that matters most: the AI callbot has no credential at all. It
reaches the database only through the domain services, so no prompt can make it
assume a staff role, because there is no code path that grants one.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from enum import IntEnum
from typing import Annotated

from fastapi import Depends, Request

from app.api.deps import get_app_settings
from app.core.config import Settings
from app.core.errors import AuthenticationError, AuthorizationError
from app.core.logging import get_logger

logger = get_logger(__name__)


class Role(IntEnum):
    """Ordered so that a higher role satisfies a lower requirement."""

    READONLY = 10
    STAFF = 20
    ADMIN = 30


_ROLE_NAMES: dict[str, Role] = {
    "readonly": Role.READONLY,
    "staff": Role.STAFF,
    "admin": Role.ADMIN,
}


@dataclass(frozen=True, slots=True)
class Principal:
    """An authenticated staff user."""

    identifier: str
    role: Role

    def has(self, required: Role) -> bool:
        return self.role >= required


def authenticate(api_key: str | None, settings: Settings) -> Principal | None:
    """Resolve an API key to a principal, or None.

    Comparison is constant-time against every configured key so that response
    timing does not reveal a valid prefix.
    """
    if not api_key:
        return None
    matched: Principal | None = None
    for candidate, role_name in settings.staff_api_key_map.items():
        if secrets.compare_digest(api_key, candidate):
            matched = Principal(identifier=f"key:{candidate[:4]}…", role=_ROLE_NAMES[role_name])
    return matched


async def current_principal(
    request: Request, settings: Annotated[Settings, Depends(get_app_settings)]
) -> Principal:
    principal = authenticate(request.headers.get("X-API-Key"), settings)
    if principal is None:
        logger.warning("auth.failed", path=request.url.path)
        raise AuthenticationError()
    return principal


def require_role(required: Role):
    """Dependency factory enforcing a minimum role."""

    async def _dependency(
        principal: Annotated[Principal, Depends(current_principal)],
    ) -> Principal:
        if not principal.has(required):
            raise AuthorizationError()
        return principal

    return _dependency


RequireStaff = Depends(require_role(Role.STAFF))
RequireAdmin = Depends(require_role(Role.ADMIN))
RequireReadonly = Depends(require_role(Role.READONLY))
