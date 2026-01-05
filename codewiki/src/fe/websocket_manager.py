#!/usr/bin/env python3
"""
WebSocket manager for broadcasting real-time progress updates.
"""

import asyncio
import json
from typing import Dict, Set
from fastapi import WebSocket
from datetime import datetime
import logging

from .models import ProgressMessage

logger = logging.getLogger(__name__)


class WebSocketManager:
    """Manages WebSocket connections and broadcasts progress updates."""
    
    def __init__(self):
        # Store active connections by job_id
        self.active_connections: Dict[str, Set[WebSocket]] = {}
        self._lock = asyncio.Lock()
    
    async def connect(self, websocket: WebSocket, job_id: str):
        """Register a new WebSocket connection for a specific job."""
        await websocket.accept()
        async with self._lock:
            if job_id not in self.active_connections:
                self.active_connections[job_id] = set()
            self.active_connections[job_id].add(websocket)
        logger.info(f"WebSocket connected for job {job_id}. Total connections: {len(self.active_connections[job_id])}")
    
    async def disconnect(self, websocket: WebSocket, job_id: str):
        """Remove a WebSocket connection."""
        async with self._lock:
            if job_id in self.active_connections:
                self.active_connections[job_id].discard(websocket)
                if not self.active_connections[job_id]:
                    del self.active_connections[job_id]
        logger.info(f"WebSocket disconnected for job {job_id}")
    
    async def broadcast_progress(self, progress_message: ProgressMessage):
        """Broadcast progress update to all connected clients for a job."""
        job_id = progress_message.job_id
        
        if job_id not in self.active_connections:
            return
        
        # Convert message to dict for JSON serialization
        message_dict = progress_message.model_dump()
        # Convert datetime to string
        message_dict['timestamp'] = message_dict['timestamp'].isoformat()
        
        message_json = json.dumps(message_dict)
        
        # Get a copy of connections to avoid modification during iteration
        connections = list(self.active_connections.get(job_id, []))
        
        # Send to all connected clients
        disconnected = []
        for connection in connections:
            try:
                await connection.send_text(message_json)
            except Exception as e:
                logger.error(f"Error sending message to WebSocket: {e}")
                disconnected.append(connection)
        
        # Clean up disconnected clients
        if disconnected:
            async with self._lock:
                if job_id in self.active_connections:
                    for conn in disconnected:
                        self.active_connections[job_id].discard(conn)
                    if not self.active_connections[job_id]:
                        del self.active_connections[job_id]
    
    def send_progress_sync(self, progress_message: ProgressMessage):
        """Synchronous wrapper for broadcasting progress (for use in non-async code)."""
        try:
            # Get or create event loop
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # No running loop, create a new one
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(self.broadcast_progress(progress_message))
                loop.close()
            else:
                # Running loop exists, schedule the coroutine
                asyncio.create_task(self.broadcast_progress(progress_message))
        except Exception as e:
            logger.error(f"Error in send_progress_sync: {e}")


# Global WebSocket manager instance
ws_manager = WebSocketManager()
