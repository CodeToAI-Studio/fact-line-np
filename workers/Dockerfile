FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# uvicorn is already in requirements.txt (FastAPI app in main.py). Start the
# FastAPI app whose lifespan launches the worker thread.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "${PORT:-8080}"]
