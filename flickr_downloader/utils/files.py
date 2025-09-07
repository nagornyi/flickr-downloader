"""
File utilities for the Flickr downloader.
Handles JSON file operations and file system utilities.
"""
import os
import re
import json
from ..config import config


def load_json_file(filepath):
    """Load JSON data from file if it exists, otherwise return empty dict."""
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_json_file(filepath, data):
    """Save data to JSON file, creating directory if needed."""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def format_file_size(size_bytes):
    """Format file size in bytes to human-readable format."""
    if size_bytes == 0:
        return ""
    elif size_bytes < 1024:
        return f" ({size_bytes} B)"
    elif size_bytes < 1024 * 1024:
        return f" ({size_bytes / 1024:.1f} KB)"
    elif size_bytes < 1024 * 1024 * 1024:
        return f" ({size_bytes / (1024 * 1024):.1f} MB)"
    else:
        return f" ({size_bytes / (1024 * 1024 * 1024):.1f} GB)"


def is_video_file(filepath):
    """Check if a file is actually a video by reading its magic bytes."""
    if not os.path.exists(filepath):
        return False
    
    try:
        with open(filepath, 'rb') as f:
            header = f.read(12)
            if not header:
                return False
            
            # Check for common video file signatures
            # MP4: starts with 'ftyp' at offset 4
            if len(header) >= 8 and header[4:8] == b'ftyp':
                return True
            # AVI: starts with 'RIFF' and has 'AVI ' at offset 8
            if len(header) >= 12 and header[:4] == b'RIFF' and header[8:12] == b'AVI ':
                return True
            # MOV/QuickTime: similar to MP4, has 'ftyp' or 'moov'
            if len(header) >= 8 and (header[4:8] == b'moov' or header[4:8] == b'mdat'):
                return True
            # WebM: starts with EBML signature
            if header[:4] == b'\x1a\x45\xdf\xa3':
                return True
                
    except Exception:
        pass
    
    return False


def sanitize_filename(name):
    """Sanitize filename by removing/replacing invalid characters."""
    return re.sub(r'[<>:"/\\|?*]', '_', name)
