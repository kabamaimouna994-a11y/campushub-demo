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
    async with engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text("ALTER TYPE userrole ADD VALUE IF NOT EXISTS 'MENTOR'"))
        # Toutes les valeurs possibles de l'enum SkillCategory (models/skill.py)
        for value in ["TECH", "DATA", "DESIGN", "BUSINESS", "LANGUAGE", "SOFT", "OTHER"]:
            await conn.execute(text(f"ALTER TYPE skillcategory ADD VALUE IF NOT EXISTS '{value}'"))
    print("OK : enums userrole et skillcategory mis à jour (valeurs ajoutées si manquantes).")


if __name__ == "__main__":
    asyncio.run(fix())