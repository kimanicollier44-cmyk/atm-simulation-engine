FROM python:3.12-slim
WORKDIR /app
COPY main.py .
COPY tests ./tests
CMD ["python", "main.py", "checks"]
