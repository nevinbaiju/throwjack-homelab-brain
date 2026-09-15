FROM docker.io/library/python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Every module, not just app.py. config.yaml is policy data the workers reload
# at runtime; secrets come from env_file and are never baked into the image.
COPY app.py store.py triage.py project.py llm.py policy.py score.py auth.py caldav.py board.py upkeep.py escalate.py config.yaml upkeep.yaml ui.html login.html ./

# The shared agent contract, rendered into contexts/ at runtime by
# project.ensure_root_context(). Every project inherits it: Claude Code reads
# CLAUDE.md from the working directory AND every parent, and contexts/CLAUDE.md
# imports AGENTS.md. These used to exist only on the storage volume -- no
# history, and absent entirely for anyone who cloned this repo.
COPY contexts/ ./contexts/

EXPOSE 5010
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "5010", "--workers", "1"]
