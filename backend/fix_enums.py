"""
Script ponctuel : ajoute toutes les valeurs d'enum PostgreSQL manquantes
après les migrations Alembic (les migrations initiales ne couvrent pas
toujours toutes les valeurs définies dans les modèles SQLAlchemy).

Usage (depuis la console Railway, dans /app) :
    python fix_enums.py
"""
import asyncio
from sqlalchemy import text
from core.database import engine


# Nom de l'enum PostgreSQL -> valeurs possibles (noms des membres Python, en majuscules)
ENUM_VALUES = {
    "userrole": ["STUDENT", "MENTOR", "CLUB_ADMIN", "ADMIN"],
    "yearlevel": ["B1", "B2", "B3", "M1", "M2"],
    "skillcategory": ["TECH", "DATA", "DESIGN", "BUSINESS", "LANGUAGE", "SOFT", "OTHER"],
    "skilllevel": ["BEGINNER", "INTERMEDIATE", "ADVANCED", "EXPERT"],
    "projectstatus": ["DRAFT", "OPEN", "IN_PROGRESS", "COMPLETED", "CANCELLED"],
    "projecttype": ["TECH", "DATA", "DESIGN", "BUSINESS", "RESEARCH", "OPEN_INNOVATION"],
    "applicationstatus": ["PENDING", "ACCEPTED", "REJECTED"],
}


async def fix():
    async with engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        for enum_name, values in ENUM_VALUES.items():
            for value in values:
                try:
                    await conn.execute(text(f"ALTER TYPE {enum_name} ADD VALUE IF NOT EXISTS '{value}'"))
                except Exception as e:
                    # L'enum n'existe peut-être pas sous ce nom -> on l'ignore et on continue
                    print(f"  ! {enum_name}.{value} : {e}")
    print("OK : valeurs d'enums ajoutées (ou déjà présentes).")


if __name__ == "__main__":
    asyncio.run(fix())