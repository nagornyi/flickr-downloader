# Flickr Downloader

This script will download all your files from your Flickr account in parallel threads, and it'll respect the Flickr API limit. If it hits that limit, it'll just retry or back off.

## Environment variables in `.env` file

```sh
API_KEY="your_api_key_here"
API_SECRET="your_api_secret_here"
DOWNLOAD_DIR="./flickr_downloads"
MAX_WORKERS=8
VIDEO_TOKEN="your_video_token_here"
```

### Getting the VIDEO_TOKEN for full-quality video downloads

The `VIDEO_TOKEN` is required to download original full-quality video files. Without it, you may only get preview images instead of actual videos.

To get your VIDEO_TOKEN:

1. **Open Flickr in your web browser** and log into your account
2. **Navigate to any video** in your account
3. **Open browser developer tools** (F12 or right-click → Inspect)
4. **Go to the Network tab** in developer tools
5. **Click to play or download the video**
6. **Look for network requests** to `live.staticflickr.com` that contain `?s=` in the URL
7. **Copy the token value** after `?s=` (the long string after the equals sign)
8. **Add it to your .env file** as `VIDEO_TOKEN="your_copied_token"`

Example token format:
```
VIDEO_TOKEN="yourtoken"
```

**Note:** This token appears to be account-specific and may have a long expiration time, but you may need to refresh it occasionally if video downloads start failing.

## Installation and running

```sh
pip3 install -r requirements.txt
python3 flickr_downloader.py
```
