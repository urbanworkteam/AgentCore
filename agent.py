"""
agent.py  —  Farmily AgentCore 메인 엔트리포인트

아키텍처:
  [클라이언트]
      └→ AgentCore Runtime (ECS 컨테이너)
           └→ Strands Agent  (apac.claude-3-5-sonnet-v2)
                ├→ @tool get_diary
                ├→ @tool get_crop_info
                ├→ @tool search_recipe
                ├→ @tool search_local_specialty
                └→ @tool get_content_history

기존 Lambda 대비 변경점:
  - farmily-generate-content Lambda (오케스트레이터) → @app.entrypoint
  - bedrock-agent-runtime.invoke_agent()            → strands Agent()
  - Action Group Lambda 5개                         → @tool 함수 (tools.py)
  - bedrock-agent-runtime VPC Endpoint              → 불필요 (bedrock-runtime만 사용)
"""
import json
import os
import random
import re

import boto3
import psycopg2.extras
from bedrock_agentcore import BedrockAgentCoreApp as BedrockAgentCore
from strands import Agent
from strands.models import BedrockModel

from farmily_utils import get_connection, sanitize_text
from tools import (
    get_diary,
    get_crop_info,
    search_recipe,
    search_local_specialty,
    get_content_history,
    search_trend,
)

# ── 설정 ───────────────────────────────────────────────────────────────────
REGION               = os.environ.get("AWS_REGION", "ap-northeast-2")
MODEL_ID             = os.environ.get("MODEL_ID", "apac.anthropic.claude-3-5-sonnet-20241022-v2:0")
MEMORY_ID            = os.environ.get("AGENTCORE_MEMORY_ID", "")  # 미설정 시 Memory 비활성화
CARD_RENDERER_LAMBDA = os.environ.get("CARD_RENDERER_LAMBDA", "farmily-card-renderer")
GUARDRAIL_ID         = os.environ.get("GUARDRAIL_ID", "")
GUARDRAIL_VERSION    = os.environ.get("GUARDRAIL_VERSION", "1")

_lambda_client = boto3.client("lambda", region_name=REGION)

_INSTRUCTION_PATH = os.path.join(os.path.dirname(__file__), "agent_instruction.txt")
with open(_INSTRUCTION_PATH, encoding="utf-8") as f:
    SYSTEM_PROMPT = f.read()

# BedrockModel 클라이언트는 컨테이너 기동 시 1회만 생성
_model_kwargs = dict(model_id=MODEL_ID, region_name=REGION)
if GUARDRAIL_ID:
    _model_kwargs.update(
        guardrail_id=GUARDRAIL_ID,
        guardrail_version=GUARDRAIL_VERSION,
        guardrail_trace="enabled",
    )
_model = BedrockModel(**_model_kwargs)

app = BedrockAgentCore()

_TOOLS = [get_diary, get_crop_info, search_recipe,
          search_local_specialty, get_content_history, search_trend]


def _make_session_manager(user_id: str, job_id: int):
    """AGENTCORE_MEMORY_ID 설정 시 Memory 세션 매니저 반환, 미설정 시 None."""
    if not MEMORY_ID:
        return None
    from bedrock_agentcore.memory.integrations.strands.config import (
        AgentCoreMemoryConfig, RetrievalConfig,
    )
    from bedrock_agentcore.memory.integrations.strands.session_manager import (
        AgentCoreMemorySessionManager,
    )
    return AgentCoreMemorySessionManager(
        agentcore_memory_config=AgentCoreMemoryConfig(
            memory_id        = MEMORY_ID,
            session_id       = f"job-{job_id}",
            actor_id         = user_id,
            retrieval_config = {
                "/preferences/{actorId}": RetrievalConfig(top_k=5,  relevance_score=0.6),
                "/facts/{actorId}":       RetrievalConfig(top_k=10, relevance_score=0.3),
            },
        ),
        region_name=REGION,
    )


# ── AgentCore 엔트리포인트 ─────────────────────────────────────────────────
@app.entrypoint
def handler(payload, context):
    # 1. 입력 파싱
    if isinstance(payload.get("body"), str):
        body = json.loads(payload["body"])
    elif "body" in payload:
        body = payload["body"] or {}
    else:
        body = payload

    user_id       = str(body.get("userId", "")).strip()
    platform      = body.get("platform", "INSTAGRAM").upper()
    diary_ids     = [int(x) for x in body.get("diaryIds") or []]
    keywords      = sanitize_text(body.get("keywords", ""))
    photo_s3_keys = body.get("photoS3Keys") or []
    job_id        = int(body.get("jobId", 0))

    if not user_id:
        return {"error": "userId 필수"}
    if not job_id:
        return {"error": "jobId 필수"}

    # 2. DB 컨텍스트 조회 (crop_id, crop_name, region 등)
    ctx = _fetch_context(user_id, diary_ids)
    if "error" in ctx:
        return ctx

    # 3. 백엔드 job_id 사용 (agentcore 자체 job 생성 없음)
    _update_job(job_id, "ANALYZING", 10)

    try:
        # 4. Strands Agent 호출
        #    요청마다 새 Agent 인스턴스 → 대화 히스토리 격리
        #    AGENTCORE_MEMORY_ID 설정 시 Memory 자동 연동
        agent_kwargs = dict(model=_model, tools=_TOOLS, system_prompt=SYSTEM_PROMPT)
        session_mgr  = _make_session_manager(user_id, job_id)
        if session_mgr:
            agent_kwargs["session_manager"] = session_mgr
        agent = Agent(**agent_kwargs)

        agent_input = json.dumps({
            "userId":        user_id,
            "platform":      platform.lower(),
            "cropName":      ctx["crop_name"],
            "region":        ctx["region"],
            "farmingMethod": ctx["farming_method"],
            "handle":        ctx["handle"],
            "diaryIds":      diary_ids,
            "keywords":      keywords,
        }, ensure_ascii=False)

        response = agent(agent_input)
        raw_text = str(response)
        _update_job(job_id, "GENERATING", 60)

        # 5. JSON 파싱
        parsed = _parse_response(raw_text)
        if parsed.get("reasoning") == "JSON 파싱 실패 — fallback":
            raise ValueError(f"응답 파싱 실패. raw={raw_text[:200]}")

        # 6. DB 저장
        caption  = parsed.get("instagramCaption", "")
        hashtags = parsed.get("hashtags", [])
        cards    = parsed.get("textPool", {})
        meta     = {k: v for k, v in parsed.items()
                    if k not in ("instagramCaption", "hashtags")}
        _save_result(job_id, caption, hashtags, meta)
        _update_job(job_id, "GENERATING", 70)

        # 7. farmily-card-renderer 동기 호출 → 카드 키 획득 후 DB 업데이트
        template_type = random.choice(['template_a', 'template_b', 'template_c'])
        card_keys = []
        try:
            card_keys = _invoke_card_renderer(job_id, user_id, cards, photo_s3_keys, template_type)
            _update_card_image_keys(job_id, card_keys)
        except Exception as render_exc:
            print(f"[WARN] card-renderer 호출 실패 (job_id={job_id}): {render_exc}")
        _update_job(job_id, "DONE", 100)

        return {
            "jobId":        job_id,
            "status":       "DONE",
            "caption":      caption,
            "hashtags":     hashtags,
            "textPool":     cards,
            "angle":        parsed.get("angle", ""),
            "contentType":  parsed.get("contentType", ""),
            "reasoning":    parsed.get("reasoning", ""),
            "cardImageKeys": card_keys,
        }

    except Exception as exc:
        _fail_job(job_id, str(exc))
        return {"error": str(exc), "jobId": job_id}


# ── DB 헬퍼 ───────────────────────────────────────────────────────────────
def _fetch_context(user_id: str, diary_ids: list) -> dict:
    try:
        with get_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT u.id,
                           u.handle,
                           fp.region,
                           fp.farming_method
                    FROM users u
                    LEFT JOIN farm_profiles fp ON fp.user_id = u.id
                    WHERE u.id = %s AND u.deleted_at IS NULL
                """, (user_id,))
                user = cur.fetchone()
                if not user:
                    return {"error": f"userId {user_id} 없음"}

                if diary_ids:
                    cur.execute("""
                        SELECT c.id, c.name FROM crops c
                        JOIN farm_diaries fd ON fd.crop_id = c.id
                        WHERE fd.id = ANY(%s) AND fd.user_id = %s
                          AND fd.deleted_at IS NULL AND c.deleted_at IS NULL
                        LIMIT 1
                    """, (diary_ids, user_id))
                else:
                    cur.execute("""
                        SELECT id, name FROM crops
                        WHERE user_id = %s AND deleted_at IS NULL
                        ORDER BY id LIMIT 1
                    """, (user_id,))
                crop = cur.fetchone()

        handle_raw = user["handle"] or ""
        handle = f"@{handle_raw}" if handle_raw and not handle_raw.startswith("@") else handle_raw

        return {
            "region":         user["region"] or "",
            "farming_method": user["farming_method"] or "",
            "handle":         handle,
            "crop_id":        crop["id"]   if crop else None,
            "crop_name":      crop["name"] if crop else "작물",
        }
    except Exception as exc:
        return {"error": str(exc)}



def _update_job(job_id: int, status: str, pct: int):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE content_jobs
                SET status = %s,
                    progress_pct = %s,
                    done_at = CASE WHEN %s = 'DONE' THEN now() ELSE done_at END
                WHERE id = %s
            """, (status, pct, status, job_id))
            conn.commit()


def _fail_job(job_id: int, reason: str):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE content_jobs
                SET status = 'FAILED', failure_reason = %s
                WHERE id = %s
            """, (reason[:500], job_id))
            conn.commit()


def _save_result(job_id, caption, hashtags, meta):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO content_results
                  (job_id, card_image_keys, caption, hashtags, meta)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (job_id) DO UPDATE
                  SET caption  = EXCLUDED.caption,
                      hashtags = EXCLUDED.hashtags,
                      meta     = EXCLUDED.meta
            """, (job_id, [], caption, hashtags,
                  json.dumps(meta, ensure_ascii=False)))
            conn.commit()


def _update_card_image_keys(job_id: int, keys: list):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE content_results
                SET card_image_keys = %s
                WHERE job_id = %s
            """, (keys, job_id))
            conn.commit()


# ── 응답 파싱 ──────────────────────────────────────────────────────────────
def _fix_json_literals(s: str) -> str:
    result, in_string, escape = [], False, False
    for ch in s:
        if escape:
            result.append(ch); escape = False
        elif ch == "\\":
            result.append(ch); escape = True
        elif ch == '"':
            in_string = not in_string; result.append(ch)
        elif in_string and ch == "\n":
            result.append("\\n")
        elif in_string and ch == "\r":
            result.append("\\r")
        elif in_string and ch == "\t":
            result.append("\\t")
        else:
            result.append(ch)
    return "".join(result)

def _parse_response(raw: str) -> dict:
    m = re.search(r"\{[\s\S]+\}", raw)
    if m:
        candidate = m.group(0)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
        try:
            return json.loads(_fix_json_literals(candidate))
        except json.JSONDecodeError:
            pass
    return {"reasoning": "JSON 파싱 실패 — fallback", "instagramCaption": raw}


# ── Lambda 호출 헬퍼 ──────────────────────────────────────────────────────
def _invoke_card_renderer(job_id: int, user_id: str, text_pool: dict, photo_s3_keys: list,
                          template_type: str = "template_b") -> list:
    """카드 렌더링 Lambda 동기 호출 → 생성된 카드 S3 키 목록 반환."""
    if not photo_s3_keys:
        return []
    payload = json.dumps({
        "textPool":     text_pool,
        "templateType": template_type,
        "userId":       user_id,
        "jobId":        str(job_id),
        "photoS3Keys":  photo_s3_keys,
    })
    resp = _lambda_client.invoke(
        FunctionName   = CARD_RENDERER_LAMBDA,
        InvocationType = "RequestResponse",
        Payload        = payload.encode(),
    )
    result = json.loads(resp["Payload"].read())
    body = json.loads(result.get("body", "{}")) if isinstance(result.get("body"), str) else result
    return body.get("keys", [])




if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
