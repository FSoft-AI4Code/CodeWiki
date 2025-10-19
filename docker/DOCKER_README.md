# CodeWiki Docker Setup

This document explains how to run CodeWiki using Docker and Docker Compose.

## Overview

The Docker setup provides a containerized environment for running the CodeWiki web application, which allows you to generate documentation for GitHub repositories through a web interface.

## File Structure

All Docker-related files are located in the `docker/` directory:

```
docker/
├── Dockerfile           # Container image definition
├── docker-compose.yml   # Service orchestration
├── env.example          # Environment variables template
└── DOCKER_README.md     # This file
```

The Dockerfile builds from the project root context to include all necessary application code.

---

## Quick Start

### 1. Clone the Repository

```bash
git clone <repository-url>
cd CodeWiki
```

### 2. Set Up Environment Variables

```bash
# Copy the example environment file
cp docker/env.example .env

# Edit .env file with your configuration
nano .env  # or use your preferred editor
```

Required configuration in `.env`:

```bash
# LLM API Configuration
MAIN_MODEL=claude-sonnet-4
FALLBACK_MODEL_1=glm-4p5
CLUSTER_MODEL=claude-sonnet-4
LLM_BASE_URL=https://api.anthropic.com  # or your LiteLLM proxy
LLM_API_KEY=your-api-key-here

# Application Port
APP_PORT=8000

# Optional: Logfire Configuration (for monitoring)
LOGFIRE_TOKEN=
LOGFIRE_PROJECT_NAME=codewiki
LOGFIRE_SERVICE_NAME=codewiki
```

### 3. Create Docker Network

```bash
docker network create codewiki-network
```

### 4. Start the Services

**Option A: From project root**
```bash
docker-compose -f docker/docker-compose.yml up -d
```

**Option B: From docker directory**
```bash
cd docker
docker-compose up -d
```

### 5. Access the Application

- Web Application: http://localhost:8000

The application will be available at the port specified in your `.env` file (default: 8000).

---

## Docker Compose Configuration

The `docker-compose.yml` file defines the CodeWiki service with the following features:

### Service Configuration

- **Image**: `codewiki:0.0.1`
- **Build Context**: Parent directory (`.` relative to docker/)
- **Container Name**: `codewiki`
- **Port Mapping**: `${APP_PORT:-8000}:8000`
- **Network**: `codewiki-network` (external)

### Environment Variables

The container uses environment variables from the `.env` file:
- `PYTHONPATH=/app/src` - Set Python module path
- `PYTHONUNBUFFERED=1` - Enable real-time logging
- All variables from `.env` file

### Volume Mounts

The following directories are mounted as volumes:

```yaml
volumes:
  - ./output:/app/output        # Persistent storage for generated docs
  - ~/.ssh:/root/.ssh:ro        # SSH keys for private repos (read-only)
```

**Note**: Git credentials can be mounted if needed for private repositories:
```yaml
  # Uncomment in docker-compose.yml if needed
  - ~/.gitconfig:/root/.gitconfig:ro
```

### Health Check

The service includes a health check that:
- Runs every 30 seconds
- Times out after 10 seconds
- Retries 3 times on failure
- Starts checking after 20 seconds

### Restart Policy

The container is set to restart automatically unless explicitly stopped (`restart: unless-stopped`).

---

## Dockerfile Details

The Dockerfile (`docker/Dockerfile`) builds the CodeWiki image with:

### Base Image
- Python 3.12 slim image for smaller size

### System Dependencies
- `git` - For repository cloning
- `curl` - For health checks
- `nodejs` and `npm` - For mermaid diagram validation

### Application Setup
1. Copies `requirements.txt` first (for better caching)
2. Installs Python dependencies
3. Copies entire application code
4. Creates output directories:
   - `output/cache`
   - `output/temp`
   - `output/docs`
   - `output/dependency_graphs`

### Runtime Configuration
- **Working Directory**: `/app`
- **Exposed Port**: `8000`
- **Entry Point**: `python codewiki/run_web_app.py --host 0.0.0.0 --port 8000`

---

## Common Operations

### View Logs

```bash
# From project root
docker-compose -f docker/docker-compose.yml logs -f

# From docker directory
cd docker
docker-compose logs -f

# View specific service
docker logs codewiki -f
```

### Stop Services

```bash
# From project root
docker-compose -f docker/docker-compose.yml stop

# From docker directory
cd docker
docker-compose stop
```

### Stop and Remove Containers

```bash
# From project root
docker-compose -f docker/docker-compose.yml down

# From docker directory
cd docker
docker-compose down

# Remove volumes as well
docker-compose down -v
```

### Rebuild Image

If you've made changes to the code or Dockerfile:

```bash
# From project root
docker-compose -f docker/docker-compose.yml build --no-cache

# From docker directory
cd docker
docker-compose build --no-cache

# Rebuild and restart
docker-compose up -d --build
```

### Access Container Shell

```bash
docker exec -it codewiki /bin/bash
```

---

## Persistent Storage

### Output Directory

The `output/` directory is mounted as a volume, ensuring generated documentation persists across container restarts:

```
output/
├── cache/                    # Cached dependency graphs and jobs
├── docs/                     # Generated documentation
├── dependency_graphs/        # JSON dependency graphs
└── temp/                     # Temporary files
```

### SSH Keys

If you need to clone private repositories, ensure your SSH keys are available:

```bash
# Verify SSH keys are accessible
ls -la ~/.ssh/

# The docker-compose.yml mounts ~/.ssh as read-only
```

---

## Troubleshooting

### Port Already in Use

If port 8000 is already in use:

```bash
# Change APP_PORT in .env file
echo "APP_PORT=8001" >> .env

# Restart services
docker-compose -f docker/docker-compose.yml down
docker-compose -f docker/docker-compose.yml up -d
```

### Container Won't Start

Check logs for errors:

```bash
docker logs codewiki
```

Common issues:
- **Invalid API key**: Verify `LLM_API_KEY` in `.env`
- **Network not found**: Create network with `docker network create codewiki-network`
- **Port conflict**: Change `APP_PORT` in `.env`

### Health Check Failing

```bash
# Check if the application is responding
curl http://localhost:8000/

# Check container health status
docker inspect codewiki --format='{{.State.Health.Status}}'

# View health check logs
docker inspect codewiki --format='{{range .State.Health.Log}}{{.Output}}{{end}}'
```

### Permission Issues with Volumes

If you encounter permission issues with mounted volumes:

```bash
# On Linux, ensure proper ownership
sudo chown -R $(id -u):$(id -g) output/

# Or run container with user mapping
docker-compose -f docker/docker-compose.yml down
# Add to docker-compose.yml under 'codewiki' service:
# user: "${UID}:${GID}"
```

### Private Repository Access

For private repositories:

1. Ensure SSH keys are properly mounted:
   ```yaml
   volumes:
     - ~/.ssh:/root/.ssh:ro
   ```

2. Verify key permissions:
   ```bash
   chmod 600 ~/.ssh/id_rsa
   chmod 644 ~/.ssh/id_rsa.pub
   ```

3. Add GitHub to known_hosts:
   ```bash
   docker exec -it codewiki ssh-keyscan github.com >> /root/.ssh/known_hosts
   ```

---

## Production Deployment

### Security Considerations

1. **Environment Variables**: Never commit `.env` file to version control
2. **API Keys**: Use secrets management in production
3. **Network**: Use isolated Docker networks
4. **Volumes**: Set appropriate permissions on mounted volumes
5. **Updates**: Regularly update base image and dependencies

### Recommended Production Setup

```yaml
# Use secrets instead of .env file
services:
  codewiki:
    secrets:
      - llm_api_key
    environment:
      - LLM_API_KEY_FILE=/run/secrets/llm_api_key

secrets:
  llm_api_key:
    external: true
```

### Resource Limits

Add resource limits in production:

```yaml
services:
  codewiki:
    deploy:
      resources:
        limits:
          cpus: '2'
          memory: 4G
        reservations:
          cpus: '1'
          memory: 2G
```

### Reverse Proxy

Use nginx or traefik as a reverse proxy:

```yaml
services:
  nginx:
    image: nginx:alpine
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
      - ./certs:/etc/nginx/certs:ro
```

---

## Integration with External Services

### Using LiteLLM Proxy

If using a LiteLLM proxy for LLM API management:

```bash
# In .env file
LLM_BASE_URL=http://litellm:4000/
LLM_API_KEY=sk-your-proxy-key

# Add LiteLLM service to docker-compose.yml
services:
  litellm:
    image: ghcr.io/berriai/litellm:latest
    ports:
      - "4000:4000"
    environment:
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
      - OPENAI_API_KEY=${OPENAI_API_KEY}
    networks:
      - codewiki-network
```

---

## More Information

- **Main Documentation**: See [../README.md](../README.md) for complete feature list and usage
- **CLI Tool**: For command-line documentation generation
- **Web Interface**: For GitHub URL-based documentation generation

---

## Support

For issues related to Docker deployment:
1. Check logs: `docker logs codewiki`
2. Verify configuration: `docker exec codewiki env | grep -E '(LLM|APP)'`
3. Test connectivity: `docker exec codewiki curl -I http://localhost:8000`
4. Report issues: https://github.com/yourusername/codewiki/issues

---

**Happy documenting with Docker! 🐳📚**
