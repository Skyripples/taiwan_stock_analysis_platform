from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from api.services.auth import AuthError, create_user, list_users, login, logout, session, update_permissions

router = APIRouter(prefix="/auth", tags=["auth"])

class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)

class UserRequest(BaseModel):
    username: str
    password: str
    permissions: dict[str, bool] = {}

class PermissionsRequest(BaseModel):
    permissions: dict[str, bool]

def fail(exc: AuthError): raise HTTPException(exc.status, {"code": exc.code, "message": str(exc)})
def bearer(value: str | None) -> str:
    if not value or not value.startswith("Bearer "): raise HTTPException(401, {"code":"AUTH_REQUIRED","message":"請先登入"})
    return value[7:]

@router.post("/login")
def sign_in(payload: LoginRequest):
    try: return login(payload.username.strip(), payload.password)
    except AuthError as exc: fail(exc)

@router.get("/me")
def me(authorization: str | None = Header(default=None)):
    try: return session(bearer(authorization))
    except AuthError as exc: fail(exc)

@router.post("/logout", status_code=204)
def sign_out(authorization: str | None = Header(default=None)):
    logout(bearer(authorization))

@router.get("/users")
def users(authorization: str | None = Header(default=None)):
    try: return {"users": list_users(bearer(authorization))}
    except AuthError as exc: fail(exc)

@router.post("/users", status_code=201)
def add_user(payload: UserRequest, authorization: str | None = Header(default=None)):
    try: return create_user(bearer(authorization), payload.username, payload.password, payload.permissions)
    except AuthError as exc: fail(exc)

@router.put("/users/{username}/permissions")
def set_permissions(username: str, payload: PermissionsRequest, authorization: str | None = Header(default=None)):
    try: return update_permissions(bearer(authorization), username, payload.permissions)
    except AuthError as exc: fail(exc)
