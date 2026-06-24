FROM python:3.12-slim

# stdout/stderr 버퍼링 비활성화 — 로그를 즉시 CloudWatch(awslogs)로 전송
ENV PYTHONUNBUFFERED=1

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
CMD ["python", "agent.py"]
