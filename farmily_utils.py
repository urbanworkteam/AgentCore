"""
farmily_utils.py  (AgentCore 버전)
Action Group 헬퍼(ok/parse_params/error)를 제거하고
DB 연결 · 임베딩 · 입력 안전 필터만 유지
"""
import os
import re
import json
import boto3
import psycopg2
import psycopg2.extras
from psycopg2 import pool
from contextlib import contextmanager

# ── DB 커넥션 풀 ───────────────────────────────────────────────────────────
DB_CONFIG = {
    "host":            os.environ["DB_HOST"],
    "port":            int(os.environ.get("DB_PORT", 5432)),
    "dbname":          os.environ["DB_NAME"],
    "user":            os.environ["DB_USER"],
    "password":        os.environ["DB_PASSWORD"],
    "connect_timeout": 10,
}

_pool = pool.ThreadedConnectionPool(minconn=1, maxconn=5, **DB_CONFIG)

@contextmanager
def get_connection():
    # psycopg2 풀은 빌려줄 때 연결을 검증하지 않는다. 컨테이너가 오래 떠 있으면
    # RDS/네트워크가 idle 연결을 끊어 "connection already closed"가 난다.
    # 빌릴 때 SELECT 1로 살아있는지 확인하고, 죽었으면 폐기 후 새 연결로 교체한다.
    conn = None
    for _ in range(3):
        candidate = _pool.getconn()
        try:
            if candidate.closed:
                raise psycopg2.OperationalError("connection closed")
            with candidate.cursor() as cur:
                cur.execute("SELECT 1")
            conn = candidate
            break
        except psycopg2.Error:
            _pool.putconn(candidate, close=True)  # 죽은 연결 폐기 → 풀이 새로 채움
    if conn is None:
        raise psycopg2.OperationalError("DB 연결 확보 실패 (풀의 모든 연결이 죽음)")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)


# ── Bedrock 임베딩 ─────────────────────────────────────────────────────────
REGION   = os.environ.get("AWS_REGION", "ap-northeast-2")
_bedrock = boto3.client("bedrock-runtime", region_name=REGION)

def embed(text: str, dimensions: int = 1024) -> list:
    resp = _bedrock.invoke_model(
        modelId      = "amazon.titan-embed-text-v2:0",
        body         = json.dumps({"inputText": text, "dimensions": dimensions, "normalize": True}),
        contentType  = "application/json",
        accept       = "application/json",
    )
    return json.loads(resp["body"].read())["embedding"]

def vec_str(embedding: list) -> str:
    return "[" + ",".join(str(v) for v in embedding) + "]"


# ── 입력 안전 필터 ─────────────────────────────────────────────────────────
_BLOCKED_PATTERNS = [
    r"(인종|민족|종교|성별|장애|나이|국적).{0,10}(차별|비하|혐오|모욕)",
    r"(죽여|죽이|폭탄|테러|살인|자살|자해)",
    r"(암|당뇨|고혈압|치매|코로나|바이러스).{0,10}(치료|예방|완치|억제|박멸)",
    r"(면역력|혈당|혈압).{0,5}(강화|개선|치료|정상화)",
    r"(100%\s*천연|무농약\s*보장|유기농\s*인증(?!받은|획득|보유))",
    r"(특허|인증|허가).{0,5}(받은척|위조|가짜)",
    r"(마약|도박|불법|음란|포르노|성인용)",
]
_COMPILED = [re.compile(p, re.IGNORECASE) for p in _BLOCKED_PATTERNS]

_FORBIDDEN_HEALTH_CLAIMS = [
    "면역력을 강화", "면역력 강화", "암을 예방", "당뇨를 치료",
    "혈압을 낮춰", "다이어트에 효과", "살을 빼", "체중 감량 효과",
    "치매 예방", "노화 방지 효과",
]

def is_safe_input(text: str) -> tuple:
    if not text or not text.strip():
        return True, ""
    for pattern in _COMPILED:
        if pattern.search(text):
            return False, f"정책 위반 패턴 감지: {pattern.pattern}"
    for claim in _FORBIDDEN_HEALTH_CLAIMS:
        if claim in text:
            return False, f"허위 의학적 효능 주장 감지: {claim}"
    if len(text) > 500:
        return False, "입력값이 너무 깁니다 (최대 500자)"
    return True, ""

def sanitize_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"[^\w\s가-힣.,!?()#@\-\n]", "", text)
    return text.strip()
