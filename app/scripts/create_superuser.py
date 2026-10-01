"""Create (or promote) a superuser.

Usage:
    uv run python -m app.scripts.create_superuser --email admin@example.com --password 'S3cret!pass'
"""

import argparse
import asyncio
import getpass

from app.db.session import SessionLocal, engine
from app.schemas.user import UserCreate
from app.services import user_service


async def _main(email: str, password: str, full_name: str | None) -> None:
    data = UserCreate(email=email, password=password, full_name=full_name)
    async with SessionLocal() as db:
        existing = await user_service.get_by_email(db, data.email)
        if existing is not None:
            existing.is_superuser = True
            await db.commit()
            print(f"Promoted existing user {existing.email} to superuser.")
        else:
            user = await user_service.create_user(db, data, is_superuser=True)
            print(f"Created superuser {user.email} (id={user.id}).")
    await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Create or promote a superuser.")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", help="Prompted for if omitted.")
    parser.add_argument("--full-name", default=None)
    args = parser.parse_args()

    password = args.password or getpass.getpass("Password: ")
    asyncio.run(_main(args.email, password, args.full_name))


if __name__ == "__main__":
    main()
