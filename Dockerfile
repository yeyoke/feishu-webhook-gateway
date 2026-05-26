FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN useradd --create-home --shell /usr/sbin/nologin appuser

COPY app.py /app/app.py

EXPOSE 8008

USER appuser

CMD ["python", "app.py"]
