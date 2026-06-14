"""
Service de Matching IA pour CampusHub
"""

from dataclasses import dataclass
from typing import Optional

YEAR_ORDER = {"B1": 1, "B2": 2, "B3": 3, "M1": 4, "M2": 5}
LEVEL_SCORES = {"débutant": 1, "intermédiaire": 2, "avancé": 3, "expert": 4}


@dataclass
class MatchResult:
    entity_id: int
    total_score: float
    skill_score: float
    availability_score: float
    interest_score: float
    history_score: float
    explanation: str

    @property
    def score_percent(self) -> int:
        return round(self.total_score * 100)


def compute_skill_score(user_skills: list, required_skills: list) -> float:
    if not required_skills:
        return 0.50

    user_map = {s["name"].lower(): LEVEL_SCORES.get(s.get("level", "débutant"), 1) for s in user_skills}
    total_weight = 0.0
    matched_weight = 0.0

    for req in required_skills:
        req_name = req.get("name", "").lower()
        req_level = LEVEL_SCORES.get(req.get("level", "débutant"), 1)
        total_weight += req_level

        if req_name in user_map:
            coverage = min(user_map[req_name] / req_level, 1.0)
            matched_weight += req_level * coverage

    return matched_weight / total_weight if total_weight > 0 else 0.0


def compute_availability_score(user_hours: int, project_hours: int) -> float:
    if project_hours == 0:
        return 1.0
    return min(user_hours / project_hours, 1.0)


def compute_interest_score(user_specialty: Optional[str], project_type: Optional[str]) -> float:
    if not user_specialty or not project_type:
        return 0.30
    specialty_lower = user_specialty.lower()
    project_lower = project_type.lower()
    if "data" in specialty_lower and "data" in project_lower:
        return 0.90
    if "dev" in specialty_lower and "tech" in project_lower:
        return 0.90
    if "design" in specialty_lower and "design" in project_lower:
        return 0.90
    return 0.40


def compute_history_score(completed_projects: int, avg_rating: float) -> float:
    project_bonus = min(completed_projects * 0.05, 0.50)
    rating_score = (avg_rating / 5.0) * 0.50 if avg_rating > 0 else 0.20
    return min(project_bonus + rating_score, 1.0)


def match_user_to_project(
    user_skills: list, 
    user_specialty: Optional[str], 
    user_hours_per_week: int, 
    user_history: dict, 
    project: dict
) -> MatchResult:
    skill_score = compute_skill_score(user_skills, project.get("required_skills", []))
    availability_score = compute_availability_score(user_hours_per_week, project.get("required_hours_per_week", 8))
    interest_score = compute_interest_score(user_specialty, project.get("type"))
    history_score = compute_history_score(user_history.get("completed_projects", 0), user_history.get("avg_rating", 0.0))

    total = (skill_score * 0.50 + availability_score * 0.20 + interest_score * 0.20 + history_score * 0.10)

    return MatchResult(
        entity_id=project["id"],
        total_score=round(min(total, 1.0), 4),
        skill_score=round(skill_score, 4),
        availability_score=round(availability_score, 4),
        interest_score=round(interest_score, 4),
        history_score=round(history_score, 4),
        explanation=f"Compétences {skill_score:.0%} · Dispo {availability_score:.0%} · Intérêts {interest_score:.0%}",
    )


def match_mentor_to_mentee(
    mentor: dict, 
    mentee: dict, 
    mentor_skills: list, 
    mentee_skills: list
) -> Optional[MatchResult]:
    mentor_order = YEAR_ORDER.get(mentor.get("year_level", ""), 0)
    mentee_order = YEAR_ORDER.get(mentee.get("year_level", ""), 0)

    if mentor_order <= mentee_order:
        return None

    mentee_weak = {s["name"].lower() for s in mentee_skills if s.get("level") in ("débutant", "intermédiaire")}
    mentor_strong = {s["name"].lower() for s in mentor_skills if s.get("level") in ("avancé", "expert")}

    if mentee_weak:
        skill_score = min(len(mentor_strong & mentee_weak) / len(mentee_weak), 1.0)
    else:
        skill_score = 0.40

    interest_score = compute_interest_score(mentee.get("specialty"), mentor.get("specialty"))
    availability_score = 1.0 if mentor.get("is_available") else 0.0
    level_bonus = min((mentor_order - mentee_order) * 0.10, 0.40)

    total = skill_score * 0.50 + interest_score * 0.25 + availability_score * 0.15 + level_bonus * 0.10

    return MatchResult(
        entity_id=mentor["id"],
        total_score=round(min(total, 1.0), 4),
        skill_score=round(skill_score, 4),
        availability_score=round(availability_score, 4),
        interest_score=round(interest_score, 4),
        history_score=round(level_bonus, 4),
        explanation=f"Complémentarité {skill_score:.0%} · Distance {mentor_order - mentee_order} niveau(x)",
    )


def rank_matches(matches: list, top_k: int = 10, min_score: float = 0.30) -> list:
    """Trie et filtre les matchings par score."""
    filtered = [m for m in matches if m.total_score >= min_score]
    return sorted(filtered, key=lambda x: x.total_score, reverse=True)[:top_k]


# ---------------------------------------------------------------------------
# Matching B1 <-> M1 basé sur les réponses aux formulaires MentorLoop
# ---------------------------------------------------------------------------

def compute_specialty_overlap_score(
    mentee_skills: list,
    mentor_skills: list,
    same_specialty_preferred: bool,
) -> float:
    """Score de chevauchement des domaines demandés / proposés.

    mentee_skills : liste de compétences "besoins" du mentoré (UserSkill)
    mentor_skills : liste de compétences "maîtrisées" du mentor (UserSkill)
    same_specialty_preferred : valeur du champ "Préférez-vous un mentor
        dans la même spécialité ?" du formulaire B1.
    """
    mentee_domains = {s["name"].lower() for s in mentee_skills}
    mentor_domains = {s["name"].lower() for s in mentor_skills}

    if not mentee_domains:
        return 0.40

    overlap = mentee_domains & mentor_domains
    coverage = len(overlap) / len(mentee_domains)

    if same_specialty_preferred and coverage == 0:
        return 0.05

    return coverage


def compute_availability_text_score(mentee_availability: str, mentor_availability: str) -> float:
    """Score heuristique de compatibilité entre deux disponibilités en texte libre."""
    if not mentee_availability or not mentor_availability:
        return 0.50

    mentee_lower = mentee_availability.lower()
    mentor_lower = mentor_availability.lower()

    high_flex_keywords = (
        "tous les jours", "permanente", "à tout moment", "a tout moment",
        "quasi totale", "disponible à tout moment", "toutes les jours",
    )
    if any(k in mentee_lower for k in high_flex_keywords) or any(k in mentor_lower for k in high_flex_keywords):
        return 0.90

    weekday_keywords = ["lundi", "mardi", "mercredi", "jeudi", "vendredi"]
    weekend_keywords = ["week-end", "weekend", "samedi", "dimanche"]
    evening_keywords = ["soir", "soirée"]

    def has_any(text: str, keywords: list[str]) -> bool:
        return any(k in text for k in keywords)

    categories = [weekday_keywords, weekend_keywords, evening_keywords]
    for cat in categories:
        if has_any(mentee_lower, cat) and has_any(mentor_lower, cat):
            return 0.80

    return 0.40


def compute_contact_method_score(mentee_contact: str, mentor_contact: str) -> float:
    """Bonus si le mentoré et le mentor partagent le même moyen de contact préféré."""
    if not mentee_contact or not mentor_contact:
        return 0.50
    return 1.0 if mentee_contact.strip().lower() == mentor_contact.strip().lower() else 0.50


def match_b1_to_m1(
    mentee: dict,
    mentor: dict,
    mentee_skills: list,
    mentor_skills: list,
) -> Optional[MatchResult]:
    """Calcule le score de matching entre un mentoré B1 et un mentor M1.

    mentee : dict avec au moins {id, specialty, same_specialty_preferred,
             availability_text, contact_method, accepts_m1}
    mentor : dict avec au moins {id, specialty, availability_text,
             contact_method, accepts_multiple_students, at_capacity}

    Retourne None si le mentoré a indiqué ne pas vouloir être contacté
    par un mentor M1, ou si le mentor est déjà à pleine capacité.
    """
    if mentee.get("accepts_m1") is False:
        return None

    if mentor.get("at_capacity"):
        return None

    skill_score = compute_specialty_overlap_score(
        mentee_skills, mentor_skills, mentee.get("same_specialty_preferred", False)
    )
    availability_score = compute_availability_text_score(
        mentee.get("availability_text", ""), mentor.get("availability_text", "")
    )
    interest_score = compute_interest_score(mentee.get("specialty"), mentor.get("specialty"))
    contact_score = compute_contact_method_score(
        mentee.get("contact_method", ""), mentor.get("contact_method", "")
    )

    total = (
        skill_score * 0.45
        + availability_score * 0.25
        + interest_score * 0.20
        + contact_score * 0.10
    )

    return MatchResult(
        entity_id=mentor["id"],
        total_score=round(min(total, 1.0), 4),
        skill_score=round(skill_score, 4),
        availability_score=round(availability_score, 4),
        interest_score=round(interest_score, 4),
        history_score=round(contact_score, 4),
        explanation=(
            f"Domaines {skill_score:.0%} · Dispo {availability_score:.0%} · "
            f"Spécialité {interest_score:.0%} · Contact {contact_score:.0%}"
        ),
    )