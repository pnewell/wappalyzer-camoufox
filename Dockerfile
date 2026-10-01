FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app
COPY . /app

RUN pip install --no-cache-dir . \
    && python -m playwright install-deps firefox \
    && echo y | python -m camoufox fetch official/156.0.1-beta.32

ENTRYPOINT ["wappalyzer"]
