#!/usr/bin/env python3
"""
Startup script for CodeWiki Web Application
"""

import os
import sys

# Add src directory to Python path
src_dir = os.path.join(os.path.dirname(__file__), 'src')
sys.path.insert(0, src_dir)

from fe.web_app import main

if __name__ == "__main__":
    main()