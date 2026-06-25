FROM python:3.12-slim

# stdout/stderr 버퍼링 비활성화 — 로그를 즉시 CloudWatch(awslogs)로 전송
ENV PYTHONUNBUFFERED=1

# ── OTel/ADOT (CloudWatch GenAI Observability) ──────────────────────────────
# opentelemetry-instrument가 trace/span/log를 OTLP로 직접 전송 → 막힌 stdout→awslogs 경로 우회.
# 이미지/env 레벨이라 EKS 전환에도 포터블(IAM만 IRSA로 remap).
ENV AGENT_OBSERVABILITY_ENABLED=true
ENV OTEL_PYTHON_DISTRO=aws_distro
ENV OTEL_PYTHON_CONFIGURATOR=aws_configurator
ENV OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
ENV OTEL_EXPORTER_OTLP_LOGS_HEADERS=x-aws-log-group=/ecs/farmily-agentcore,x-aws-log-stream=otel,x-aws-metric-namespace=Farmily/AgentCore
ENV OTEL_RESOURCE_ATTRIBUTES=service.name=farmily-agentcore

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

# AgentCore Runtime은 agent.py의 app 객체를 자동으로 찾음
# opentelemetry-instrument로 래핑 → Strands/Bedrock/도구/DB 호출 자동 계측
CMD ["opentelemetry-instrument", "python", "agent.py"]
