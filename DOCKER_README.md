# CodeWiki Docker Setup

This document explains how to run CodeWiki using Docker and Docker Compose.

## Quick Start

1. **Clone the repository** (if not already done):
   ```bash
   git clone <repository-url>
   cd CodeWiki
   ```

2. **Set up environment variables**:
   ```bash
   cp env.example .env
   # Edit .env file with your API keys
   ```

3. **Create network**
   ```bash
   docker network create codewiki-network
   ```

3. **Start the services**:
   ```bash
   docker-compose up -d
   ```

4. **Access the application**:
   - Main web app: http://localhost:8000
