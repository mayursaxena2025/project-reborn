FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
COPY static static
# single worker: decks are held in memory (see README)
CMD ["uvicorn","main:app","--host","0.0.0.0","--port","8000","--workers","1"]
