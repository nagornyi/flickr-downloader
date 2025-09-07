"""
Configuration module for Flickr Downloader.
Centralizes all configuration settings and environment variables.
"""
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

class Config:
    """Configuration settings for the Flickr downloader."""
    
    # Flickr API credentials
    API_KEY = os.getenv("API_KEY")
    API_SECRET = os.getenv("API_SECRET")
    
    # Download settings
    DOWNLOAD_VIDEO = os.getenv("DOWNLOAD_VIDEO", "true").lower() == "true"
    
    # Directory settings
    DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "./flickr_downloads")
    CACHE_DIR = "./cache"
    
    @property
    def url_cache_file(self):
        return os.path.join(self.CACHE_DIR, "url_cache.json")
    
    @property 
    def progress_file(self):
        return os.path.join(self.CACHE_DIR, "progress.json")
    
    @property
    def log_file(self):
        return os.path.join(self.CACHE_DIR, "flickr_downloader.log")
    
    # Performance settings
    MAX_WORKERS = int(os.getenv("MAX_WORKERS", 8))
    API_CALL_DELAY = float(os.getenv("API_CALL_DELAY", 1.1))
    
    # Retry/backoff settings
    MAX_RETRIES = 5
    INITIAL_BACKOFF = 2  # seconds
    MAX_BACKOFF = 60     # max wait time between retries
    
    def validate(self):
        """Validate that required configuration is present."""
        if not self.API_KEY or not self.API_SECRET:
            raise ValueError("API_KEY and API_SECRET must be set in environment variables or .env file")
        
        if self.MAX_WORKERS < 1:
            raise ValueError("MAX_WORKERS must be at least 1")
            
        if self.API_CALL_DELAY < 0:
            raise ValueError("API_CALL_DELAY must be non-negative")

# Global configuration instance
config = Config()
