FROM python:3.12-slim

WORKDIR /app

# Node.js/npm are needed at install time by PythonMonkey (mermaid-parser-py).
RUN apt-get update && apt-get install -y --no-install-recommends \
    git nodejs npm \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Preload the grammars used by the analyzers for offline repository analysis.
RUN python -c "import tree_sitter_language_pack as t; t.download(['dart', 'go', 'javascript', 'lean', 'typescript', 'vue', 'zig'])"

COPY codewiki ./codewiki
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir --no-deps .

ENV PYTHONUNBUFFERED=1
ENV CODEWIKI_NO_KEYRING=1

# The target repository, including its generated docs, is mounted here.
WORKDIR /workspace
ENTRYPOINT ["python", "-m", "codewiki"]
CMD ["--help"]
