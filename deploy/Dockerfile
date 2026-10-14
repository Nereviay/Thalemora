FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml requirements.lock LICENSE THIRD_PARTY_NOTICES.md ./
COPY src ./src
RUN pip install --no-cache-dir -c requirements.lock .
ENV THALEMORA_DATA=/data PYTHONDONTWRITEBYTECODE=1
EXPOSE 8765
HEALTHCHECK --interval=20s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health',timeout=2)"
CMD ["thalemora", "serve", "--container"]
