import os
import asyncio
import uuid
import gc
import re
import logging
import traceback
import concurrent.futures
import shutil
import requests
import json
from flask import Flask, request, jsonify

# Telegram Imports
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import DocumentAttributeVideo, DocumentAttributeAudio

# Media & Metadata Imports
import yt_dlp
from fake_useragent import UserAgent
from hachoir.metadata import extractMetadata
from hachoir.parser import createParser

# --- LOGGING SETUP ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# --- CONFIGURATION ---
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
SESSION_STR = os.environ.get("SESSION_STR", "")

# RAPID API CONFIG
RAPID_API_KEY = os.environ.get("RAPID_API_KEY", "") 
RAPID_API_HOST = os.environ.get("RAPID_API_HOST", "terabox-downloader-direct-download-link-generator1.p.rapidapi.com")
RAPID_API_URL = os.environ.get("RAPID_API_URL", f"https://{RAPID_API_HOST}/url")

# --- GLOBAL STATE ---
TASK_STORE = {}
executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

# --- HELPER: Update Status ---
def update_task(task_id, status, phase=None, progress=None, message=None):
    if task_id not in TASK_STORE:
        TASK_STORE[task_id] = {}
    if status: TASK_STORE[task_id]["status"] = status
    if phase: TASK_STORE[task_id]["phase"] = phase
    if progress is not None: TASK_STORE[task_id]["progress"] = progress
    if message: TASK_STORE[task_id]["message"] = message

# --- HELPER: File Deletion ---
def safe_delete(path):
    try:
        if path and os.path.exists(path):
            if os.path.isfile(path):
                os.remove(path)
            elif os.path.isdir(path):
                shutil.rmtree(path)
    except Exception:
        pass

# --- HELPER: Send Message to Telegram (User Friendly) ---
async def send_msg_to_user(client, chat_id, message, is_error=False):
    """
    Sends a clean message to the user. 
    If is_error=True, adds a warning emoji but keeps the text polite.
    """
    try:
        if client and client.is_connected():
            prefix = "⚠️ **Oops!**\n" if is_error else "ℹ️ "
            await client.send_message(chat_id, f"{prefix}{message}")
    except Exception as e:
        logger.error(f"Telegram Send Error: {e}")

# --- HELPER: Metadata ---
def get_file_attributes(file_path):
    parser = None
    try:
        parser = createParser(file_path)
        if not parser: return []
        metadata = extractMetadata(parser)
        if not metadata: return []
        
        attrs = []
        if metadata.has("duration"):
            duration = metadata.get("duration").seconds
            width = metadata.get("width") if metadata.has("width") else 0
            height = metadata.get("height") if metadata.has("height") else 0
            
            if width > 0:
                attrs.append(DocumentAttributeVideo(
                    duration=duration, w=width, h=height, supports_streaming=True
                ))
            else:
                attrs.append(DocumentAttributeAudio(
                    duration=duration, title="Downloaded Media"
                ))
        return attrs
    except Exception:
        return []
    finally:
        if parser: parser.close()

# --- HELPER: Fetch Terabox Data (API) ---
def fetch_terabox_data(link, task_id):
    """
    Fetches data. Returns (list_of_files, error_message_for_log).
    If success, error_message is None.
    """
    try:
        headers = {
            "x-rapidapi-key": RAPID_API_KEY,
            "x-rapidapi-host": RAPID_API_HOST
        }
        querystring = {"url": link}
        
        # Log purely for admin debugging
        logger.info(f"[{task_id}] API Request: {RAPID_API_URL}")
        
        response = requests.get(RAPID_API_URL, headers=headers, params=querystring, timeout=30)
        
        if response.status_code == 403:
            return None, "API Key Invalid or Expired (403)"
        if response.status_code == 429:
            return None, "API Rate Limit Exceeded (429)"
        if response.status_code != 200:
            return None, f"API Error {response.status_code}: {response.text[:100]}"

        try:
            data = response.json()
        except json.JSONDecodeError:
            return None, f"Invalid JSON response: {response.text[:100]}"
        
        if not data:
            return None, "API returned empty data (Link might be dead/private)"

        # Normalize to list
        if isinstance(data, dict):
            data = [data]
            
        file_list = []
        for item in data:
            # Flexible key search
            d_link = item.get("direct_link") or item.get("link") or item.get("url") or item.get("downloadLink")
            fname = item.get("file_name") or item.get("filename") or item.get("title") or "video.mp4"
            
            if d_link:
                file_list.append({"url": d_link, "name": fname})
        
        if not file_list:
            # If we got JSON but extracted 0 links
            return None, f"No direct links found in response: {str(data)[:100]}"
            
        return file_list, None

    except Exception as e:
        return None, f"Exception during fetch: {str(e)}"

# --- HELPER: Generic File Downloader ---
def download_file(url, output_path, task_id, current_file_idx, total_files):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36",
            "Referer": "https://terabox.com/"
        }
        
        with requests.get(url, stream=True, headers=headers, timeout=20) as r:
            r.raise_for_status()
            total_size = int(r.headers.get('content-length', 0))
            downloaded = 0
            
            with open(output_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total_size > 0:
                            p = (downloaded / total_size) * 100
                            # Log every 10% internally, don't spam logs
                            if int(p) % 10 == 0:
                                update_task(task_id, "processing", phase="downloading", progress=p)
        
        # Validation: Check if we downloaded a tiny error file
        if os.path.getsize(output_path) < 2000:
             logger.warning(f"[{task_id}] Downloaded file is too small (<2kb). Likely an error page.")
             return None

        return output_path
    except Exception as e:
        logger.error(f"[{task_id}] Download Exception: {e}")
        return None

# --- HELPER: Instagram Downloader ---
def download_instagram_video(link, output_path, task_id):
    try:
        ua = UserAgent()
        def progress_hook(d):
            if d['status'] == 'downloading':
                try:
                    p = d.get('_percent_str', '0%').replace('%','')
                    update_task(task_id, "processing", phase="downloading", progress=float(p))
                except: pass

        ydl_opts = {
            'outtmpl': output_path,
            'format': 'best[ext=mp4]/best',
            'quiet': True,
            'no_warnings': True,
            'user_agent': ua.random,
            'progress_hooks': [progress_hook],
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([link])
            
        if os.path.exists(output_path): return output_path
        return None
    except Exception:
        return None

# --- ASYNC WORKER LOGIC ---
async def process_task_async(link, chat_id, task_id):
    update_task(task_id, "processing", phase="initializing", progress=0)
    
    client = None
    temp_dir = "/tmp"
    download_queue = []

    try:
        # 1. Connect Telegram
        client = TelegramClient(StringSession(SESSION_STR), API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            update_task(task_id, "failed", message="Bot Login Failed")
            logger.error(f"[{task_id}] Login Failed: Check Session String")
            return

        # 2. Determine Source
        if "instagram.com" in link:
            target = f"{temp_dir}/{task_id}.mp4"
            path = await asyncio.to_thread(download_instagram_video, link, target, task_id)
            if path:
                download_queue.append({'type': 'local', 'path': path})
            else:
                await send_msg_to_user(client, chat_id, "Could not download from Instagram. The post might be private.", is_error=True)
                return

        elif any(x in link for x in ["terabox", "1024tera", "teraboxapp", "momerybox"]):
            # Use updated fetcher that returns detailed error for LOGS, but polite msg for USER
            files_info, log_error = await asyncio.to_thread(fetch_terabox_data, link, task_id)
            
            if log_error:
                # Log the real ugly error for you
                logger.error(f"[{task_id}] Terabox Failure: {log_error}")
                # Send polite message to user
                await send_msg_to_user(client, chat_id, "Unable to process this Terabox link. It might be invalid, expired, or server is busy.", is_error=True)
                update_task(task_id, "failed", message="API Fetch Failed")
                return
            
            for item in files_info:
                download_queue.append({'type': 'url', 'url': item['url'], 'name': item['name']})

        elif "t.me" in link:
            # Standard Telegram Logic
            try:
                if "/c/" in link: 
                    match = re.search(r'/c/(\d+)/(\d+)', link)
                    cid, mid = int("-100" + match.group(1)), int(match.group(2))
                    entity = await client.get_entity(cid)
                else: 
                    match = re.search(r't\.me/([^/]+)/(\d+)', link)
                    entity = await client.get_entity(match.group(1))
                    mid = int(match.group(2))
                
                message = await client.get_messages(entity, ids=mid)
                if not message or not message.media:
                    await send_msg_to_user(client, chat_id, "No media found in that Telegram link.", is_error=True)
                    return

                target = f"{temp_dir}/{task_id}.mp4"
                def telegram_progress(current, total):
                    p = (current / total) * 100
                    update_task(task_id, "processing", phase="downloading", progress=p)

                path = await client.download_media(message, file=target, progress_callback=telegram_progress)
                if path:
                     download_queue.append({'type': 'local', 'path': path})

            except Exception as e:
                logger.error(f"[{task_id}] Telegram Fetch Error: {e}")
                await send_msg_to_user(client, chat_id, "Could not access that Telegram message. Ensure the bot is in the channel.", is_error=True)
                return

        # 3. PROCESS QUEUE
        total_files = len(download_queue)
        
        if total_files == 0:
            await send_msg_to_user(client, chat_id, "No downloadable files found.", is_error=True)
            return

        # Notify user processing started
        if total_files > 1:
            await send_msg_to_user(client, chat_id, f"Found {total_files} files. Downloading one by one...")

        for index, item in enumerate(download_queue, start=1):
            current_path = None
            
            # Step A: Download
            if item['type'] == 'url':
                update_task(task_id, "processing", phase="downloading", message=f"Downloading {index}/{total_files}")
                
                clean_name = re.sub(r'[\\/*?:"<>|]', "", item['name'])
                
                # FIX: Added image extensions to this check so they aren't forced to .mp4
                if not clean_name.lower().endswith(('.mp4', '.mkv', '.webm', '.jpg', '.png', '.jpeg', '.webp', '.bmp', '.gif')):
                    clean_name += ".mp4"
                
                temp_path = os.path.join(temp_dir, f"{uuid.uuid4()}_{clean_name}")
                current_path = await asyncio.to_thread(download_file, item['url'], temp_path, task_id, index, total_files)
            
            elif item['type'] == 'local':
                current_path = item['path']

            if not current_path or not os.path.exists(current_path):
                # Silent fail for the user on specific file, but log it
                logger.error(f"[{task_id}] Failed to download item {index}")
                await send_msg_to_user(client, chat_id, f"Skipped file {index}: Download failed.", is_error=True)
                continue 

            # Step B: Upload
            try:
                update_task(task_id, "processing", phase="uploading", message=f"Uploading {index}/{total_files}")
                
                # FIX: Check if it is an image or video to determine attributes and streaming support
                is_video_file = current_path.lower().endswith(('.mp4', '.mkv', '.webm', '.avi', '.mov', '.flv'))
                
                if is_video_file:
                    attrs = get_file_attributes(current_path)
                    use_streaming = True
                else:
                    # It is an image/photo
                    attrs = []
                    use_streaming = False
                
                async def upload_progress(current, total):
                    p = (current / total) * 100
                    if int(p) % 5 == 0:
                        update_task(task_id, "processing", phase="uploading", progress=p)

                await client.send_file(
                    chat_id,
                    current_path,
                    caption=f"📁 **File {index}/{total_files}**",
                    attributes=attrs,
                    supports_streaming=use_streaming,
                    progress_callback=upload_progress
                )
            except Exception as e:
                logger.error(f"[{task_id}] Upload Fail: {e}")
                await send_msg_to_user(client, chat_id, f"Failed to upload file {index} to Telegram.", is_error=True)
            finally:
                safe_delete(current_path)

        update_task(task_id, "completed", phase="done", progress=100, message="All files sent.")

    except Exception as e:
        logger.error(f"[{task_id}] Critical Error: {traceback.format_exc()}")
        update_task(task_id, "failed", message="Internal Server Error")
        await send_msg_to_user(client, chat_id, "An internal error occurred. Please try again later.", is_error=True)
    finally:
        if client: await client.disconnect()
        gc.collect()

# --- RUNNER ---
def run_background_process(link, chat_id, task_id):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(process_task_async(link, chat_id, task_id))
    finally:
        loop.close()

# --- ROUTES ---
@app.route('/download', methods=['POST'])
def handle_download():
    try:
        data = request.json
        link = data.get('link', '').strip()
        chat_id = data.get('chat_id')
        if not link or not chat_id: return jsonify({"status": "error"}), 400
        
        task_id = str(uuid.uuid4())
        TASK_STORE[task_id] = {"status": "queued", "phase": "pending", "progress": 0}
        executor.submit(run_background_process, link, chat_id, task_id)
        return jsonify({"status": "queued", "task_id": task_id}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/status/<task_id>', methods=['GET'])
def check_status(task_id):
    return jsonify(TASK_STORE.get(task_id, {"status": "not_found"})), 200

@app.route('/', methods=['GET'])
def health_check():
    return "Bot is Alive", 200

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
