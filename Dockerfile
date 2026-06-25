FROM python:3.12-slim

# stdout/stderr 버퍼링 비활성화 — 로그를 즉시 CloudWatch(awslogs)로 전송
ENV PYTHONUNBUFFERED=1

# ── OTel/ADOT (CloudWatch GenAI Observability) ──────────────────────────────
# opentelemetry-instrument가 Strands/Bedrock/@tool를 자동계측 → CloudWatch GenAI Observability로 전송.
# 이미지/env 레벨이라 EKS 전환에도 포터블(IAM만 IRSA로 remap; EKS도 CloudWatch로 통일됨).
ENV AGENT_OBSERVABILITY_ENABLED=true
ENV OTEL_PYTHON_DISTRO=aws_distro
ENV OTEL_PYTHON_CONFIGURATOR=aws_configurator
ENV OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
ENV OTEL_EXPORTER_OTLP_LOGS_HEADERS=x-aws-log-group=/ecs/farmily-agentcore,x-aws-log-stream=otel,x-aws-metric-namespace=Farmily/AgentCore
ENV OTEL_RESOURCE_ATTRIBUTES=service.name=farmily-agentcore
# 헬스체크(/ping)는 30초마다 호출돼 trace를 도배하므로 계측에서 제외 → 실제 에이전트 trace만 남김
ENV OTEL_PYTHON_EXCLUDED_URLS=ping,health
# psycopg2 자동계측이 ThreadedConnectionPool 연결을 즉시 깨뜨려(첫 쿼리에서 connection already closed)
# 모든 job이 죽었던 원인. 계측만 제외하면 DB 정상 + 벡터검색(%s::vector 문자열)도 무관하게 동작.
# 나머지(Strands/Bedrock/tool span·토큰)는 그대로 수집. (job122~125 'connection already closed'로 확정)
ENV OTEL_PYTHON_DISABLED_INSTRUMENTATIONS=psycopg2

WORKDIR /app

# 시스템 의존성 (psycopg2-binary용)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY farmily_utils.py .
COPY tools.py .
COPY agent.py .
COPY prompts/ prompts/

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8080/ping || exit 1

# AgentCore Runtime은 agent.py의 app 객체를 자동으로 찾음.
# OTel 래퍼 복원 — psycopg2 자동계측만 위 DISABLED_INSTRUMENTATIONS로 제외해 DB 깨짐을 회피.
# 이게 CloudWatch GenAI Observability(비런타임 에이전트)의 공식 경로.
CMD ["opentelemetry-instrument", "python", "agent.py"]
