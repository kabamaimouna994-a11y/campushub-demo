from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from core.database import get_db
from core.security import get_current_user
from models.user import User, UserRole, YearLevel
from models.mentorship import Mentorship, MentoringSession, MentorMessage
from models.skill import UserSkill
from services.matching_service import match_b1_to_m1, rank_matches


# ⭐ MENTORLOOP IA - marqueur utilisé dans Mentorship.goals pour distinguer
# les demandes envoyées depuis l'onglet "Suggestions IA" des demandes
# envoyées depuis la recherche classique de mentors.
MENTORLOOP_IA_MARKER = "[MentorLoop IA]"


def _ensure_aware(dt: datetime) -> datetime:
    """S'assure que le datetime est timezone-aware (UTC)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ⭐ ORDRE DES NIVEAUX POUR LE MENTORAT ⭐
LEVEL_ORDER = {
    "B1": 1,
    "B2": 2,
    "B3": 3,
    "M1": 4,
    "M2": 5
}


router = APIRouter()


class MentorshipCreate(BaseModel):
    mentor_id: int
    goals: Optional[str] = None


class SessionCreate(BaseModel):
    scheduled_at: datetime
    duration_min: int = 60
    location: Optional[str] = None
    topic: Optional[str] = None


class SessionFeedback(BaseModel):
    rating: int
    feedback: Optional[str] = None


class SessionUpdate(BaseModel):
    status: Optional[str] = None
    location: Optional[str] = None
    topic: Optional[str] = None
    scheduled_at: Optional[datetime] = None


class MessageCreate(BaseModel):
    content: str


# ========== MENTORATS ==========

@router.get("/")
async def get_my_mentorships(
    current_user: User = Depends(get_current_user), 
    db: AsyncSession = Depends(get_db)
):
    """Récupère toutes les relations de mentorat de l'utilisateur"""
    
    stmt = select(Mentorship).where(
        (Mentorship.mentor_id == current_user.id) | (Mentorship.mentee_id == current_user.id)
    ).options(selectinload(Mentorship.mentor), selectinload(Mentorship.mentee))
    result = await db.execute(stmt)
    mentorships = result.scalars().all()

    output = []
    for m in mentorships:
        if m.mentor_id == current_user.id:
            partner = m.mentee
            role = "mentor"
        else:
            partner = m.mentor
            role = "mentoré"
        
        output.append({
            "id": m.id,
            "role": role,
            "partner_name": partner.full_name,
            "partner_year_level": partner.year_level.value if partner.year_level else None,
            "status": m.status,
        })
    return output


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_mentorship(
    data: MentorshipCreate, 
    current_user: User = Depends(get_current_user), 
    db: AsyncSession = Depends(get_db)
):
    """Crée une relation de mentorat - ⭐ Vérifie la hiérarchie des niveaux ⭐"""
    
    result = await db.execute(select(User).where(User.id == data.mentor_id))
    mentor = result.scalar_one_or_none()
    if not mentor:
        raise HTTPException(status_code=404, detail="Mentor introuvable")
    
    # ⭐ VÉRIFICATION DE LA HIÉRARCHIE ⭐
    mentee_level = current_user.year_level.value if current_user.year_level else "B1"
    mentor_level = mentor.year_level.value if mentor.year_level else "B1"
    
    mentee_order = LEVEL_ORDER.get(mentee_level, 1)
    mentor_order = LEVEL_ORDER.get(mentor_level, 1)
    
    if mentor_order <= mentee_order:
        raise HTTPException(
            status_code=400, 
            detail=f"Le mentor doit avoir un niveau supérieur. "
                   f"Votre niveau: {mentee_level}, Niveau du mentor: {mentor_level}. "
                   f"Hiérarchie acceptée: B1 → B2 → B3 → M1 → M2"
        )
    
    # Vérifier si une relation existe déjà
    existing = await db.execute(
        select(Mentorship).where(
            Mentorship.mentor_id == data.mentor_id,
            Mentorship.mentee_id == current_user.id
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Vous avez déjà contacté ce mentor")
    
    mentorship = Mentorship(
        mentor_id=data.mentor_id, 
        mentee_id=current_user.id, 
        goals=data.goals,
        status="pending"
    )
    db.add(mentorship)
    await db.flush()
    
    return {
        "id": mentorship.id, 
        "mentor_name": mentor.full_name, 
        "mentor_level": mentor_level,
        "message": "Demande de mentorat envoyée"
    }


# ========== ACCEPTER UN MENTORAT ==========

@router.put("/{mentorship_id}/accept")
async def accept_mentorship(
    mentorship_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Accepter une demande de mentorat (pour le mentor uniquement)"""
    
    # Récupérer le mentorat avec les relations
    result = await db.execute(
        select(Mentorship)
        .where(Mentorship.id == mentorship_id)
        .options(selectinload(Mentorship.mentor), selectinload(Mentorship.mentee))
    )
    mentorship = result.scalar_one_or_none()
    
    if not mentorship:
        raise HTTPException(status_code=404, detail="Mentorat non trouvé")
    
    # Vérifier que l'utilisateur actuel est bien le mentor
    if mentorship.mentor_id != current_user.id:
        raise HTTPException(
            status_code=403, 
            detail="Vous n'êtes pas le mentor de cette relation. Seul le mentor peut accepter la demande."
        )
    
    # Vérifier que le statut est 'pending'
    if mentorship.status != 'pending':
        raise HTTPException(
            status_code=400, 
            detail=f"Impossible d'accepter : le statut actuel est '{mentorship.status}'. Seules les demandes en attente peuvent être acceptées."
        )
    
    # Mettre à jour le statut
    mentorship.status = 'active'
    await db.flush()
    
    # Retourner les informations mises à jour
    return {
        "message": "Demande de mentorat acceptée avec succès",
        "id": mentorship.id,
        "status": mentorship.status,
        "mentor_name": mentorship.mentor.full_name if mentorship.mentor else None,
        "mentee_name": mentorship.mentee.full_name if mentorship.mentee else None
    }


# ========== MESSAGES ==========

@router.post("/{mentorship_id}/messages", status_code=status.HTTP_201_CREATED)
async def send_message(
    mentorship_id: int, 
    data: MessageCreate, 
    current_user: User = Depends(get_current_user), 
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(Mentorship).where(Mentorship.id == mentorship_id))
    mentorship = result.scalar_one_or_none()
    if not mentorship:
        raise HTTPException(status_code=404, detail="Relation introuvable")

    message = MentorMessage(mentorship_id=mentorship_id, sender_id=current_user.id, content=data.content)
    db.add(message)
    await db.flush()
    return {"id": message.id, "message": "Message envoyé"}


@router.get("/{mentorship_id}/messages")
async def get_messages(
    mentorship_id: int, 
    current_user: User = Depends(get_current_user), 
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(Mentorship)
        .where(Mentorship.id == mentorship_id)
        .options(selectinload(Mentorship.messages))
    )
    mentorship = result.scalar_one_or_none()
    if not mentorship:
        raise HTTPException(status_code=404, detail="Relation introuvable")

    return [
        {
            "id": m.id,
            "sender_id": m.sender_id,
            "content": m.content,
            "sent_at": m.sent_at.isoformat(),
            "is_mine": m.sender_id == current_user.id,
            "is_read": m.is_read
        }
        for m in mentorship.messages
    ]


@router.put("/{mentorship_id}/messages/{message_id}/read")
async def mark_message_as_read(
    mentorship_id: int,
    message_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(MentorMessage).where(
            MentorMessage.id == message_id,
            MentorMessage.mentorship_id == mentorship_id
        )
    )
    message = result.scalar_one_or_none()
    
    if not message:
        raise HTTPException(status_code=404, detail="Message introuvable")
    
    if message.sender_id == current_user.id:
        raise HTTPException(status_code=403, detail="Vous ne pouvez pas marquer vos propres messages comme lus")
    
    message.is_read = True
    await db.flush()
    
    return {"message": "Message marqué comme lu"}


# ========== SESSIONS ==========

@router.get("/{mentorship_id}/sessions")
async def get_mentorship_sessions(
    mentorship_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Récupère toutes les sessions d'un mentorat (uniquement les futures)"""
    result = await db.execute(
        select(Mentorship).where(Mentorship.id == mentorship_id)
    )
    mentorship = result.scalar_one_or_none()
    
    if not mentorship:
        raise HTTPException(status_code=404, detail="Mentorat non trouvé")
    
    if mentorship.mentor_id != current_user.id and mentorship.mentee_id != current_user.id:
        raise HTTPException(status_code=403, detail="Accès non autorisé")
    
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(MentoringSession)
        .where(
            MentoringSession.mentorship_id == mentorship_id,
            MentoringSession.scheduled_at >= now
        )
        .order_by(MentoringSession.scheduled_at)
    )
    sessions = result.scalars().all()
    
    output = []
    for session in sessions:
        output.append({
            "id": session.id,
            "mentorship_id": session.mentorship_id,
            "scheduled_at": session.scheduled_at.isoformat(),
            "duration_min": session.duration_min,
            "location": session.location,
            "topic": session.topic,
            "status": session.status,
            "mentee_rating": session.mentee_rating,
            "mentee_feedback": session.mentee_feedback,
            "mentor_feedback": session.mentor_feedback
        })
    
    return output


@router.post("/{mentorship_id}/sessions", status_code=201)
async def create_mentorship_session(
    mentorship_id: int,
    session_data: SessionCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Crée une nouvelle session de mentorat - ⭐ Seul le mentor peut le faire ⭐"""
    
    result = await db.execute(
        select(Mentorship).where(Mentorship.id == mentorship_id)
    )
    mentorship = result.scalar_one_or_none()
    
    if not mentorship:
        raise HTTPException(status_code=404, detail="Mentorat non trouvé")
    
    # ⭐ VÉRIFICATION : Seul le mentor peut créer une session ⭐
    if mentorship.mentor_id != current_user.id:
        raise HTTPException(status_code=403, detail="Seul le mentor peut planifier une session")
    
    if _ensure_aware(session_data.scheduled_at) <= datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="La date de la session doit être dans le futur")
    
    new_session = MentoringSession(
        mentorship_id=mentorship_id,
        scheduled_at=session_data.scheduled_at,
        duration_min=session_data.duration_min,
        location=session_data.location,
        topic=session_data.topic,
        status="scheduled"
    )
    
    db.add(new_session)
    await db.flush()
    await db.refresh(new_session)
    
    return {
        "id": new_session.id,
        "mentorship_id": new_session.mentorship_id,
        "scheduled_at": new_session.scheduled_at.isoformat(),
        "duration_min": new_session.duration_min,
        "location": new_session.location,
        "topic": new_session.topic,
        "status": new_session.status
    }


@router.delete("/{mentorship_id}/sessions/{session_id}")
async def delete_session(
    mentorship_id: int,
    session_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Supprimer une session (uniquement par le mentor)"""
    
    result = await db.execute(
        select(Mentorship).where(Mentorship.id == mentorship_id)
    )
    mentorship = result.scalar_one_or_none()
    
    if not mentorship:
        raise HTTPException(status_code=404, detail="Mentorat non trouvé")
    
    if mentorship.mentor_id != current_user.id and mentorship.mentee_id != current_user.id:
        raise HTTPException(status_code=403, detail="Accès non autorisé")
    
    result = await db.execute(
        select(MentoringSession).where(
            MentoringSession.mentorship_id == mentorship_id,
            MentoringSession.id == session_id
        )
    )
    session = result.scalar_one_or_none()
    
    if not session:
        raise HTTPException(status_code=404, detail="Session non trouvée")
    
    await db.delete(session)
    await db.commit()
    
    return {"message": "Session supprimée avec succès"}


@router.get("/{mentorship_id}/sessions/{session_id}")
async def get_session_detail(
    mentorship_id: int,
    session_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Récupère les détails d'une session spécifique"""
    result = await db.execute(
        select(Mentorship).where(Mentorship.id == mentorship_id)
    )
    mentorship = result.scalar_one_or_none()
    
    if not mentorship:
        raise HTTPException(status_code=404, detail="Mentorat non trouvé")
    
    if mentorship.mentor_id != current_user.id and mentorship.mentee_id != current_user.id:
        raise HTTPException(status_code=403, detail="Accès non autorisé")
    
    result = await db.execute(
        select(MentoringSession).where(
            MentoringSession.mentorship_id == mentorship_id,
            MentoringSession.id == session_id
        )
    )
    session = result.scalar_one_or_none()
    
    if not session:
        raise HTTPException(status_code=404, detail="Session non trouvée")
    
    return {
        "id": session.id,
        "mentorship_id": session.mentorship_id,
        "scheduled_at": session.scheduled_at.isoformat(),
        "duration_min": session.duration_min,
        "location": session.location,
        "topic": session.topic,
        "status": session.status,
        "mentee_rating": session.mentee_rating,
        "mentee_feedback": session.mentee_feedback,
        "mentor_feedback": session.mentor_feedback
    }


@router.patch("/{mentorship_id}/sessions/{session_id}")
async def update_session(
    mentorship_id: int,
    session_id: int,
    session_update: SessionUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Met à jour une session (annuler, reporter, modifier)"""
    result = await db.execute(
        select(Mentorship).where(Mentorship.id == mentorship_id)
    )
    mentorship = result.scalar_one_or_none()
    
    if not mentorship:
        raise HTTPException(status_code=404, detail="Mentorat non trouvé")
    
    if mentorship.mentor_id != current_user.id and mentorship.mentee_id != current_user.id:
        raise HTTPException(status_code=403, detail="Accès non autorisé")
    
    result = await db.execute(
        select(MentoringSession).where(
            MentoringSession.mentorship_id == mentorship_id,
            MentoringSession.id == session_id
        )
    )
    session = result.scalar_one_or_none()
    
    if not session:
        raise HTTPException(status_code=404, detail="Session non trouvée")
    
    update_data = session_update.dict(exclude_unset=True)
    
    if "status" in update_data:
        valid_statuses = ["scheduled", "completed", "cancelled", "in_progress"]
        if update_data["status"] not in valid_statuses:
            raise HTTPException(status_code=400, detail=f"Statut invalide. Choisir parmi: {valid_statuses}")
        session.status = update_data["status"]
    
    if "location" in update_data:
        session.location = update_data["location"]
    
    if "topic" in update_data:
        session.topic = update_data["topic"]
    
    if "scheduled_at" in update_data:
        if _ensure_aware(update_data["scheduled_at"]) <= datetime.now(timezone.utc):
            raise HTTPException(status_code=400, detail="La date doit être dans le futur")
        session.scheduled_at = update_data["scheduled_at"]
    
    await db.flush()
    
    return {"message": "Session mise à jour", "status": session.status}


@router.post("/{mentorship_id}/sessions/{session_id}/feedback")
async def submit_session_feedback(
    mentorship_id: int,
    session_id: int,
    feedback: SessionFeedback,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Soumet une évaluation pour une session (par le mentoré)"""
    result = await db.execute(
        select(Mentorship).where(Mentorship.id == mentorship_id)
    )
    mentorship = result.scalar_one_or_none()
    
    if not mentorship:
        raise HTTPException(status_code=404, detail="Mentorat non trouvé")
    
    if mentorship.mentee_id != current_user.id:
        raise HTTPException(status_code=403, detail="Seul le mentoré peut évaluer la session")
    
    result = await db.execute(
        select(MentoringSession).where(
            MentoringSession.mentorship_id == mentorship_id,
            MentoringSession.id == session_id
        )
    )
    session = result.scalar_one_or_none()
    
    if not session:
        raise HTTPException(status_code=404, detail="Session non trouvée")
    
    if _ensure_aware(session.scheduled_at) > datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Impossible d'évaluer une session future")
    
    if session.mentee_rating is not None:
        raise HTTPException(status_code=400, detail="Cette session a déjà été évaluée")
    
    if feedback.rating < 1 or feedback.rating > 5:
        raise HTTPException(status_code=400, detail="La note doit être comprise entre 1 et 5")
    
    session.mentee_rating = float(feedback.rating)
    session.mentee_feedback = feedback.feedback
    session.status = "completed"
    
    await db.flush()
    
    return {
        "message": "Évaluation enregistrée avec succès",
        "rating": feedback.rating
    }


# ========== SUPPRIMER UNE CONVERSATION (MENTORAT) ==========

@router.delete("/{mentorship_id}")
async def delete_mentorship(
    mentorship_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Supprimer une conversation (mentorat) et tous ses messages"""
    
    result = await db.execute(
        select(Mentorship).where(Mentorship.id == mentorship_id)
    )
    mentorship = result.scalar_one_or_none()
    
    if not mentorship:
        raise HTTPException(status_code=404, detail="Conversation introuvable")
    
    # Vérifier que l'utilisateur est bien impliqué dans la conversation
    if mentorship.mentor_id != current_user.id and mentorship.mentee_id != current_user.id:
        raise HTTPException(status_code=403, detail="Accès non autorisé")
    
    # Supprimer le mentorat (les messages et sessions sont supprimés en cascade)
    await db.delete(mentorship)
    await db.commit()
    
    return {"message": "Conversation supprimée avec succès"}

# =============================================================================
# ========== MENTORLOOP : MATCHING B1 <-> M1 (basé sur formulaires) ==========
# =============================================================================
#
# Ces endpoints ne modifient aucune route existante. Ils s'appuient sur les
# données importées via seed_mentorloop.py :
#   - User.role / User.year_level pour distinguer mentors (M1) et mentorés (B1)
#   - User.specialty pour la filière / spécialité
#   - User.bio pour stocker la disponibilité, le moyen de contact préféré,
#     la préférence "même spécialité" et l'acceptation de contact M1
#     (sérialisées sous forme de texte "clé: valeur | clé: valeur")
#   - UserSkill pour les domaines (besoins pour les B1, compétences pour M1)


def _parse_bio_field(bio: Optional[str], field_prefix: str) -> str:
    """Extrait la valeur d'un champ encodé dans bio sous la forme
    'Préfixe: valeur | Autre: ...'. Retourne '' si absent."""
    if not bio:
        return ""
    for part in bio.split("|"):
        part = part.strip()
        if part.lower().startswith(field_prefix.lower()):
            return part.split(":", 1)[1].strip() if ":" in part else ""
    return ""


def _bio_flag_is_yes(bio: Optional[str], field_prefix: str, default: bool = True) -> bool:
    value = _parse_bio_field(bio, field_prefix)
    if not value:
        return default
    return value.strip().lower() == "oui"


async def _build_mentor_dict(db: AsyncSession, mentor: User) -> dict:
    """Construit le dict mentor attendu par match_b1_to_m1, en incluant
    la vérification de capacité (déjà au max d'étudiants accompagnés)."""
    accepts_multiple = _bio_flag_is_yes(mentor.bio, "Accompagne plusieurs étudiants")

    at_capacity = False
    if not accepts_multiple:
        count_result = await db.execute(
            select(func.count(Mentorship.id)).where(
                Mentorship.mentor_id == mentor.id,
                Mentorship.status.in_(["pending", "active"]),
            )
        )
        active_count = count_result.scalar_one()
        at_capacity = active_count >= 1

    return {
        "id": mentor.id,
        "specialty": mentor.specialty,
        "availability_text": _parse_bio_field(mentor.bio, "Disponibilité"),
        "contact_method": _parse_bio_field(mentor.bio, "Contact préféré"),
        "accepts_multiple_students": accepts_multiple,
        "at_capacity": at_capacity,
    }


def _build_mentee_dict(mentee: User) -> dict:
    return {
        "id": mentee.id,
        "specialty": mentee.specialty,
        "same_specialty_preferred": _bio_flag_is_yes(mentee.bio, "Préfère un mentor même spécialité"),
        "availability_text": _parse_bio_field(mentee.bio, "Disponibilité"),
        "contact_method": _parse_bio_field(mentee.bio, "Contact préféré"),
        "accepts_m1": _bio_flag_is_yes(mentee.bio, "Accepte contact M1"),
    }


async def _get_skill_dicts(db: AsyncSession, user_id: int) -> list[dict]:
    result = await db.execute(select(UserSkill).where(UserSkill.user_id == user_id))
    return [{"name": s.name, "level": s.level.value} for s in result.scalars().all()]


@router.get("/mentorloop/suggestions/{mentee_id}")
async def get_mentorloop_suggestions(
    mentee_id: int,
    top_k: int = 3,
    min_score: float = 0.0,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Retourne les meilleurs mentors M1 suggérés pour un mentoré B1 donné.

    Basé sur les réponses aux formulaires MentorLoop (domaines, disponibilité,
    préférence de spécialité, moyen de contact, capacité d'accueil du mentor).
    """
    mentee_result = await db.execute(select(User).where(User.id == mentee_id))
    mentee = mentee_result.scalar_one_or_none()
    if not mentee:
        raise HTTPException(status_code=404, detail="Mentoré introuvable")

    if mentee.year_level != YearLevel.B1:
        raise HTTPException(status_code=400, detail="Cet endpoint concerne uniquement les mentorés B1")

    mentors_result = await db.execute(
        select(User).where(User.role == UserRole.MENTOR, User.year_level == YearLevel.M1)
    )
    mentors = mentors_result.scalars().all()

    mentee_dict = _build_mentee_dict(mentee)
    mentee_skills = await _get_skill_dicts(db, mentee.id)

    if mentee_dict["accepts_m1"] is False:
        return {
            "mentee_id": mentee.id,
            "mentee_name": mentee.full_name,
            "message": "Ce mentoré a indiqué ne pas vouloir être contacté par un mentor M1.",
            "suggestions": [],
        }

    matches = []
    for mentor in mentors:
        mentor_dict = await _build_mentor_dict(db, mentor)
        mentor_skills = await _get_skill_dicts(db, mentor.id)
        result = match_b1_to_m1(mentee_dict, mentor_dict, mentee_skills, mentor_skills)
        if result:
            matches.append((result, mentor))

    ranked = rank_matches([r for r, _ in matches], top_k=top_k, min_score=min_score)
    mentor_by_id = {m.entity_id: mentor for m, mentor in matches}

    suggestions = []
    for r in ranked:
        mentor = mentor_by_id[r.entity_id]
        suggestions.append({
            "mentor_id": mentor.id,
            "mentor_name": mentor.full_name,
            "mentor_specialty": mentor.specialty,
            "score_percent": r.score_percent,
            "explanation": r.explanation,
        })

    return {
        "mentee_id": mentee.id,
        "mentee_name": mentee.full_name,
        "mentee_specialty": mentee.specialty,
        "suggestions": suggestions,
    }


@router.post("/mentorloop/run-matching")
async def run_mentorloop_matching(
    top_k: int = 3,
    min_score: float = 0.0,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Lance le matching pour tous les mentorés B1 importés et retourne
    un récapitulatif des meilleures suggestions par mentoré.

    Ne crée AUCUNE relation de Mentorship automatiquement — c'est un
    aperçu/recommandation que l'utilisateur peut ensuite valider via
    POST /api/mentorat/ (create_mentorship).
    """
    mentees_result = await db.execute(
        select(User).where(User.role == UserRole.STUDENT, User.year_level == YearLevel.B1)
    )
    mentees = mentees_result.scalars().all()

    mentors_result = await db.execute(
        select(User).where(User.role == UserRole.MENTOR, User.year_level == YearLevel.M1)
    )
    mentors = mentors_result.scalars().all()

    if not mentees or not mentors:
        raise HTTPException(
            status_code=404,
            detail="Aucun mentoré B1 ou mentor M1 trouvé. Avez-vous lancé seed_mentorloop.py ?"
        )

    # Pré-calcul des compétences pour éviter les requêtes répétées
    mentor_skills_cache: dict[int, list[dict]] = {}
    mentor_dict_cache: dict[int, dict] = {}
    for mentor in mentors:
        mentor_skills_cache[mentor.id] = await _get_skill_dicts(db, mentor.id)
        mentor_dict_cache[mentor.id] = await _build_mentor_dict(db, mentor)

    results = []
    no_match_count = 0
    excluded_count = 0

    for mentee in mentees:
        mentee_dict = _build_mentee_dict(mentee)

        if mentee_dict["accepts_m1"] is False:
            excluded_count += 1
            results.append({
                "mentee_id": mentee.id,
                "mentee_name": mentee.full_name,
                "mentee_specialty": mentee.specialty,
                "suggestions": [],
                "note": "N'accepte pas le contact par un mentor M1",
            })
            continue

        mentee_skills = await _get_skill_dicts(db, mentee.id)

        matches = []
        for mentor in mentors:
            result = match_b1_to_m1(
                mentee_dict, mentor_dict_cache[mentor.id], mentee_skills, mentor_skills_cache[mentor.id]
            )
            if result:
                matches.append((result, mentor))

        ranked = rank_matches([r for r, _ in matches], top_k=top_k, min_score=min_score)
        mentor_by_id = {m.entity_id: mentor for m, mentor in matches}

        if not ranked:
            no_match_count += 1

        suggestions = []
        for r in ranked:
            mentor = mentor_by_id[r.entity_id]
            suggestions.append({
                "mentor_id": mentor.id,
                "mentor_name": mentor.full_name,
                "mentor_specialty": mentor.specialty,
                "score_percent": r.score_percent,
                "explanation": r.explanation,
            })

        results.append({
            "mentee_id": mentee.id,
            "mentee_name": mentee.full_name,
            "mentee_specialty": mentee.specialty,
            "suggestions": suggestions,
        })

    return {
        "total_mentees": len(mentees),
        "total_mentors": len(mentors),
        "mentees_without_match": no_match_count,
        "mentees_excluded_no_m1_contact": excluded_count,
        "results": results,
    }


@router.get("/mentorloop/stats")
async def get_mentorloop_stats(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Statistiques globales sur le matching MentorLoop B1 <-> M1.

    Retourne :
    - le nombre de mentorés B1 ayant au moins une suggestion >= 50%
    - le top des mentors M1 les plus suggérés (toutes positions confondues)
    - le nombre de demandes de mentorat envoyées depuis l'onglet
      "Suggestions IA" (identifiées via le marqueur MENTORLOOP_IA_MARKER
      dans Mentorship.goals)
    """
    mentees_result = await db.execute(
        select(User).where(User.role == UserRole.STUDENT, User.year_level == YearLevel.B1)
    )
    mentees = mentees_result.scalars().all()

    mentors_result = await db.execute(
        select(User).where(User.role == UserRole.MENTOR, User.year_level == YearLevel.M1)
    )
    mentors = mentors_result.scalars().all()

    if not mentees or not mentors:
        raise HTTPException(
            status_code=404,
            detail="Aucun mentoré B1 ou mentor M1 trouvé. Avez-vous lancé seed_mentorloop.py ?"
        )

    # Pré-calcul des compétences / dicts mentors
    mentor_skills_cache: dict[int, list[dict]] = {}
    mentor_dict_cache: dict[int, dict] = {}
    for mentor in mentors:
        mentor_skills_cache[mentor.id] = await _get_skill_dicts(db, mentor.id)
        mentor_dict_cache[mentor.id] = await _build_mentor_dict(db, mentor)

    mentees_with_good_match = 0
    mentor_suggestion_counts: dict[int, int] = {m.id: 0 for m in mentors}

    for mentee in mentees:
        mentee_dict = _build_mentee_dict(mentee)
        if mentee_dict["accepts_m1"] is False:
            continue

        mentee_skills = await _get_skill_dicts(db, mentee.id)

        matches = []
        for mentor in mentors:
            result = match_b1_to_m1(
                mentee_dict, mentor_dict_cache[mentor.id], mentee_skills, mentor_skills_cache[mentor.id]
            )
            if result:
                matches.append((result, mentor))

        ranked = rank_matches([r for r, _ in matches], top_k=3, min_score=0)
        mentor_by_id = {m.entity_id: mentor for m, mentor in matches}

        if ranked and ranked[0].score_percent >= 50:
            mentees_with_good_match += 1

        for r in ranked:
            mentor = mentor_by_id[r.entity_id]
            mentor_suggestion_counts[mentor.id] = mentor_suggestion_counts.get(mentor.id, 0) + 1

    top_mentors = sorted(
        ((mid, count) for mid, count in mentor_suggestion_counts.items() if count > 0),
        key=lambda x: x[1],
        reverse=True
    )[:5]
    mentor_by_id_all = {m.id: m for m in mentors}
    top_mentors_list = [
        {
            "mentor_id": mid,
            "mentor_name": mentor_by_id_all[mid].full_name,
            "mentor_specialty": mentor_by_id_all[mid].specialty,
            "suggestion_count": count,
        }
        for mid, count in top_mentors
    ]

    # Compter les demandes de mentorat envoyées depuis MentorLoop IA
    ia_requests_result = await db.execute(
        select(func.count(Mentorship.id)).where(
            Mentorship.goals.ilike(f"%{MENTORLOOP_IA_MARKER}%")
        )
    )
    ia_requests_count = ia_requests_result.scalar_one()

    ia_requests_accepted_result = await db.execute(
        select(func.count(Mentorship.id)).where(
            Mentorship.goals.ilike(f"%{MENTORLOOP_IA_MARKER}%"),
            Mentorship.status == "active",
        )
    )
    ia_requests_accepted = ia_requests_accepted_result.scalar_one()

    total_eligible_mentees = sum(
        1 for m in mentees if _bio_flag_is_yes(m.bio, "Accepte contact M1")
    )

    return {
        "total_mentees": len(mentees),
        "total_mentors": len(mentors),
        "eligible_mentees": total_eligible_mentees,
        "mentees_with_good_match": mentees_with_good_match,
        "good_match_rate_percent": (
            round(mentees_with_good_match / total_eligible_mentees * 100)
            if total_eligible_mentees > 0 else 0
        ),
        "top_mentors": top_mentors_list,
        "ia_requests_sent": ia_requests_count,
        "ia_requests_accepted": ia_requests_accepted,
    }
