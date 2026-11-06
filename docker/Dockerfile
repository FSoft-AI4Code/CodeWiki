# Use Python 3.12 slim image as base
FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    git \
    curl \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# tree-sitter-language-pack 1.x downloads each grammar on first use. Fetch the
# ones the analyzers load at build time, so the image runs offline.
RUN python -c "import tree_sitter_language_pack as t; t.download(['dart', 'go', 'javascript', 'lean', 'typescript', 'vue', 'zig'])"

# Copy application code
COPY codewiki ./codewiki
COPY img ./img
COPY pyproject.toml .
COPY README.md .

# Create output directories
RUN mkdir -p output/cache output/temp output/docs output/dependency_graphs

# Set environment variables
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/ || exit 1

# Default command
CMD ["python", "codewiki/run_web_app.py", "--host", "0.0.0.0", "--port", "8000"]
