# Farmily AgentCore

농부 소셜 미디어 콘텐츠 자동 생성 AI 에이전트. 영농일지 데이터를 기반으로 인스타그램 캡션, 해시태그, 카드 이미지용 텍스트를 생성합니다.

---

## 아키텍처

```
[Spring Boot 백엔드]
    └→ BedrockAgentCore Runtime (ECS Fargate, port 8080)
         └→ Strands Agent (claude-3-5-sonnet-v2 APAC)
              ├→ @tool get_diary            # 영농일지 조회
              ├→ @tool get_crop_info        # 작물 정보 조회
              ├→ @tool search_recipe        # 레시피 검색
              ├→ @tool search_local_specialty  # 지역 특산물 검색
              ├→ @tool get_content_history  # 콘텐츠 히스토리 조회
              └→ @tool search_trend         # 트렌드 검색
                   └→ farmily-card-renderer (Lambda) # 카드 이미지 생성
```

---

## 폴더 구조

```
farmily-agentcore/
├── agent.py              # 메인 엔트리포인트 (@app.entrypoint)
├── tools.py              # Strands @tool 함수 6개
├── farmily_utils.py      # DB 연결 등 공통 유틸
├── prompts/
│   ├── base_instruction.txt      # System Prompt 기본 지침
│   └── angles/                   # 콘텐츠 각도별 가이드
│       ├── harvest.txt           # 수확 현장
│       ├── nutrition.txt         # 영양 정보
│       ├── purchase.txt          # 구매 유도
│       ├── recipe.txt            # 레시피 활용
│       ├── regional.txt          # 지역 특산
│       ├── seasonal.txt          # 제철 강조
│       ├── storage.txt           # 보관법
│       └── storytelling.txt      # 농부 스토리
├── requirements.txt
├── Dockerfile
└── .github/
    └── workflows/
        └── deploy.yml            # CI/CD (PR 오픈 → CI, main 머지 → 빌드+ECS 배포)
```

---

## 주요 흐름

1. Spring Boot가 `BedrockAgentClient.invoke()` 로 AgentCore 호출
2. `handler()` 에서 DB 컨텍스트 조회 후 Strands Agent 실행
3. Agent가 @tool 함수들을 호출해 데이터 수집
4. Claude가 JSON 형식의 콘텐츠(caption, hashtags, textPool) 생성
5. `farmily-card-renderer` Lambda 호출 → 카드 이미지 S3 저장
6. DB에 결과 저장 후 `DONE(100%)` 상태 업데이트

---

## 진행 상태 (progress_pct)

| 단계 | 상태 | progress |
|------|------|----------|
| 요청 수신 | ANALYZING | 10% |
| Agent 초기화 완료 | ENRICHING | 30% |
| Agent 응답 완료 | GENERATING | 60% |
| DB 저장 완료 | GENERATING | 70% |
| 카드 렌더링 완료 | DONE | 100% |

---

## 환경변수

| 변수 | 설명 |
|------|------|
| `MODEL_ID` | Bedrock 모델 ID |
| `AWS_REGION` | AWS 리전 |
| `DB_HOST` / `DB_USER` / `DB_PASSWORD` | RDS 접속 정보 |
| `CARD_RENDERER_LAMBDA` | 카드 렌더러 Lambda 함수명 |
| `GUARDRAIL_ID` / `GUARDRAIL_VERSION` | Bedrock Guardrail |
| `AGENTCORE_MEMORY_ID` | AgentCore Memory ID (미설정 시 비활성) |
| `PYTHONUNBUFFERED` | `1` 고정 — CloudWatch 로그 즉시 전송 |

---

## 인프라

- **런타임**: AWS BedrockAgentCore (ECS Fargate, `prod-cluster`)
- **서비스 디스커버리**: Cloud Map (`farmily-agentcore.farmily.local`)
- **로그**: CloudWatch Logs `/ecs/farmily-agentcore`
- **모니터링**: CloudWatch 커스텀 메트릭 `Farmily/AgentCore` 네임스페이스
- **CI/CD**: GitHub Actions → ECR Push → ECS Rolling Deploy
