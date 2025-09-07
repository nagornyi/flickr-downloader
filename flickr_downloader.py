#!/usr/bin/env python3
"""
Entry point for the modular Flickr downloader.
"""

import sys
import os

# Add current directory to path for importing the package
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flickr_downloader.main import FlickrDownloaderApp

if __name__ == "__main__":
    app = FlickrDownloaderApp()
    app.run()
