from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from api.services.auth import AuthError, begin, complete, logout, session

router = APIRouter(prefix="/auth", tags=["auth"])

class BeginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)

class CompleteRequest(BaseModel):
    flow_id: str
    credential: dict

def fail(exc: AuthError):
    raise HTTPException(exc.status, {"code": exc.code, "message": str(exc)})

def bearer(value: str | None) -> str:
    if not value or not value.startswith("Bearer "): raise HTTPException(401, {"code":"AUTH_REQUIRED","message":"請先登入"})
    return value[7:]

@router.post("/login/begin")
def login_begin(payload: BeginRequest):
    try: return begin(payload.username.strip(), payload.password)
    except AuthError as exc: fail(exc)

@router.post("/login/complete")
def login_complete(payload: CompleteRequest):
    try: return complete(payload.flow_id, payload.credential)
    except AuthError as exc: fail(exc)

@router.get("/me")
def me(authorization: str | None = Header(default=None)):
    try: return session(bearer(authorization))
    except AuthError as exc: fail(exc)

@router.post("/logout", status_code=204)
def sign_out(authorization: str | None = Header(default=None)):
    logout(bearer(authorization))
