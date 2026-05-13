"""One-off seed: create the first superadmin user. Run manually: `python -m app.core.seed`."""

from __future__ import annotations

import asyncio
import os

from fastapi_users.password import PasswordHelper
from pwdlib import PasswordHash
from pwdlib.hashers.bcrypt import BcryptHasher
from sqlalchemy import select

from app.config import settings
from app.models.db import AsyncSessionLocal
from app.models.tables import User


async def seed_superadmin() -> None:
    email = os.environ.get("SUPERADMIN_EMAIL", "superadmin@al-mawsuat.local")
    password = os.environ.get("SUPERADMIN_PASSWORD")
    if not password:
        raise SystemExit(
            "Set SUPERADMIN_PASSWORD in the environment before running this script."
        )

    helper = PasswordHelper(PasswordHash((BcryptHasher(),)))
    hashed = helper.hash(password)

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.email == email))
        if result.scalar_one_or_none() is not None:
            print("Superadmin email already exists; nothing to do.")
            return

        session.add(
            User(
                email=email,
                hashed_password=hashed,
                tenant_id=settings.DEFAULT_TENANT_ID,
                role="superadmin",
                is_superuser=True,
                is_verified=True,
                is_active=True,
            )
        )
        await session.commit()
        print(f"Created superadmin user: {email}")


def main() -> None:
    asyncio.run(seed_superadmin())


if __name__ == "__main__":
    main()
