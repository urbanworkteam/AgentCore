"""
tools.py
기존 Action Group Lambda 5개를 Strands @tool 함수로 변환.
Lambda 이벤트 파싱(parse_params/ok) 제거 — 인자를 직접 받고 dict/str 반환.
"""
import json
import psycopg2.extras
from datetime import date
from strands import tool
from farmily_utils import get_connection, embed, vec_str, is_safe_input


# ── 1. 영농일지 조회 ───────────────────────────────────────────────────────
@tool
def get_diary(user_id: str, diary_id: str = "") -> str:
    """
    영농일지를 조회합니다.
    diary_id를 지정하면 해당 일지를, 없으면 가장 최근 일지를 반환합니다.
    반환 필드: found, diaryId, date, crop, weatherSummary, memo, workTypes
    """
    for val in [user_id, diary_id]:
        safe, reason = is_safe_input(val)
        if not safe:
            return json.dumps({"error": reason}, ensure_ascii=False)

    try:
        with get_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                if diary_id:
                    cur.execute("""
                        SELECT fd.id, fd.diary_date,
                               c.name AS crop,
                               fd.weather_main, fd.memo,
                               COALESCE(
                                 json_agg(DISTINCT dwb.work_type)
                                 FILTER (WHERE dwb.work_type IS NOT NULL), '[]'
                               ) AS work_types
                        FROM farm_diaries fd
                        LEFT JOIN crops c ON c.id = fd.crop_id
                        LEFT JOIN diary_work_blocks dwb ON dwb.diary_id = fd.id
                        WHERE fd.id = %s AND fd.user_id = %s
                          AND fd.deleted_at IS NULL
                        GROUP BY fd.id, c.name
                    """, (diary_id, user_id))
                else:
                    cur.execute("""
                        SELECT fd.id, fd.diary_date,
                               c.name AS crop,
                               fd.weather_main, fd.memo,
                               COALESCE(
                                 json_agg(DISTINCT dwb.work_type)
                                 FILTER (WHERE dwb.work_type IS NOT NULL), '[]'
                               ) AS work_types
                        FROM farm_diaries fd
                        LEFT JOIN crops c ON c.id = fd.crop_id
                        LEFT JOIN diary_work_blocks dwb ON dwb.diary_id = fd.id
                        WHERE fd.user_id = %s AND fd.deleted_at IS NULL
                        GROUP BY fd.id, c.name
                        ORDER BY fd.diary_date DESC
                        LIMIT 1
                    """, (user_id,))
                row = cur.fetchone()

        if not row:
            return json.dumps({"found": False, "message": "영농일지 없음"}, ensure_ascii=False)

        return json.dumps({
            "found":          True,
            "diaryId":        str(row["id"]),
            "date":           str(row["diary_date"]),
            "crop":           row["crop"] or "",
            "weatherSummary": row["weather_main"] or "",
            "memo":           row["memo"] or "",
            "workTypes":      row["work_types"] if isinstance(row["work_types"], list) else [],
        }, ensure_ascii=False)

    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ── 2. 작물 정보 조회 ──────────────────────────────────────────────────────
_MONTH_KO = ["", "1월", "2월", "3월", "4월", "5월", "6월",
             "7월", "8월", "9월", "10월", "11월", "12월"]

def _filter_health_claims(text: str) -> str:
    replacements = {
        "암을 예방":      "항산화 성분이 풍부",
        "면역력을 강화":  "건강한 식생활에 도움",
        "당뇨를 예방":   "혈당 관리에 관심 있는 분께 추천",
        "혈압을 낮춰":   "나트륨 배출에 도움을 주는 칼륨 함유",
        "치매를 예방":   "뇌 건강에 관심 있는 분께 추천",
        "다이어트에 효과": "식이섬유가 풍부",
    }
    for original, replacement in replacements.items():
        text = text.replace(original, replacement)
    return text

@tool
def get_crop_info(crop_name: str) -> str:
    """
    작물의 제철 정보, 영양 정보, 보관법, 조리법을 조회합니다.
    반환 필드: found, cropName, harvestMonths, inSeasonNow, nutritionBrief, storageMethod, cookingMethod
    """
    safe, reason = is_safe_input(crop_name)
    if not safe:
        return json.dumps({"error": reason}, ensure_ascii=False)

    try:
        with get_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT crop_name, category, harvest_months,
                           origin_region, cooking_method,
                           storage_method, nutrition_brief, effect_brief
                    FROM crop_knowledge
                    WHERE crop_name = %s
                """, (crop_name,))
                row = cur.fetchone()

        if not row:
            return json.dumps({"found": False, "cropName": crop_name}, ensure_ascii=False)

        harvest_months = row["harvest_months"] or []
        current_month  = date.today().month

        return json.dumps({
            "found":           True,
            "cropName":        row["crop_name"],
            "category":        row["category"] or "",
            "harvestMonths":   harvest_months,
            "harvestMonthsKo": [_MONTH_KO[m] for m in harvest_months if 1 <= m <= 12],
            "inSeasonNow":     current_month in harvest_months,
            "originRegion":    row["origin_region"] or "",
            "cookingMethod":   row["cooking_method"] or "",
            "storageMethod":   row["storage_method"] or "",
            "nutritionBrief":  row["nutrition_brief"] or "",
            "effectBrief":     _filter_health_claims(row["effect_brief"] or ""),
        }, ensure_ascii=False)

    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ── 3. 레시피 검색 ─────────────────────────────────────────────────────────
@tool
def search_recipe(crop_name: str, query: str = "") -> str:
    """
    작물과 관련된 레시피를 벡터 유사도로 검색합니다 (pgvector).
    반환 필드: count, recipes[]{recipeName, content, source, similarity}
    """
    if not query:
        query = crop_name + " 레시피"

    for val in [crop_name, query]:
        safe, reason = is_safe_input(val)
        if not safe:
            return json.dumps({"error": reason}, ensure_ascii=False)

    try:
        q_vec = embed(query)
        v_str = vec_str(q_vec)

        with get_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                if crop_name:
                    cur.execute("""
                        SELECT recipe_name, content, source,
                               1 - (embedding <=> %s::vector) AS similarity
                        FROM recipe_embeddings
                        WHERE crop_name = %s AND embedding IS NOT NULL
                        ORDER BY embedding <=> %s::vector
                        LIMIT 5
                    """, (v_str, crop_name, v_str))
                else:
                    cur.execute("""
                        SELECT recipe_name, content, source,
                               1 - (embedding <=> %s::vector) AS similarity
                        FROM recipe_embeddings
                        WHERE embedding IS NOT NULL
                        ORDER BY embedding <=> %s::vector
                        LIMIT 5
                    """, (v_str, v_str))
                rows = cur.fetchall()

        return json.dumps({
            "cropName": crop_name,
            "query":    query,
            "count":    len(rows),
            "recipes": [
                {
                    "recipeName": r["recipe_name"],
                    "content":    r["content"],
                    "source":     r["source"],
                    "similarity": float(r["similarity"]),
                }
                for r in rows
            ],
        }, ensure_ascii=False)

    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ── 4. 지역 특산물 검색 ────────────────────────────────────────────────────
@tool
def search_local_specialty(crop_name: str, query: str = "") -> str:
    """
    지역 특산물 스토리를 벡터 유사도로 검색합니다 (pgvector).
    반환 필드: count, specialties[]{localName, region, content, similarity}
    """
    if not query:
        query = crop_name + " 지역 특산물 스토리"

    for val in [crop_name, query]:
        safe, reason = is_safe_input(val)
        if not safe:
            return json.dumps({"error": reason}, ensure_ascii=False)

    try:
        q_vec = embed(query)
        v_str = vec_str(q_vec)

        with get_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT local_name, region, content,
                           1 - (embedding <=> %s::vector) AS similarity
                    FROM local_specialty
                    WHERE crop_name = %s
                    ORDER BY embedding <=> %s::vector
                    LIMIT 3
                """, (v_str, crop_name, v_str))
                rows = cur.fetchall()

        return json.dumps({
            "cropName": crop_name,
            "count":    len(rows),
            "specialties": [
                {
                    "localName":  r["local_name"] or "",
                    "region":     r["region"] or "",
                    "content":    r["content"],
                    "similarity": float(r["similarity"]),
                }
                for r in rows
            ],
        }, ensure_ascii=False)

    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ── 5. 콘텐츠 이력 조회 ──────────────────────────────────────────────────
@tool
def get_content_history(user_id: str, crop_name: str, limit: int = 10) -> str:
    """
    최근 콘텐츠 생성 이력과 사용된 각도를 조회합니다.
    각도 비율 규칙 적용을 위해 최근 10건 기준 usedAngles 목록을 반환합니다.
    반환 필드: count, history[]{jobId, angle, platform, createdAt}, usedAngles[]
    """
    for val in [user_id, crop_name]:
        safe, reason = is_safe_input(val)
        if not safe:
            return json.dumps({"error": reason}, ensure_ascii=False)

    limit = min(int(limit), 20)

    try:
        with get_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT cj.id, cj.platform, cj.created_at,
                           COALESCE(cr.meta->>'angle', '') AS angle
                    FROM content_jobs cj
                    LEFT JOIN content_results cr ON cr.job_id = cj.id
                    JOIN crops c ON c.id = cj.crop_id
                    WHERE cj.user_id = %s
                      AND c.name = %s
                      AND cj.status = 'DONE'
                    ORDER BY cj.created_at DESC
                    LIMIT %s
                """, (user_id, crop_name, limit))
                rows = cur.fetchall()

        history = [
            {
                "jobId":     str(r["id"]),
                "angle":     r["angle"] or "",
                "platform":  r["platform"] or "",
                "createdAt": str(r["created_at"]),
            }
            for r in rows
        ]
        used_angles = sorted({h["angle"] for h in history if h["angle"]})

        return json.dumps({
            "count":      len(history),
            "history":    history,
            "usedAngles": used_angles,
        }, ensure_ascii=False)

    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ── 6. 트렌드 검색 ────────────────────────────────────────────────────────
@tool
def search_trend(crop_name: str, query: str = "") -> str:
    """
    최근 7일간 수집된 트렌드 리포트를 벡터 유사도로 검색합니다.
    네이버 DataLab 검색량, YouTube 트렌딩, 만개의레시피 기반 Claude 요약 결과를 반환합니다.
    반환 필드: query, count, reports[]{date, source, content, similarity}
    """
    if not query:
        query = crop_name + " 트렌드"

    safe, reason = is_safe_input(query)
    if not safe:
        return json.dumps({"error": reason}, ensure_ascii=False)

    try:
        q_vec = embed(query)
        v_str = vec_str(q_vec)

        with get_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT report_date, source, content,
                           1 - (embedding <=> %s::vector) AS similarity
                    FROM trend_reports
                    WHERE report_date >= CURRENT_DATE - INTERVAL '7 days'
                      AND embedding IS NOT NULL
                    ORDER BY embedding <=> %s::vector
                    LIMIT 3
                """, (v_str, v_str))
                rows = cur.fetchall()

        return json.dumps({
            "query": query,
            "count": len(rows),
            "reports": [
                {
                    "date":       str(r["report_date"]),
                    "source":     r["source"],
                    "content":    r["content"],
                    "similarity": float(r["similarity"]),
                }
                for r in rows
            ],
        }, ensure_ascii=False)

    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
