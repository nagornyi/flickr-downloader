"""
Command line interface for the Flickr downloader.
Handles argument parsing and user interaction.
"""
import argparse
import fnmatch


def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Download photos and videos from your Flickr account",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                           # Download all albums
  %(prog)s --album "Vacation 2023"   # Download only the "Vacation 2023" album
  %(prog)s --album "Trip*"           # Download albums starting with "Trip"
        """
    )
    
    parser.add_argument(
        '--album', '-a',
        type=str,
        help='Download only the specified album. Supports wildcards (* and ?). '
             'If not specified, all albums will be downloaded.'
    )
    
    return parser.parse_args()


def filter_albums_by_pattern(photosets, pattern):
    """Filter albums by name pattern (supports wildcards)."""
    if not pattern:
        return photosets
    
    filtered = []
    
    for photoset in photosets:
        album_title = photoset['title']['_content']
        if fnmatch.fnmatch(album_title, pattern):
            filtered.append(photoset)
    
    return filtered
