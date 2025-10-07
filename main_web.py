from flask import Flask, render_template, request, send_file, jsonify, Response
import yt_dlp
import os
import shutil
import logging
from datetime import datetime
import threading
import time
import uuid
import json
from urllib.parse import urlparse
import platform
import tempfile
import subprocess
import sys

# Optional: force update yt-dlp at startup (useful if Render image is stale)
YTDLP_FORCE_UPDATE = os.environ.get('YTDLP_FORCE_UPDATE', 'false').lower() in ('1', 'true', 'yes')


# Small logger adapter for yt-dlp to route messages into Python logging
class YtDlpLogger:
    def debug(self, msg):
        logging.debug('yt-dlp: %s', msg)

    def info(self, msg):
        logging.info('yt-dlp: %s', msg)

    def warning(self, msg):
        logging.warning('yt-dlp: %s', msg)

    def error(self, msg):
        logging.error('yt-dlp: %s', msg)


def _parse_cookie_names(path):
    """Return a set of cookie names found in a Netscape-format cookie file."""
    names = set()
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw or raw.startswith('#'):
                    continue
                parts = raw.split('\t') if '\t' in raw else raw.split()
                if len(parts) >= 7:
                    names.add(parts[5])
    except Exception:
        logging.exception('Failed to parse cookie names from %s', path)
    return names

# Log yt-dlp version
try:
    logging.info('yt-dlp version: %s', getattr(yt_dlp, '__version__', 'unknown'))
except Exception:
    pass

if YTDLP_FORCE_UPDATE:
    try:
        logging.info('Forcing yt-dlp update at startup...')
        subprocess.run([sys.executable, '-m', 'pip', 'install', '--upgrade', 'yt-dlp'], check=True)
        logging.info('yt-dlp force-update completed')
    except Exception:
        logging.exception('yt-dlp force-update failed')

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# Path to ffmpeg: prefer platform-appropriate default but allow override via FFMPEG_PATH env var
# Detect platform: use ffmpeg.exe locally on Windows, /usr/bin/ffmpeg on Linux (Render)
if platform.system().lower() == 'windows':
    FFMPEG_PATH = os.environ.get('FFMPEG_PATH', 'ffmpeg.exe')
else:
    FFMPEG_PATH = os.environ.get('FFMPEG_PATH', '/usr/bin/ffmpeg')

# Optional API key: if INSTAWEB_API_KEY is set, incoming /start requests must provide it
API_KEY = os.environ.get('INSTAWEB_API_KEY')

# In-memory job store (for dev). Replace with Redis or DB in production.
jobs = {}
jobs_lock = threading.Lock()

# Rate limiting: allow RATE_LIMIT_COUNT requests per RATE_LIMIT_WINDOW seconds per IP
rate_limit = {}
RATE_LIMIT_COUNT = int(os.environ.get('RATE_LIMIT_COUNT', '5'))
RATE_LIMIT_WINDOW = int(os.environ.get('RATE_LIMIT_WINDOW', str(60 * 60)))  # seconds
RATE_LIMIT_CONCURRENT = int(os.environ.get('RATE_LIMIT_CONCURRENT', '3'))

# Optional: allow yt-dlp auto-update
YTDLP_AUTO_UPDATE = os.environ.get('YTDLP_AUTO_UPDATE', 'false').lower() in ('1', 'true', 'yes')
YTDLP_UPDATE_INTERVAL_MIN = int(os.environ.get('YTDLP_UPDATE_INTERVAL_MIN', '60'))

# Directory for temporary downloads
BASE_DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), 'downloads')
os.makedirs(BASE_DOWNLOAD_DIR, exist_ok=True)


def get_client_ip():
    forwarded = request.headers.get('X-Forwarded-For')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.remote_addr or 'unknown'


def check_rate_limit(ip):
    now = time.time()
    times = rate_limit.get(ip, [])
    times = [t for t in times if now - t < RATE_LIMIT_WINDOW]
    if len(times) >= RATE_LIMIT_COUNT:
        return False
    times.append(now)
    rate_limit[ip] = times
    return True


def check_concurrent_limit(ip):
    """Ensure an IP doesn't have too many queued/running jobs at once."""
    count = 0
    with jobs_lock:
        for j in jobs.values():
            if j.get('ip') == ip and j.get('status') in ('queued', 'running'):
                count += 1
    return count < RATE_LIMIT_CONCURRENT


def is_valid_instagram_url(url):
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            return False
        host = parsed.netloc.lower()
        # allow instagram.com and www.instagram.com and m.instagram.com
        if not (host == 'instagram.com' or host.endswith('.instagram.com')):
            return False
        # require a path that looks like a reel/post/tv: e.g. /reel/, /reels/, /p/, /tv/
        path = parsed.path.lower()
        if any(path.startswith(p) for p in ('/reel/', '/reels/', '/p/', '/tv/')):
            return True
        # also accept URLs that contain /reel or /reels anywhere
        if '/reel' in path or '/reels' in path or '/p/' in path or '/tv/' in path:
            return True
        return False
    except Exception:
        return False


def yt_progress_hook(job_id):
    def hook(d):
        with jobs_lock:
            job = jobs.get(job_id)
            if not job:
                return
            status = d.get('status')
            if status == 'downloading':
                total = d.get('total_bytes') or d.get('total_bytes_estimate')
                downloaded = d.get('downloaded_bytes', 0)
                try:
                    percent = (downloaded / total) * 100 if total else None
                except Exception:
                    percent = None
                job['progress'] = {
                    'status': 'downloading',
                    'downloaded': downloaded,
                    'total': total,
                    'percent': percent,
                    'eta': d.get('eta')
                }
            elif status == 'finished':
                job['progress'] = {'status': 'finished'}
            elif status == 'error':
                job['progress'] = {'status': 'error', 'message': d.get('errmsg')}
    return hook


def background_cleaner():
    """Background thread that deletes files older than 30 minutes and clears jobs."""
    while True:
        now = time.time()
        cutoff = now - (30 * 60)
        try:
            for name in os.listdir(BASE_DOWNLOAD_DIR):
                path = os.path.join(BASE_DOWNLOAD_DIR, name)
                try:
                    mtime = os.path.getmtime(path)
                    if mtime < cutoff:
                        if os.path.isdir(path):
                            shutil.rmtree(path, ignore_errors=True)
                        else:
                            os.remove(path)
                        with jobs_lock:
                            for jid, j in list(jobs.items()):
                                if j.get('temp_dir') == path or j.get('filepath', '').startswith(path):
                                    jobs.pop(jid, None)
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(60)


# Launch cleaner thread (daemon)
cleaner_thread = threading.Thread(target=background_cleaner, daemon=True)
cleaner_thread.start()


# Optional yt-dlp auto-updater thread
def ytdlp_auto_updater():
    while True:
        try:
            logging.info('Running yt-dlp auto-update check...')
            # Use pip to update the installed package in the current Python environment
            subprocess.run([sys.executable, '-m', 'pip', 'install', '--upgrade', 'yt-dlp'], check=True)
            logging.info('yt-dlp auto-update completed')
        except Exception:
            logging.exception('yt-dlp auto-update failed')
        time.sleep(max(1, YTDLP_UPDATE_INTERVAL_MIN) * 60)


if YTDLP_AUTO_UPDATE:
    t = threading.Thread(target=ytdlp_auto_updater, daemon=True)
    t.start()


def run_download_job(job_id, url, fmt, proxy=None):
    temp_dir = os.path.join(BASE_DOWNLOAD_DIR, job_id)
    os.makedirs(temp_dir, exist_ok=True)
    with jobs_lock:
        jobs[job_id]['temp_dir'] = temp_dir
        jobs[job_id]['status'] = 'running'
        jobs[job_id]['progress'] = {'status': 'started'}
    try:
        outtmpl = os.path.join(temp_dir, '%(id)s.%(ext)s')
        ydl_opts = {
            'outtmpl': outtmpl,
            'noplaylist': True,
            'progress_hooks': [yt_progress_hook(job_id)],
            'quiet': True,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Referer': 'https://www.instagram.com/',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
                'Accept-Language': 'en-US,en;q=0.9',
                'Accept-Encoding': 'gzip, deflate, br',
                'DNT': '1',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
                'Sec-Fetch-Dest': 'document',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Site': 'none',
                'Sec-Fetch-User': '?1',
                'Cache-Control': 'max-age=0',
            },
            'extractor_args': {
                'instagram': {
                    'api': 'web',
                },
            },
        }

        # Resolve ffmpeg: prefer system 'ffmpeg' on PATH, otherwise fall back to configured FFMPEG_PATH
        resolved_ffmpeg = shutil.which('ffmpeg') or FFMPEG_PATH
        if not resolved_ffmpeg or not os.path.exists(resolved_ffmpeg):
            logging.warning('ffmpeg not found at resolved path: %s; yt-dlp may fail if ffmpeg is required', resolved_ffmpeg)
        else:
            ydl_opts['ffmpeg_location'] = resolved_ffmpeg

        # Proxy support
        if proxy:
            ydl_opts['proxy'] = proxy
        if fmt == 'audio':
            ydl_opts['format'] = 'bestaudio/best'
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }]
            final_ext = 'mp3'
        else:
            ydl_opts['format'] = 'bestvideo+bestaudio/best'
            final_ext = 'mp4'

        try:
            # Attach our logger to yt-dlp for richer debug output
            ydl_opts_with_logger = dict(ydl_opts)
            ydl_opts_with_logger['logger'] = YtDlpLogger()
            # enable verbose output in yt-dlp
            ydl_opts_with_logger['no_warnings'] = False
            ydl_opts_with_logger['verbose'] = True
            with yt_dlp.YoutubeDL(ydl_opts_with_logger) as ydl:
                ydl.extract_info(url, download=True)
        except Exception as de:

            msg = str(de)
            logging.exception('yt-dlp download error: %s', msg)

            # Inspect message for login/auth issues
            login_keywords = ('login', '403', 'forbidden', 'private', 'authentication', 'login_required', 'not authorized', 'please sign in')
            if any(k in msg.lower() for k in login_keywords):
                err_text = 'Instagram requires login for this content. Unable to download without authentication.'
            else:
                err_text = 'Failed to download media. The URL may be private or invalid.'

            with jobs_lock:
                jobs[job_id]['status'] = 'error'
                jobs[job_id]['error'] = err_text
            return
        except FileNotFoundError as fe:
            # Likely ffmpeg missing
            logging.exception('ffmpeg not found')
            with jobs_lock:
                jobs[job_id]['status'] = 'error'
                jobs[job_id]['error'] = 'ffmpeg not found or not executable. Please install ffmpeg.'
            return
        except Exception as e:
            logging.exception('unexpected download error')
            with jobs_lock:
                jobs[job_id]['status'] = 'error'
                jobs[job_id]['error'] = 'Unexpected error during download.'
            return

        produced = None
        for root, _, files in os.walk(temp_dir):
            for f in files:
                if f.lower().endswith(final_ext):
                    produced = os.path.join(root, f)
                    break
            if produced:
                break

        if not produced:
            with jobs_lock:
                jobs[job_id]['status'] = 'error'
                jobs[job_id]['error'] = 'Download finished but output file not found.'
            return

        with jobs_lock:
            jobs[job_id]['status'] = 'ready'
            jobs[job_id]['filepath'] = produced
            jobs[job_id]['filename'] = os.path.basename(produced)
            try:
                jobs[job_id]['size'] = os.path.getsize(produced)
            except Exception:
                jobs[job_id]['size'] = None

    except Exception as e:
        logging.exception('run_download_job failed')
        with jobs_lock:
            jobs[job_id]['status'] = 'error'
            jobs[job_id]['error'] = 'Server error while processing the download.'


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/start', methods=['POST'])
def start():
    data = request.get_json() or {}
    url = data.get('url')
    fmt = data.get('format', 'mp4')

    # If API key is configured, require it in header or JSON
    if API_KEY:
        key = request.headers.get('X-API-Key') or data.get('api_key')
        if not key or key != API_KEY:
            return jsonify({'error': 'Invalid or missing API key'}), 401

    if not url or not is_valid_instagram_url(url):
        return jsonify({'error': 'Invalid Instagram URL'}), 400

    ip = get_client_ip()
    if not check_rate_limit(ip):
        return jsonify({'error': 'Rate limit exceeded'}), 429

    job_id = uuid.uuid4().hex
    # Accept optional proxy from the request
    proxy = data.get('proxy')

    with jobs_lock:
        jobs[job_id] = {
            'id': job_id,
            'status': 'queued',
            'progress': {'status': 'queued'},
            'created_at': time.time(),
            'format': fmt,
            'url': url,
            'ip': ip,
            'proxy': bool(proxy),
        }

    thread = threading.Thread(target=run_download_job, args=(job_id, url, fmt, proxy), daemon=True)
    thread.start()

    return jsonify({'job_id': job_id})


@app.route('/events/<job_id>')
def events(job_id):
    def gen():
        last = None
        while True:
            with jobs_lock:
                job = jobs.get(job_id)
                if not job:
                    payload = {'status': 'unknown'}
                else:
                    payload = {
                        'status': job.get('status'),
                        'progress': job.get('progress'),
                        'error': job.get('error') if job.get('status') == 'error' else None,
                        'filename': job.get('filename'),
                        'size': job.get('size'),
                    }
            s = json.dumps(payload)
            if s != last:
                yield f'data: {s}\n\n'
                last = s
            if payload.get('status') in ('ready', 'error', 'unknown'):
                break
            time.sleep(0.5)
    return Response(gen(), mimetype='text/event-stream')

@app.route('/download/<job_id>')
def download(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return 'Job not found', 404
        if job.get('status') != 'ready' or not job.get('filepath'):
            return 'File not ready', 400
        filepath = job['filepath']
        filename = job.get('filename') or os.path.basename(filepath)
    # send_file with download_name requires Flask >=2.0; fallback to attachment filename if not available
    try:
        return send_file(filepath, as_attachment=True, download_name=filename)
    except TypeError:
        return send_file(filepath, as_attachment=True)


if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True, threaded=True)

