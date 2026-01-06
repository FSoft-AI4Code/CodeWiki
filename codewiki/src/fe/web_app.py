#!/usr/bin/env python3
"""
CodeWiki Web Application

A web interface for users to submit GitHub repositories for documentation generation.
Features:
- Simple web form for GitHub repo URL input
- Background processing queue
- Cache system for generated documentation
- Job status tracking
"""

import argparse
from fastapi import FastAPI, Request, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from .cache_manager import CacheManager
from .background_worker import BackgroundWorker
from .routes import WebRoutes
from .config import WebAppConfig
from .websocket_manager import ws_manager


# Initialize FastAPI app
app = FastAPI(
    title="CodeWiki", 
    description="Generate comprehensive documentation for any GitHub repository"
)

# Initialize components
cache_manager = CacheManager(
    cache_dir=WebAppConfig.CACHE_DIR, 
    cache_expiry_days=WebAppConfig.CACHE_EXPIRY_DAYS
)
background_worker = BackgroundWorker(
    cache_manager=cache_manager, 
    temp_dir=WebAppConfig.TEMP_DIR
)
# Set WebSocket manager for background worker
background_worker.set_ws_manager(ws_manager)
web_routes = WebRoutes(background_worker=background_worker, cache_manager=cache_manager)


# Register routes
@app.get("/", response_class=HTMLResponse)
async def index_get(request: Request):
    """Main page with form for submitting GitHub repositories."""
    return await web_routes.index_get(request)


@app.post("/", response_class=HTMLResponse)
async def index_post(request: Request, repo_url: str = Form(...), commit_id: str = Form("")):
    """Handle repository submission."""
    return await web_routes.index_post(request, repo_url, commit_id)


@app.get("/api/job/{job_id}")
async def get_job_status(job_id: str):
    """API endpoint to get job status."""
    return await web_routes.get_job_status(job_id)


@app.get("/docs/{job_id}")
async def view_docs(job_id: str):
    """View generated documentation."""
    return await web_routes.view_docs(job_id)


@app.get("/static-docs/{job_id}/")
@app.get("/static-docs/{job_id}/{filename:path}")
async def serve_generated_docs(job_id: str, filename: str = "overview.md"):
    """Serve generated documentation files."""
    if not filename: 
        filename = "overview.md"
    return await web_routes.serve_generated_docs(job_id, filename)


@app.websocket("/ws/progress/{job_id}")
async def websocket_progress(websocket: WebSocket, job_id: str):
    """WebSocket endpoint for real-time progress updates."""
    await ws_manager.connect(websocket, job_id)
    try:
        # Keep connection alive and listen for client messages
        while True:
            # Wait for any message from client (ping/pong)
            data = await websocket.receive_text()
            # Echo back to confirm connection is alive
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        await ws_manager.disconnect(websocket, job_id)


def main():
    """Main function to run the web application."""
    import uvicorn
    import os
    import logging
    
    parser = argparse.ArgumentParser(
        description="CodeWiki Web Application - Generate documentation for GitHub repositories"
    )
    parser.add_argument(
        "--host",
        type=str,
        default=WebAppConfig.DEFAULT_HOST,
        help=f"Host to bind the server to (default: {WebAppConfig.DEFAULT_HOST})"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=WebAppConfig.DEFAULT_PORT,
        help=f"Port to run the server on (default: {WebAppConfig.DEFAULT_PORT})"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Run the server in debug mode"
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reload for development"
    )
    parser.add_argument(
        "--log-file",
        type=str,
        default=None,
        help="Log file path (default: output to console only)"
    )
    parser.add_argument(
        "--log-level",
        type=str,
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
        default=None,
        help="Set log level (default: DEBUG if --debug, else INFO)"
    )
    
    args = parser.parse_args()
    
    # Determine log level: command line > environment variable > default
    if args.log_level:
        log_level_name = args.log_level.upper()
    else:
        log_level_name = os.getenv('LOG_LEVEL', 'DEBUG' if args.debug else 'INFO').upper()
    
    log_level_map = {'DEBUG': 'debug', 'INFO': 'info', 'WARNING': 'warning', 'ERROR': 'error', 'CRITICAL': 'critical'}
    uvicorn_log_level = log_level_map.get(log_level_name, 'info')
    python_log_level = getattr(logging, log_level_name, logging.INFO)
    
    # Configure backend logging with the same log level
    from codewiki.src.be.dependency_analyzer.utils.logging_config import setup_logging
    setup_logging(level=python_log_level)
    
    # Set LOG_LEVEL environment variable for child processes
    os.environ['LOG_LEVEL'] = log_level_name
    
    # Configure log file if specified
    if args.log_file:
        # Configure logging to file for all loggers
        file_handler = logging.FileHandler(args.log_file, mode='a', encoding='utf-8')
        file_handler.setLevel(python_log_level)
        formatter = logging.Formatter(
            '[%(asctime)s] %(levelname)-8s %(name)s - %(message)s',
            datefmt='%H:%M:%S'
        )
        file_handler.setFormatter(formatter)
        
        # Add file handler to root logger (this will catch all loggers)
        root_logger = logging.getLogger()
        root_logger.addHandler(file_handler)
        root_logger.setLevel(python_log_level)  # Ensure root logger level is set
        
        # Explicitly configure backend loggers to use DEBUG level
        for logger_name in ['codewiki.src.be', 'codewiki.src.be.agent_orchestrator', 
                           'codewiki.src.be.agent_tools', 'codewiki.src.be.documentation_generator',
                           'codewiki.src.be.cluster_modules', 'codewiki.src.be.dependency_analyzer']:
            logger = logging.getLogger(logger_name)
            logger.setLevel(python_log_level)
            logger.addHandler(file_handler)
        
        # Also configure uvicorn to log to file
        uvicorn_logger = logging.getLogger('uvicorn')
        uvicorn_logger.addHandler(file_handler)
        uvicorn_access_logger = logging.getLogger('uvicorn.access')
        uvicorn_access_logger.addHandler(file_handler)
        
        print(f"📝 Logging to file: {args.log_file}")
        print(f"📊 File log level: {log_level_name}")
    
    # Ensure required directories exist
    WebAppConfig.ensure_directories()
    
    # Start background worker
    background_worker.start()
    
    print(f"🚀 CodeWiki Web Application starting...")
    print(f"🌐 Server running at: http://{args.host}:{args.port}")
    print(f"📊 Log level: {log_level_name}")
    print(f"📁 Cache directory: {WebAppConfig.get_absolute_path(WebAppConfig.CACHE_DIR)}")
    print(f"🗂️  Temp directory: {WebAppConfig.get_absolute_path(WebAppConfig.TEMP_DIR)}")
    print("\nPress Ctrl+C to stop the server")
    
    try:
        uvicorn.run(
            "fe.web_app:app",
            host=args.host,
            port=args.port,
            reload=args.reload,
            log_level=uvicorn_log_level
        )
    except KeyboardInterrupt:
        print("\n👋 Server stopped")
        background_worker.stop()


if __name__ == "__main__":
    main()