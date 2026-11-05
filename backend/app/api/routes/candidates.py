import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import clear_session_cookie, current_candidate
from app.core.config import Settings, get_settings
from app.db.session import get_db
from app.models import Candidate
from app.models.candidate import EMAIL_UNIQUE_CONSTRAINT
from app.schemas.candidate import CandidateResponse, CandidateUpdate

logger = logging.getLogger(__name__)
# Candidates are created by POST /api/auth/register, never here.
router = APIRouter(prefix="/candidates", tags=["candidates"])

NOT_FOUND = "Candidate not found"


def get_candidate(
    candidate_id: uuid.UUID, current: Candidate = Depends(current_candidate)
) -> Candidate:
    """The candidate by id, only if it is the signed-in one. Anyone else's id is a 404, exactly
    like an id that does not exist (A4): a 403 would confirm that it exists."""
    if candidate_id != current.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    return current


@contextmanager
def candidate_write(db: Session) -> Iterator[None]:
    """Commits what the block wrote. A duplicate email is a 409; other constraint errors a 500."""
    try:
        with db.begin_nested():
            yield
        db.commit()
    except IntegrityError as exc:
        constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        if constraint in (EMAIL_UNIQUE_CONSTRAINT, "uq_candidates_email", "uq_candidates_email_active"):
            raise HTTPException(
                status.HTTP_409_CONFLICT, "A candidate with this email already exists"
            ) from None
        # The DB error text contains row values (personal data); log only the constraint.
        logger.error("Candidate write violated constraint %s", constraint)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not save candidate"
        ) from None


@router.get("/{candidate_id}", response_model=CandidateResponse)
def read_candidate(candidate: Candidate = Depends(get_candidate)) -> Candidate:
    return candidate


@router.patch("/{candidate_id}", response_model=CandidateResponse)
def update_candidate(
    body: CandidateUpdate,
    candidate: Candidate = Depends(get_candidate),
    db: Session = Depends(get_db),
) -> Candidate:
    with candidate_write(db):
        for field, value in body.model_dump(exclude_unset=True).items():
            setattr(candidate, field, value)
    return candidate


@router.delete("/{candidate_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_candidate(
    response: Response,
    candidate: Candidate = Depends(get_candidate),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> None:
    # Child tables use ON DELETE CASCADE, so this removes all candidate data, sessions included.
    with candidate_write(db):
        db.delete(candidate)
    clear_session_cookie(response, settings)
