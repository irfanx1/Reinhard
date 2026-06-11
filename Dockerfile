FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y ffmpeg build-essential && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["python3", "-m", "bot"]
