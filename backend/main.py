import os
from datetime import datetime, timezone

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_ANON_KEY = os.environ["SUPABASE_ANON_KEY"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
DEVICE_KEY = os.environ["DEVICE_KEY"]
DEVICE_ID = os.getenv("DEVICE_ID", "xiao-01")
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",")]

app = FastAPI(title="XIAO ESP32S3 IoT API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

client = httpx.AsyncClient(timeout=10)


def _anon_headers() -> dict:
    return {"apikey": SUPABASE_ANON_KEY, "Content-Type": "application/json"}


def _service_headers() -> dict:
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }


def _raise_from(resp: httpx.Response) -> None:
    if resp.is_success:
        return
    try:
        body = resp.json()
        msg = body.get("msg") or body.get("error_description") or body.get("message") or str(body)
    except ValueError:
        msg = resp.text
    raise HTTPException(status_code=resp.status_code, detail=msg)


async def _db(method: str, table: str, params: dict | None = None, json=None, prefer: str | None = None):
    headers = _service_headers()
    if prefer:
        headers["Prefer"] = prefer
    resp = await client.request(method, f"{SUPABASE_URL}/rest/v1/{table}", params=params, json=json, headers=headers)
    _raise_from(resp)
    return resp.json() if resp.content else None


# ---------- 인증 ----------

class Credentials(BaseModel):
    email: str
    password: str


async def current_user(authorization: str = Header(default="")) -> dict:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="로그인이 필요합니다")
    resp = await client.get(
        f"{SUPABASE_URL}/auth/v1/user",
        headers={**_anon_headers(), "Authorization": authorization},
    )
    if not resp.is_success:
        raise HTTPException(status_code=401, detail="토큰이 만료되었거나 유효하지 않습니다")
    return resp.json()


@app.post("/auth/signup")
async def signup(body: Credentials):
    resp = await client.post(f"{SUPABASE_URL}/auth/v1/signup", json=body.model_dump(), headers=_anon_headers())
    _raise_from(resp)
    data = resp.json()
    # 이메일 확인이 꺼져 있으면 바로 세션이 돌아온다
    return {
        "email": body.email,
        "access_token": data.get("access_token"),
        "needs_confirmation": data.get("access_token") is None,
    }


@app.post("/auth/login")
async def login(body: Credentials):
    resp = await client.post(
        f"{SUPABASE_URL}/auth/v1/token",
        params={"grant_type": "password"},
        json=body.model_dump(),
        headers=_anon_headers(),
    )
    _raise_from(resp)
    data = resp.json()
    return {"access_token": data["access_token"], "email": data["user"]["email"]}


@app.get("/auth/me")
async def me(user: dict = Depends(current_user)):
    return {"id": user["id"], "email": user["email"]}


# ---------- 대시보드용 API (로그인 필요) ----------

class LedCommand(BaseModel):
    on: bool


@app.get("/api/sensors")
async def sensors(limit: int = 60, user: dict = Depends(current_user)):
    limit = max(1, min(limit, 500))
    rows = await _db(
        "GET",
        "iot_sensor_data",
        params={"device_id": f"eq.{DEVICE_ID}", "order": "created_at.desc", "limit": str(limit)},
    )
    return {"device_id": DEVICE_ID, "data": rows}


@app.get("/api/led")
async def get_led(user: dict = Depends(current_user)):
    rows = await _db("GET", "iot_device_state", params={"device_id": f"eq.{DEVICE_ID}"})
    if not rows:
        return {"device_id": DEVICE_ID, "on": False, "updated_at": None}
    return {"device_id": DEVICE_ID, "on": rows[0]["led"], "updated_at": rows[0]["updated_at"]}


@app.post("/api/led")
async def set_led(cmd: LedCommand, user: dict = Depends(current_user)):
    now = datetime.now(timezone.utc).isoformat()
    await _db(
        "POST",
        "iot_device_state",
        params={"on_conflict": "device_id"},
        json={"device_id": DEVICE_ID, "led": cmd.on, "updated_at": now},
        prefer="resolution=merge-duplicates",
    )
    return {"device_id": DEVICE_ID, "on": cmd.on, "updated_at": now}


# ---------- ESP32 디바이스용 API (X-Device-Key) ----------

class SensorReading(BaseModel):
    temperature: float | None = None
    analog: int | None = None
    rssi: int | None = None


def device_auth(x_device_key: str = Header(default="")) -> None:
    if x_device_key != DEVICE_KEY:
        raise HTTPException(status_code=401, detail="invalid device key")


@app.post("/device/data", dependencies=[Depends(device_auth)])
async def device_data(reading: SensorReading):
    """센서값을 저장하고, 응답으로 현재 LED 목표 상태를 돌려준다 (요청 1번으로 업/다운 처리)."""
    await _db("POST", "iot_sensor_data", json={"device_id": DEVICE_ID, **reading.model_dump()}, prefer="return=minimal")
    rows = await _db("GET", "iot_device_state", params={"device_id": f"eq.{DEVICE_ID}", "select": "led"})
    return {"led": bool(rows and rows[0]["led"])}


@app.get("/device/led", dependencies=[Depends(device_auth)])
async def device_led():
    rows = await _db("GET", "iot_device_state", params={"device_id": f"eq.{DEVICE_ID}", "select": "led"})
    return {"led": bool(rows and rows[0]["led"])}


@app.get("/health")
async def health():
    return {"status": "ok"}
