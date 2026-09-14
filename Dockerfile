FROM docker.io/library/python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Every module, not just app.py. config.yaml is policy data the workers reload
# at runtime; secrets come from env_file and are never baked into the image.
COPY app.py store.py triage.py project.py llm.py policy.py score.py auth.py caldav.py board.py upkeep.py escalate.py config.yaml upkeep.yaml ui.html login.html ./

EXPOSE 5010
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "5010", "--workers", "1"]
