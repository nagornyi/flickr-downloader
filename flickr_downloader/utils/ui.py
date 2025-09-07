"""
User interface utilities for the Flickr downloader.
Handles progress display, logging, and user interaction.
"""
import sys
import logging
import os
from datetime import datetime
from ..config import config


def setup_logging():
    """Setup logging to both console and file."""
    # Create cache directory if it doesn't exist
    os.makedirs(config.CACHE_DIR, exist_ok=True)
    
    # Disable flickrapi's verbose logging
    flickr_logger = logging.getLogger('flickrapi')
    flickr_logger.setLevel(logging.WARNING)
    
    # Set up our custom logger
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.DEBUG)  # Set to DEBUG to capture all levels
    
    # Remove existing handlers to avoid duplicates
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
    
    # Create file handler
    file_handler = logging.FileHandler(config.log_file, mode='a', encoding='utf-8')
    file_handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
    
    # Add only file handler to our logger
    logger.addHandler(file_handler)
    
    return logger


# Initialize logger
_logger = None


def get_logger():
    """Get the global logger instance."""
    global _logger
    if _logger is None:
        _logger = setup_logging()
    return _logger


def print_and_log(message, level="INFO"):
    """Print message to console and log to file with timestamp."""
    logger = get_logger()
    
    # For DEBUG messages, only log to file, don't print to console to avoid clutter
    if level.upper() != "DEBUG":
        # Print to console with timestamp
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        formatted_message = f"{timestamp} - {message}"
        print(formatted_message)
    
    # Log to file
    if level.upper() == "ERROR":
        logger.error(message)
    elif level.upper() == "WARNING":
        logger.warning(message)
    elif level.upper() == "DEBUG":
        logger.debug(message)
    else:
        logger.info(message)


class ProgressSpinner:
    """A rotating progress indicator."""
    
    def __init__(self, message=""):
        self.message = message
        self.spinner = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
        self.current = 0
        self.running = False
        self.last_logged_message = ""
        
    def start(self):
        """Start showing the spinner."""
        self.running = True
        self._show()
        
    def update(self, message=None):
        """Update the spinner and optionally the message."""
        if message:
            self.message = message
            # Log progress updates periodically (every 10th album or when message changes significantly)
            if message != self.last_logged_message:
                # Only log when the album changes (not just spinner updates)
                if "(" in message and ")" in message:
                    album_part = message.split(")")[-1].strip()
                    if album_part != self.last_logged_message:
                        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        log_message = f"{timestamp} - {message}"
                        # Write to log file directly without console output
                        logger = get_logger()
                        if logger:
                            logger.info(message)
                        self.last_logged_message = album_part
                        
        if self.running:
            self._show()
            
    def stop(self, final_message=None):
        """Stop the spinner and show final message."""
        self.running = False
        if final_message:
            # Clear the line and use print_and_log for final message
            sys.stdout.write(f'\r{" " * 120}\r')
            sys.stdout.flush()
            print_and_log(final_message)
        else:
            # Just clear the line
            sys.stdout.write(f'\r{" " * 120}\r')
            sys.stdout.flush()
        
    def _show(self):
        """Show the current spinner frame."""
        if self.running:
            spinner_char = self.spinner[self.current % len(self.spinner)]
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            message = f'{timestamp} - {spinner_char} {self.message}'
            
            # Clear the entire line first, then write the new message
            sys.stdout.write(f'\r{" " * 120}\r{message}')
            sys.stdout.flush()
            self.current += 1


def create_spinner_message(album_index, total_albums, album_title):
    """Create a spinner message that fits within the display buffer."""
    base_message = f"Scanning albums... ({album_index}/{total_albums}) "
    available_space = 110 - len(base_message)  # Leave 10 chars buffer
    
    if len(album_title) > available_space:
        truncated_title = album_title[:available_space-3] + "..."
    else:
        truncated_title = album_title
        
    return f"{base_message}{truncated_title}"
