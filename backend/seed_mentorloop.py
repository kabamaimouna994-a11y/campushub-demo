"""
Script de seed pour MentorLoop — Import des réponses aux formulaires
B1 (mentorés) et M1 (mentors) dans la base CampusHub.

Usage (dans le conteneur Docker) :
    docker compose exec api python seed_mentorloop.py

Ce script :
1. Crée un compte User pour chaque mentor M1 et chaque mentoré B1
   (mot de passe par défaut, à changer en prod).
2. Convertit les domaines / besoins en UserSkill.
3. Stocke la disponibilité brute dans le champ `bio` (en attendant un
   futur champ dédié) et le moyen de contact préféré dans `linkedin_url`
   (réutilisé comme champ "contact" générique — TODO si un vrai champ
   contact_method est ajouté au modèle User).
4. Ignore les doublons (même email) pour pouvoir relancer le script
   sans tout dupliquer.
"""

import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import select  # noqa: E402

from core.database import AsyncSessionLocal  # noqa: E402
from core.security import hash_password  # noqa: E402
from models.user import User, UserRole, YearLevel  # noqa: E402
from models.skill import UserSkill, SkillCategory, SkillLevel  # noqa: E402

# Importe tous les autres modèles pour que SQLAlchemy puisse résoudre les
# relations de User (mentorships, project_memberships, etc.) lors de la
# configuration des mappers. Sans ces imports, SQLAlchemy lève
# "NameError: name 'ProjectMember' is not defined" (et similaires) car
# ces classes ne sont pas encore enregistrées dans son registre.
from models import mentorship, project, event, club, certification  # noqa: E402, F401


DEFAULT_PASSWORD = "MentorLoop2026!"


# ---------------------------------------------------------------------------
# Mapping des libellés "domaines" du formulaire -> (nom de compétence normalisé,
# catégorie). Les clés sont volontairement en minuscules pour un matching
# insensible à la casse / aux accents partiels.
# ---------------------------------------------------------------------------
DOMAIN_TO_SKILL = {
    "développement web": ("Développement Web", SkillCategory.TECH),
    "developpement web": ("Développement Web", SkillCategory.TECH),
    "dev": ("Développement Web", SkillCategory.TECH),
    "python": ("Python", SkillCategory.TECH),
    "java": ("Java", SkillCategory.TECH),
    "base de données": ("Base de données", SkillCategory.DATA),
    "base de donnees": ("Base de données", SkillCategory.DATA),
    "ia": ("IA", SkillCategory.DATA),
    "intelligence artificielle": ("IA", SkillCategory.DATA),
    "réseau": ("Réseau", SkillCategory.TECH),
    "reseau": ("Réseau", SkillCategory.TECH),
    "cybersécurité": ("Cybersécurité", SkillCategory.TECH),
    "cybersecurite": ("Cybersécurité", SkillCategory.TECH),
    "cybersecurité": ("Cybersécurité", SkillCategory.TECH),
    "gestion de projet": ("Gestion de projet", SkillCategory.BUSINESS),
    "open innovation": ("Open Innovation", SkillCategory.BUSINESS),
    "infra": ("Infrastructure", SkillCategory.TECH),
    "infrastructure": ("Infrastructure", SkillCategory.TECH),
    "infra / reseau": ("Infrastructure", SkillCategory.TECH),
    "infrastructure / reseau": ("Infrastructure", SkillCategory.TECH),
    "systeme": ("Systèmes", SkillCategory.TECH),
    "système": ("Systèmes", SkillCategory.TECH),
    "reseau / systeme": ("Réseau", SkillCategory.TECH),
    "autres": ("Autres", SkillCategory.OTHER),
    "autre": ("Autres", SkillCategory.OTHER),
}


def split_domains(raw: str) -> list[str]:
    """Découpe une chaîne de domaines séparés par des virgules ou '/'."""
    if not raw or not isinstance(raw, str):
        return []
    raw = raw.strip()
    # Remplace les séparateurs '/' par des virgules pour homogénéiser
    raw = raw.replace(" / ", ",").replace("/", ",")
    parts = [p.strip().lower() for p in raw.split(",")]
    return [p for p in parts if p]


def domain_to_skill(domain: str) -> tuple[str, SkillCategory] | None:
    """Retourne (nom_skill, catégorie) pour un libellé de domaine donné."""
    domain = domain.strip().lower()
    if domain in DOMAIN_TO_SKILL:
        return DOMAIN_TO_SKILL[domain]
    # Recherche approximative : un mot-clé connu est contenu dans le libellé
    for key, value in DOMAIN_TO_SKILL.items():
        if key in domain or domain in key:
            return value
    return None


def split_name(full_name: str) -> tuple[str, str]:
    """Sépare un nom complet en (prénom, nom) au mieux."""
    full_name = " ".join(full_name.strip().split())
    parts = full_name.split(" ")
    if len(parts) == 1:
        return parts[0], parts[0]
    # Heuristique simple : premier mot = prénom, reste = nom
    return parts[0], " ".join(parts[1:])


def clean_email(email: str) -> str:
    return email.strip().lower()


# ---------------------------------------------------------------------------
# Données brutes — Mentors M1 (extraites du formulaire "master")
# ---------------------------------------------------------------------------
M1_MENTORS = [
    {"name": "Kaba Maïmouna", "email": "kabamaimouna994@gmail.com", "specialty": "Infra",
     "domains": "Réseau, Cybersécurité", "availability": "2h/semaine", "multi_student": False,
     "contact": "Discord"},
    {"name": "TSANGA Kelly", "email": "henrykelly.tsanga@ecoles-epsi.net", "specialty": "DEV",
     "domains": "Développement Web, Autres", "availability": "1h/semaine", "multi_student": True,
     "contact": "E-mail"},
    {"name": "Carelle Massemo", "email": "c.massemotchouiague@ecoles-epsi.net", "specialty": "Expert IA",
     "domains": "Développement Web, Python, Base de données, IA, Autres", "availability": "3h/semaine",
     "multi_student": True, "contact": "E-mail"},
    {"name": "Kaké M'mawa", "email": "mmawakake041@gmail.com", "specialty": "Cybersécurité",
     "domains": "Python, Réseau, Cybersécurité", "availability": "2h/semaine", "multi_student": True,
     "contact": "E-mail"},
    {"name": "Djenaba fofana", "email": "Jenaba1952@gmail.com", "specialty": "Cybersecurité",
     "domains": "Python, Réseau, Cybersécurité", "availability": "1h/semaine", "multi_student": False,
     "contact": "Discord"},
    {"name": "Aashifa bagha", "email": "aashifa.bashir321@gmail.com", "specialty": "Data & AI",
     "domains": "Python, Java, Base de données, IA", "availability": "2h/semaine", "multi_student": True,
     "contact": "E-mail"},
    {"name": "Ramata Kamano", "email": "ramatakamano0@gmail.com", "specialty": "Informatique",
     "domains": "Développement Web", "availability": "3h/semaine", "multi_student": True,
     "contact": "Téléphone"},
    {"name": "Diallo Mamadou Lamine", "email": "Mamadou-lamine.diallo@imt-bs.eu", "specialty": "Informatique",
     "domains": "Gestion de projet", "availability": "1h/semaine", "multi_student": False,
     "contact": "E-mail"},
    {"name": "Kaba Kara", "email": "Punisherpkiller@outlook.com", "specialty": "Développeur",
     "domains": "Développement Web, Python, Base de données", "availability": "3h/semaine",
     "multi_student": True, "contact": "Téléphone"},
    {"name": "Ibrahima kalil Kaba", "email": "kalilkaba4@gmail.com", "specialty": "Base de données",
     "domains": "Cybersécurité, Base de données", "availability": "1h/semaine", "multi_student": True,
     "contact": "Teams"},
    {"name": "Dupont Pierre", "email": "dupontpierre@gmail.com", "specialty": "Expert IA",
     "domains": "Dev", "availability": "3h/semaine", "multi_student": True, "contact": "Teams"},
    {"name": "Charlotte Carotte", "email": "charlottecarot@gmail.com", "specialty": "Infra",
     "domains": "Reseau", "availability": "2h/semaine", "multi_student": True, "contact": "Teams"},
    {"name": "Tom Ford", "email": "tomfolrd@gmail.com", "specialty": "Reseau",
     "domains": "Reseau", "availability": "1h/semaine", "multi_student": True, "contact": "Teams"},
]


# ---------------------------------------------------------------------------
# Données brutes — Mentorés B1 (extraites du formulaire "Etudiants Bachelor")
# ---------------------------------------------------------------------------
B1_MENTEES = [
    {"name": "Maïmouna kaba", "email": "kabamaimouna994@gmail.com", "filiere": "Infra",
     "domains": "Réseau, Cybersécurité", "same_specialty": True, "availability": "Lundi",
     "accepts_m1": True, "contact": "Teams"},
    {"name": "Djiongo dontsi dany brel", "email": "d.djiongodontsi@ecoles-epsi.net", "filiere": "SN1",
     "domains": "Développement Web, Python, Base de données, IA, Gestion de projet, Autres",
     "same_specialty": True, "availability": "Dès que possible", "accepts_m1": True, "contact": "Teams"},
    {"name": "Ndeye mbasse Ndiaye", "email": "ndeyembasse.ndiaye@ecoles-epsi.net", "filiere": "Bachelor 1",
     "domains": "Java, Base de données, Gestion de projet", "same_specialty": True,
     "availability": "Juillet", "accepts_m1": True, "contact": "E-mail"},
    {"name": "Ibbaane", "email": "bakaryibbaane@gmail.com", "filiere": "Informatique",
     "domains": "Cybersécurité", "same_specialty": True, "availability": "Soir",
     "accepts_m1": True, "contact": "Discord"},
    {"name": "AWATE Matawédéou Matis-Léon", "email": "m.awate@ecoles-epsi.net", "filiere": "Développement web",
     "domains": "Réseau", "same_specialty": False, "availability": "Permanente",
     "accepts_m1": False, "contact": "Téléphone"},
    {"name": "Dayane AMOUSSA", "email": "dayaneams25@gmail.com", "filiere": "Informatique",
     "domains": "Développement Web, Python, Base de données", "same_specialty": True,
     "availability": "Tous les jours à partir de 16h", "accepts_m1": True, "contact": "Discord"},
    # NB: AWATE 2e réponse (doublon) volontairement ignorée
    {"name": "Ayemele brile", "email": "brileayemele2@gmail.com", "filiere": "Bachelor SIN1",
     "domains": "Développement Web, Python, Réseau, Cybersécurité, Base de données", "same_specialty": True,
     "availability": "Week-ends, en soirée", "accepts_m1": True, "contact": "E-mail"},
    {"name": "Sacko Oumar", "email": "oumarsacko895@gmail.com", "filiere": "Informatique, Bachelor 1",
     "domains": "Développement Web, Python, Réseau, Cybersécurité, Base de données, IA", "same_specialty": True,
     "availability": "Tous les jours", "accepts_m1": True, "contact": "Téléphone"},
    {"name": "Diané mamadi", "email": "mamdi8511@gmail.com", "filiere": "Informatique, Bachelor 1",
     "domains": "IA", "same_specialty": True, "availability": "3 jours par semaine",
     "accepts_m1": True, "contact": "E-mail"},
    {"name": "Moustapha camara", "email": "Camaramoustapha474@gmail.com", "filiere": "Génie informatique",
     "domains": "Python, Java, Réseau, Cybersécurité, Base de données, IA, Gestion de projet, Open Innovation",
     "same_specialty": True, "availability": "4 fois par semaine", "accepts_m1": False, "contact": "E-mail"},
    {"name": "Bah Boubacar Sidy", "email": "bahboubacarsidy23@gmail.com", "filiere": "Maths-informatique",
     "domains": "Python", "same_specialty": True, "availability": "Les week-ends",
     "accepts_m1": True, "contact": "Téléphone"},
    {"name": "Camara Almamy", "email": "limancamara37@gmail.com", "filiere": "Informatique, Bachelor 1",
     "domains": "Développement Web, Python, Java, Cybersécurité, IA, Gestion de projet", "same_specialty": True,
     "availability": "Disponible à tout moment", "accepts_m1": True, "contact": "E-mail"},
    {"name": "Tounkara Maladho Dian", "email": "nanettetounkara50@gmail.com", "filiere": "Informatique, Bachelor 1",
     "domains": "Infra", "same_specialty": True, "availability": "Soirée (jours ouvrables) et week-end",
     "accepts_m1": True, "contact": "Teams"},
    {"name": "Tounkara Maladho Dian 2", "email": "nanettetounkara@gmail.com", "filiere": "Informatique, Bachelor 2",
     "domains": "Infra / Reseau", "same_specialty": True, "availability": "Tous les jours",
     "accepts_m1": False, "contact": "Teams"},
    {"name": "Tounkara Maladho Dian 3", "email": "nanettetounkara0@gmail.com", "filiere": "Informatique, Bachelor 3",
     "domains": "Développement Web", "same_specialty": True, "availability": "2 fois par semaine",
     "accepts_m1": True, "contact": "Teams"},
    {"name": "test1", "email": "test1@gmail.com", "filiere": "Informatique, Bachelor 4",
     "domains": "Développement Web", "same_specialty": True, "availability": "Week-end",
     "accepts_m1": False, "contact": "Teams"},
    {"name": "test2", "email": "test2@gmail.com", "filiere": "Informatique, Bachelor 5",
     "domains": "Infrastructure", "same_specialty": True, "availability": "Tous les jours",
     "accepts_m1": True, "contact": "Teams"},
    {"name": "test3", "email": "test3@gmail.com", "filiere": "Informatique, Bachelor 6",
     "domains": "Réseau / Système", "same_specialty": True, "availability": "3 fois par semaine",
     "accepts_m1": True, "contact": "Teams"},
    {"name": "tst4", "email": "tst4@gmail.com", "filiere": "Informatique, Bachelor 7",
     "domains": "Développement Web", "same_specialty": True, "availability": "Tous les lundis",
     "accepts_m1": False, "contact": "Teams"},
    {"name": "test5", "email": "test5@gmail.com", "filiere": "Informatique, Bachelor 8",
     "domains": "Développement Web", "same_specialty": True, "availability": "Tous les après-midis",
     "accepts_m1": False, "contact": "Teams"},
    {"name": "test6", "email": "test6@gmail.com", "filiere": "Informatique, Bachelor 9",
     "domains": "Développement Web", "same_specialty": True, "availability": "À tout moment",
     "accepts_m1": True, "contact": "Teams"},
    {"name": "test7", "email": "test7@gmail.fr", "filiere": "Informatique, Bachelor 10",
     "domains": "Développement Web", "same_specialty": True, "availability": "3h par jour",
     "accepts_m1": False, "contact": "Teams"},
    {"name": "test8", "email": "test8@gmail.net", "filiere": "Informatique, Bachelor 11",
     "domains": "Développement Web", "same_specialty": True, "availability": "2h par jour",
     "accepts_m1": True, "contact": "Teams"},
    {"name": "tsstt", "email": "tsstt@gmail.com", "filiere": "Informatique, Bachelor 12",
     "domains": "Développement Web", "same_specialty": True, "availability": "Tous les jours",
     "accepts_m1": True, "contact": "Teams"},
    {"name": "testss", "email": "testss@gmail.com", "filiere": "Informatique, Bachelor 13",
     "domains": "Développement Web", "same_specialty": True, "availability": "Les week-ends",
     "accepts_m1": True, "contact": "Teams"},
    {"name": "coltrane", "email": "coltrane@gmail.com", "filiere": "Informatique, Bachelor 14",
     "domains": "Développement Web", "same_specialty": True, "availability": "Lundi au vendredi",
     "accepts_m1": True, "contact": "Teams"},
    {"name": "tesssssss", "email": "tesss@gamil.net", "filiere": "Informatique, Bachelor 15",
     "domains": "Développement Web", "same_specialty": True, "availability": "Tous les jours",
     "accepts_m1": True, "contact": "Teams"},
]


def filiere_to_specialty(filiere: str) -> str:
    """Nettoie le champ 'Filière' pour le stocker dans User.specialty."""
    return " ".join(filiere.strip().split())


async def get_or_create_user(
    session, email: str, name: str, role: UserRole, year_level: YearLevel,
    specialty: str, bio: str,
) -> tuple[User, bool]:
    """Récupère un utilisateur existant par email, ou le crée.

    Retourne (user, created)."""
    email = clean_email(email)
    result = await session.execute(select(User).where(User.email == email))
    existing = result.scalar_one_or_none()
    if existing:
        return existing, False

    first, last = split_name(name)
    user = User(
        email=email,
        hashed_password=hash_password(DEFAULT_PASSWORD),
        first_name=first,
        last_name=last,
        role=role,
        year_level=year_level,
        specialty=specialty,
        bio=bio,
        is_active=True,
        is_available=True,
    )
    session.add(user)
    await session.flush()
    return user, True


async def add_skills(session, user: User, domains_raw: str, level: SkillLevel, created: bool):
    """Ajoute une UserSkill pour chaque domaine reconnu, en évitant les doublons.

    `created` indique si `user` vient d'être créé dans cette même session :
    dans ce cas, on sait qu'il n'a aucune compétence existante et on évite
    d'accéder à la relation `user.skills` (lazy-load), ce qui provoquerait
    une erreur MissingGreenlet en contexte async. Pour un utilisateur
    déjà existant, on fait une requête explicite avec `select`.
    """
    if created:
        existing_names: set[str] = set()
    else:
        result = await session.execute(select(UserSkill).where(UserSkill.user_id == user.id))
        existing_names = {s.name.lower() for s in result.scalars().all()}

    for domain in split_domains(domains_raw):
        mapped = domain_to_skill(domain)
        if not mapped:
            continue
        skill_name, category = mapped
        if skill_name.lower() in existing_names:
            continue
        existing_names.add(skill_name.lower())
        session.add(UserSkill(
            user_id=user.id,
            name=skill_name,
            category=category,
            level=level,
        ))


async def seed_mentors(session) -> dict[str, int]:
    """Importe les mentors M1. Retourne un dict email -> user_id."""
    mentor_ids: dict[str, int] = {}
    created_count = 0
    skipped_count = 0

    for m in M1_MENTORS:
        user, created = await get_or_create_user(
            session,
            email=m["email"],
            name=m["name"],
            role=UserRole.MENTOR,
            year_level=YearLevel.M1,
            specialty=m["specialty"],
            bio=(
                f"Disponibilité: {m['availability']} | "
                f"Accompagne plusieurs étudiants: {'Oui' if m['multi_student'] else 'Non'} | "
                f"Contact préféré: {m['contact']}"
            ),
        )
        # Les compétences sont "maîtrisées" -> niveau avancé par défaut
        await add_skills(session, user, m["domains"], SkillLevel.ADVANCED, created)
        mentor_ids[clean_email(m["email"])] = user.id
        if created:
            created_count += 1
        else:
            skipped_count += 1

    await session.flush()
    print(f"Mentors M1 : {created_count} créés, {skipped_count} déjà existants.")
    return mentor_ids


async def seed_mentees(session) -> dict[str, int]:
    """Importe les mentorés B1. Retourne un dict email -> user_id."""
    mentee_ids: dict[str, int] = {}
    created_count = 0
    skipped_count = 0

    for b in B1_MENTEES:
        user, created = await get_or_create_user(
            session,
            email=b["email"],
            name=b["name"],
            role=UserRole.STUDENT,
            year_level=YearLevel.B1,
            specialty=filiere_to_specialty(b["filiere"]),
            bio=(
                f"Préfère un mentor même spécialité: {'Oui' if b['same_specialty'] else 'Non'} | "
                f"Disponibilité: {b['availability']} | "
                f"Accepte contact M1: {'Oui' if b['accepts_m1'] else 'Non'} | "
                f"Contact préféré: {b['contact']}"
            ),
        )
        # Les compétences demandées = besoins -> niveau débutant par défaut
        await add_skills(session, user, b["domains"], SkillLevel.BEGINNER, created)
        mentee_ids[clean_email(b["email"])] = user.id
        if created:
            created_count += 1
        else:
            skipped_count += 1

    await session.flush()
    print(f"Mentorés B1 : {created_count} créés, {skipped_count} déjà existants.")
    return mentee_ids


async def main():
    async with AsyncSessionLocal() as session:
        try:
            print("=== Import des mentors M1 ===")
            await seed_mentors(session)

            print("\n=== Import des mentorés B1 ===")
            await seed_mentees(session)

            await session.commit()
            print("\n✅ Import terminé avec succès.")
            print(f"   Mot de passe par défaut pour tous les comptes créés : {DEFAULT_PASSWORD}")
        except Exception:
            await session.rollback()
            raise


if __name__ == "__main__":
    asyncio.run(main())