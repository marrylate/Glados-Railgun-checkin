"""一次性：把旧仓库的 solver 密钥搬到新仓库（值不打印、不落日志）"""
import base64
import os
import sys

import requests
from nacl import encoding, public

API = "https://api.github.com"
DST = "marrylate/glados-daily-checkin"
pat = os.environ["PAT"].strip()
h = {"Authorization": "Bearer " + pat, "Accept": "application/vnd.github+json"}

payload = {}
if os.environ.get("SRC_SOLVER", "").strip():
    payload["GLADOS_SOLVER"] = os.environ["SRC_SOLVER"].strip()
if os.environ.get("SRC_KEY", "").strip():
    payload["SOLVER_API_KEY"] = os.environ["SRC_KEY"].strip()

if not payload:
    print("旧仓库没有 solver 相关 secret，跳过")
    sys.exit(0)

pk = requests.get(f"{API}/repos/{DST}/actions/secrets/public-key", headers=h, timeout=30).json()
pub = public.PublicKey(pk["key"].encode(), encoding.Base64Encoder)

for name, value in payload.items():
    sealed = public.SealedBox(pub).encrypt(value.encode())
    r = requests.put(
        f"{API}/repos/{DST}/actions/secrets/{name}",
        headers=h,
        json={"encrypted_value": base64.b64encode(sealed).decode(), "key_id": pk["key_id"]},
        timeout=30,
    )
    print(f"{name} -> {r.status_code}")
