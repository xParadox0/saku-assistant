FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

COPY pyproject.toml README.md ./
COPY saku ./saku
RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 saku \
    && install -d -o saku -g saku -m 0700 /run/saku-parser
USER saku

CMD ["python", "-m", "saku", "demo"]
