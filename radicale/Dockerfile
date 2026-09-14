FROM docker.io/library/python:3.12-slim
RUN pip install --no-cache-dir radicale passlib[bcrypt]
EXPOSE 5232
CMD ["python","-m","radicale","--config","/etc/radicale/config"]
