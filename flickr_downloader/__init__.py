"""
Flickr Downloader - A modular tool for downloading photos and videos from Flickr.

This package provides a clean, maintainable structure for downloading media from Flickr
with features like progress tracking, verification, and concurrent downloads.
"""

__version__ = "2.0.0"
__author__ = "Flickr Downloader Contributors"

from .config import config
from .main import main

__all__ = ['main', 'config']
