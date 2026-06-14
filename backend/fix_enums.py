"""
Script ponctuel : ajoute les valeurs d'enum PostgreSQL manquantes
(userrole.MENTOR et skillcategory.OTHER) après les migrations Alembic.

Usage (depuis la console Railway, dans /app) :
    python fix_enums.py
"""
import asyncio
from sqlalchemy import text
from core.database import engine


async def fix():
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TYPE userrole ADD VALUE IF NOT EXISTS 'MENTOR'"))
        await conn.execute(text("ALTER TYPE skillcategory ADD VALUE IF NOT EXISTS 'OTHER'"))
    print("OK : enums userrole.MENTOR et skillcategory.OTHER ajoutés (ou déjà présents).")


if __name__ == "__main__":
    asyncio.run(fix())