# TASK-01 — Infrastructure Setup

**Feature:** Local development environment  
**Repo:** al-mawsuat-backend  
**Week:** 1

---

## Description

Set up the complete local development environment. This includes installing all system packages, Docker, Python 3.12, and creating the project folder structure. After this task everything is ready to write code.

Install the following system packages on Ubuntu/Debian:
- build-essential, libssl-dev, libffi-dev, libpq-dev, pkg-config
- git, curl, wget, unzip
- tesseract-ocr with Arabic (ara) and Urdu (urd) language packs
- libmupdf-dev, mupdf-tools
- Docker Engine + Docker Compose v2 from official Docker repository (not apt default)
- pyenv → Python 3.12.3
- nvm → Node 20 LTS (for frontend repo only)

Create the backend repo folder structure exactly as defined in the MVP plan:
```
al-mawsuat-backend/
├── app/
│   ├── api/
│   ├── agent/
│   ├── rag/
│   ├── pipeline/
│   ├── storage/
│   ├── models/
│   ├── core/
│   └── main.py
├── workers/
├── inference/
├── infra/
│   └── nginx/
├── tasks/          ← all task .md files live here
├── .env            ← copy from .env.example, never commit real values
├── .env.example
├── .gitignore
├── Dockerfile
├── Dockerfile.worker
└── requirements.txt
```

Create `.gitignore` containing at minimum:
```
.venv/
__pycache__/
*.pyc
.env
*.egg-info/
.pytest_cache/
```

Create Python virtual environment inside the repo and install all packages from `requirements.txt`.

`requirements.txt` must include:
```
fastapi==0.111.0
uvicorn[standard]==0.30.0
gunicorn==22.0.0
pydantic==2.7.0
pydantic-settings==2.2.0
python-multipart==0.0.9
sqlalchemy[asyncio]==2.0.30
asyncpg==0.29.0
alembic==1.13.1
fastapi-users[sqlalchemy]==13.0.0
python-jose[cryptography]==3.3.0
passlib[bcrypt]==1.7.4
langgraph==0.1.5
langchain-core==0.2.0
sentence-transformers==3.0.0
openai==1.30.0
langdetect==1.0.9
transformers==4.41.0
torch==2.3.0
qdrant-client==1.9.1
meilisearch==0.31.0
redis[hiredis]==5.0.4
minio==7.2.7
PyMuPDF==1.24.3
pytesseract==0.3.10
Pillow==10.3.0
pdfplumber==0.11.0
celery==5.4.0
flower==2.0.1
sentry-sdk[fastapi]==2.3.0
pytest==8.2.0
pytest-asyncio==0.23.6
httpx==0.27.0
black==24.4.2
ruff==0.4.4
```

---

## Acceptance criteria

- [ ] `python --version` returns `Python 3.12.x`
- [ ] `docker --version` returns `Docker version 24+`
- [ ] `docker compose version` returns `Docker Compose version v2+`
- [ ] `docker run hello-world` runs without sudo
- [ ] `tesseract --list-langs` output includes `ara` and `urd`
- [ ] `source .venv/bin/activate && python -c "import fastapi; import langgraph; import qdrant_client"` runs without error
- [ ] All folders in the structure above exist
- [ ] `.gitignore` exists and contains `.env` and `.venv/`
- [ ] `.env.example` exists with all variable names but no real values
