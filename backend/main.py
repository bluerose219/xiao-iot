import asyncio
import base64
import io
import json
import logging
import os
import re
import time
import uuid
import zipfile
from collections import deque
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_ANON_KEY = os.environ["SUPABASE_ANON_KEY"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
DEVICE_KEY = os.environ["DEVICE_KEY"]
DEVICE_ID = os.getenv("DEVICE_ID", "xiao-01")
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",")]
RETENTION_DAYS = float(os.getenv("RETENTION_DAYS", "7"))  # 이보다 오래된 센서 데이터는 자동 삭제
CLEANUP_INTERVAL_SEC = 3600
IMAGE_BUCKET = os.getenv("IMAGE_BUCKET", "ei-images")

log = logging.getLogger("uvicorn.error")

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
    headers = {"apikey": SUPABASE_SERVICE_KEY, "Content-Type": "application/json"}
    # legacy service_role 키(JWT)만 Bearer로 보낸다. 새 sb_secret_ 키는 apikey 헤더만으로 동작
    if SUPABASE_SERVICE_KEY.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {SUPABASE_SERVICE_KEY}"
    return headers


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


_user_cache: dict[str, tuple[float, dict]] = {}
USER_CACHE_SEC = 60  # 카메라 프레임처럼 잦은 요청마다 Supabase에 토큰을 확인하지 않도록 캐시


async def current_user(authorization: str = Header(default="")) -> dict:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="로그인이 필요합니다")
    cached = _user_cache.get(authorization)
    if cached and time.time() - cached[0] < USER_CACHE_SEC:
        return cached[1]
    resp = await client.get(
        f"{SUPABASE_URL}/auth/v1/user",
        headers={**_anon_headers(), "Authorization": authorization},
    )
    if not resp.is_success:
        _user_cache.pop(authorization, None)
        raise HTTPException(status_code=401, detail="토큰이 만료되었거나 유효하지 않습니다")
    if len(_user_cache) > 1000:
        _user_cache.clear()
    _user_cache[authorization] = (time.time(), resp.json())
    return _user_cache[authorization][1]


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


_last_cleanup = 0.0


async def cleanup_old_data() -> None:
    """RETENTION_DAYS보다 오래된 센서 데이터를 지운다. 기기 요청이 들어올 때 최대 1시간에 한 번 실행."""
    global _last_cleanup
    if RETENTION_DAYS <= 0 or time.monotonic() - _last_cleanup < CLEANUP_INTERVAL_SEC:
        return
    _last_cleanup = time.monotonic()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat()
    try:
        await _db("DELETE", "iot_sensor_data", params={"created_at": f"lt.{cutoff}"}, prefer="return=minimal")
        log.info("cleanup: deleted sensor rows older than %s", cutoff)
    except HTTPException as e:
        # 정리 실패가 기기 데이터 수신을 막으면 안 된다
        log.warning("cleanup failed: %s", e.detail)


@app.post("/device/data", dependencies=[Depends(device_auth)])
async def device_data(reading: SensorReading):
    """센서값을 저장하고, 응답으로 현재 LED 목표 상태를 돌려준다 (요청 1번으로 업/다운 처리)."""
    await _db("POST", "iot_sensor_data", json={"device_id": DEVICE_ID, **reading.model_dump()}, prefer="return=minimal")
    await cleanup_old_data()
    rows = await _db("GET", "iot_device_state", params={"device_id": f"eq.{DEVICE_ID}", "select": "led"})
    return {"led": bool(rows and rows[0]["led"])}


@app.get("/device/led", dependencies=[Depends(device_auth)])
async def device_led():
    rows = await _db("GET", "iot_device_state", params={"device_id": f"eq.{DEVICE_ID}", "select": "led"})
    return {"led": bool(rows and rows[0]["led"])}


# ---------- 카메라 / Edge Impulse 이미지 수집 ----------
# 기기가 최신 프레임을 계속 올리고(메모리 보관), 대시보드는 그 프레임을 보거나 촬영해 Storage에 저장한다.

FRAME_MAX_BYTES = 300_000
VIEWER_TIMEOUT_SEC = 15      # 대시보드가 이 시간 안에 프레임을 봤으면 기기가 빠르게 전송
FAST_INTERVAL_MS = 0
IDLE_INTERVAL_MS = 5000
LABEL_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


class FrameStore:
    def __init__(self) -> None:
        self.jpeg: bytes | None = None
        self.result: dict | None = None   # 추론 펌웨어가 보낸 프레임이면 추론 결과
        self.history: deque[dict] = deque(maxlen=500)
        self.seq = 0
        self.received_at = 0.0
        self.viewer_seen = 0.0
        self.cond = asyncio.Condition()

    async def put(self, jpeg: bytes, result: dict | None = None) -> None:
        async with self.cond:
            self.jpeg = jpeg
            self.result = result
            self.seq += 1
            self.received_at = time.time()
            if result is not None:
                self.history.append({"t": self.received_at, "seq": self.seq, **result})
            self.cond.notify_all()

    async def wait_newer(self, seq: int, timeout: float) -> bool:
        async with self.cond:
            try:
                await asyncio.wait_for(self.cond.wait_for(lambda: self.seq > seq), timeout)
                return True
            except asyncio.TimeoutError:
                return False

    def viewer_active(self) -> bool:
        return time.time() - self.viewer_seen < VIEWER_TIMEOUT_SEC


frames = FrameStore()


def _storage_url(path: str) -> str:
    return f"{SUPABASE_URL}/storage/v1/{path}"


async def _sign_urls(paths: list[str], expires: int = 3600) -> dict[str, str]:
    if not paths:
        return {}
    resp = await client.post(
        _storage_url(f"object/sign/{IMAGE_BUCKET}"),
        json={"expiresIn": expires, "paths": paths},
        headers=_service_headers(),
    )
    _raise_from(resp)
    return {
        item["path"]: _storage_url(item["signedURL"].lstrip("/"))
        for item in resp.json()
        if item.get("signedURL")
    }


@app.post("/device/frame", dependencies=[Depends(device_auth)])
async def device_frame(request: Request):
    """기기가 JPEG 원본(body)을 올린다. 응답의 interval_ms 만큼 쉬었다가 다음 프레임을 보낸다."""
    jpeg = await request.body()
    if not jpeg.startswith(b"\xff\xd8") or len(jpeg) > FRAME_MAX_BYTES:
        raise HTTPException(status_code=400, detail="invalid jpeg")
    await frames.put(jpeg)
    return {"interval_ms": FAST_INTERVAL_MS if frames.viewer_active() else IDLE_INTERVAL_MS}


@app.get("/api/camera/frame")
async def camera_frame(after: int = 0, user: dict = Depends(current_user)):
    """최신 프레임. after=<마지막으로 받은 seq>를 주면 새 프레임이 올 때까지 최대 5초 기다린다."""
    frames.viewer_seen = time.time()
    if frames.seq <= after and not await frames.wait_newer(after, 5):
        return Response(status_code=204)
    if frames.jpeg is None:
        return Response(status_code=204)
    return Response(
        content=frames.jpeg,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "no-store",
            "X-Frame-Seq": str(frames.seq),
            "X-Frame-Age": f"{time.time() - frames.received_at:.2f}",
            "Access-Control-Expose-Headers": "X-Frame-Seq, X-Frame-Age",
        },
    )


class CaptureRequest(BaseModel):
    label: str


@app.post("/api/captures")
async def capture(body: CaptureRequest, user: dict = Depends(current_user)):
    """촬영 요청 이후에 도착한 새 프레임을 저장한다 (연속 촬영 시 같은 프레임이 중복 저장되지 않음)."""
    label = body.label.strip()
    if not LABEL_RE.match(label):
        raise HTTPException(status_code=400, detail="라벨은 영문/숫자/_/- 만 사용 (최대 40자)")
    frames.viewer_seen = time.time()
    if not await frames.wait_newer(frames.seq, 8) or frames.jpeg is None:
        raise HTTPException(status_code=504, detail="카메라에서 새 프레임이 오지 않습니다")
    jpeg = frames.jpeg

    image_id = str(uuid.uuid4())
    path = f"{label}/{label}.{image_id[:8]}.jpg"   # Edge Impulse는 파일명 첫 '.' 앞을 라벨로 인식
    resp = await client.post(
        _storage_url(f"object/{IMAGE_BUCKET}/{path}"),
        content=jpeg,
        headers={**_service_headers(), "Content-Type": "image/jpeg"},
    )
    _raise_from(resp)
    rows = await _db(
        "POST",
        "iot_images",
        json={"id": image_id, "label": label, "path": path, "size": len(jpeg)},
        prefer="return=representation",
    )
    row = rows[0]
    row["url"] = (await _sign_urls([path])).get(path)
    return row


@app.get("/api/labels")
async def labels(user: dict = Depends(current_user)):
    rows = await _db("GET", "iot_images", params={"select": "label", "limit": "100000"})
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["label"]] = counts.get(r["label"], 0) + 1
    return {"labels": [{"label": k, "count": v} for k, v in sorted(counts.items())]}


@app.get("/api/images")
async def list_images(label: str | None = None, limit: int = 200, offset: int = 0, user: dict = Depends(current_user)):
    params = {"order": "created_at.desc", "limit": str(max(1, min(limit, 1000))), "offset": str(max(0, offset))}
    if label:
        params["label"] = f"eq.{label}"
    rows = await _db("GET", "iot_images", params=params)
    urls = await _sign_urls([r["path"] for r in rows])
    for r in rows:
        r["url"] = urls.get(r["path"])
    return {"images": rows}


class DeleteRequest(BaseModel):
    ids: list[str]


@app.post("/api/images/delete")
async def delete_images(body: DeleteRequest, user: dict = Depends(current_user)):
    if not body.ids:
        return {"deleted": 0}
    id_list = ",".join(str(uuid.UUID(i)) for i in body.ids)  # uuid 형식 검증
    rows = await _db("GET", "iot_images", params={"id": f"in.({id_list})", "select": "id,path"})
    if not rows:
        return {"deleted": 0}
    resp = await client.request(
        "DELETE",
        _storage_url(f"object/{IMAGE_BUCKET}"),
        json={"prefixes": [r["path"] for r in rows]},
        headers=_service_headers(),
    )
    _raise_from(resp)
    await _db("DELETE", "iot_images", params={"id": f"in.({id_list})"}, prefer="return=minimal")
    return {"deleted": len(rows)}


@app.get("/api/images/download")
async def download_images(label: str | None = None, user: dict = Depends(current_user)):
    """클래스별(또는 전체) ZIP. 폴더=라벨, 파일명=<label>.<id>.jpg 라서 Edge Impulse 업로더에 그대로 넣을 수 있다."""
    params = {"select": "path", "order": "created_at.asc", "limit": "100000"}
    if label:
        params["label"] = f"eq.{label}"
    rows = await _db("GET", "iot_images", params=params)
    if not rows:
        raise HTTPException(status_code=404, detail="이미지가 없습니다")

    sem = asyncio.Semaphore(8)

    async def fetch(path: str) -> tuple[str, bytes | None]:
        async with sem:
            r = await client.get(_storage_url(f"object/{IMAGE_BUCKET}/{path}"), headers=_service_headers())
            return path, r.content if r.is_success else None

    results = await asyncio.gather(*(fetch(r["path"]) for r in rows))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:   # JPEG은 이미 압축되어 있음
        for path, data in results:
            if data is not None:
                zf.writestr(path, data)
    name = f"{label or 'all'}_{datetime.now().strftime('%Y%m%d_%H%M')}.zip"
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}"', "Access-Control-Expose-Headers": "Content-Disposition"},
    )


# ---------- Edge Impulse 추론 결과 ----------
# 추론 펌웨어가 JPEG(body) + 결과(X-Result 헤더, JSON)를 함께 올린다. 기록은 메모리에만 최근 500개 보관.

def _parse_result(raw: str) -> dict:
    try:
        r = json.loads(raw)
        scores = {str(k): float(v) for k, v in r["scores"].items()}
    except (ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(status_code=400, detail="invalid X-Result")
    top = max(scores, key=scores.get)
    return {
        "label": str(r.get("label") or top),          # 임계값 미만이면 기기가 "uncertain"으로 보냄
        "top": top,
        "confidence": scores[top],
        "scores": scores,
        "dsp_ms": int(r.get("dsp", 0)),
        "nn_ms": int(r.get("nn", 0)),
        "loop_ms": int(r.get("loop", 0)),
    }


@app.post("/device/infer", dependencies=[Depends(device_auth)])
async def device_infer(request: Request, x_result: str = Header(default="")):
    jpeg = await request.body()
    if not jpeg.startswith(b"\xff\xd8") or len(jpeg) > FRAME_MAX_BYTES:
        raise HTTPException(status_code=400, detail="invalid jpeg")
    await frames.put(jpeg, _parse_result(x_result))
    return {"interval_ms": FAST_INTERVAL_MS if frames.viewer_active() else IDLE_INTERVAL_MS}


@app.get("/api/infer/live")
async def infer_live(after: int = 0, user: dict = Depends(current_user)):
    """최신 프레임(base64) + 그 프레임의 추론 결과. after 이후 새 프레임이 올 때까지 최대 5초 대기."""
    frames.viewer_seen = time.time()
    if frames.seq <= after and not await frames.wait_newer(after, 5):
        return Response(status_code=204)
    if frames.jpeg is None:
        return Response(status_code=204)
    return {
        "seq": frames.seq,
        "age": round(time.time() - frames.received_at, 2),
        "image": base64.b64encode(frames.jpeg).decode(),
        "result": frames.result,
    }


@app.get("/api/infer/history")
async def infer_history(limit: int = 200, user: dict = Depends(current_user)):
    items = list(frames.history)[-max(1, min(limit, 500)):]
    return {"now": time.time(), "history": items}


@app.get("/health")
async def health():
    return {"status": "ok"}
