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

# [NEW] RapidAPI Configuration (Or any other Terabox API service)
# Recommended: Subscribe to a "Terabox Downloader" on RapidAPI (many have free tiers)
# If using a public free worker (like qtcloud), leave Key empty and change the URL.
RAPID_API_KEY = os.environ.get("RAPID_API_KEY", "") 
RAPID_API_HOST = os.environ.get("RAPID_API_HOST", "terabox-downloader-direct-download-link-generator.p.rapidapi.com")
RAPID_API_URL = os.environ.get("RAPID_API_URL", "https://terabox-downloader-direct-download-link-generator.p.rapidapi.com/fetch")

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

# --- HELPER: Send Error to Telegram ---
async def send_error_to_user(client, chat_id, message):
    try:
        if client and client.is_connected():
            await client.send_message(chat_id, f"⚠️ **Task Failed**\n{message}")
    except Exception:
        pass

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
    except Exception as e:
        logger.error(f"IG Error: {e}")
        return None

# --- NEW HELPER: Terabox Downloader (API Based) ---
def download_terabox_video(link, output_dir, task_id):
    """
    Uses an external API to get the direct link and downloads it manually.
    This avoids local cookie dependency.
    """
    try:
        # 1. Fetch Direct Link from API
        # Note: Depending on which API you use, the response structure (json) will vary.
        # This example assumes a common RapidAPI response format.
        
        headers = {
            "x-rapidapi-key": RAPID_API_KEY,
            "x-rapidapi-host": RAPID_API_HOST,
            "Content-Type": "application/json"
        }
        
        # Adjust payload based on your chosen API documentation
        payload = {"url": link} 
        
        logger.info(f"Fetching Terabox link via API: {RAPID_API_URL}")
        response = requests.post(RAPID_API_URL, json=payload, headers=headers)
        
        if response.status_code != 200:
            logger.error(f"API Error: {response.text}")
            return None

        data = response.json()
        
        # EXTRACT DATA (Adjust keys based on the specific API you subscribe to)
        # Common keys: 'downloadLink', 'url', 'fast_download_link'
        direct_url = None
        if isinstance(data, list) and len(data) > 0:
             direct_url = data[0].get("url") or data[0].get("downloadLink")
        elif isinstance(data, dict):
             direct_url = data.get("url") or data.get("downloadLink") or data.get("fast_download_link")

        if not direct_url:
            update_task(task_id, "failed", message="Could not extract direct link from API.")
            return None

        # 2. Download the File using requests (Streamed)
        file_name = f"{task_id}.mp4"
        file_path = os.path.join(output_dir, file_name)
        
        # Important: Some direct links require a specific User-Agent
        dl_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36",
            "Referer": "https://terabox.com/"
        }

        with requests.get(direct_url, stream=True, headers=dl_headers) as r:
            r.raise_for_status()
            total_size = int(r.headers.get('content-length', 0))
            downloaded = 0
            
            with open(file_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total_size > 0:
                            p = (downloaded / total_size) * 100
                            # Update status every 5% to save DB/Memory calls
                            if int(p) % 5 == 0:
                                update_task(task_id, "processing", phase="downloading", progress=p)
        
        return file_path

    except Exception as e:
        logger.error(f"Terabox API Exception: {e}")
        return None

# --- ASYNC WORKER LOGIC ---
async def process_task_async(link, chat_id, task_id):
    update_task(task_id, "processing", phase="initializing", progress=0)
    
    client = None
    temp_dir = "/tmp"
    target_file_path = f"{temp_dir}/{task_id}.mp4"
    final_path = None

    try:
        client = TelegramClient(StringSession(SESSION_STR), API_ID, API_HASH)
        await client.connect()
        
        if not await client.is_user_authorized():
            update_task(task_id, "failed", message="Bot Login Failed")
            return

        update_task(task_id, "processing", phase="downloading", progress=0)
        
        if "instagram.com" in link:
            final_path = await asyncio.to_thread(download_instagram_video, link, target_file_path, task_id)
        
        elif any(x in link for x in ["terabox", "1024tera", "teraboxapp", "momerybox"]):
            # Use the new API-based downloader
            final_path = await asyncio.to_thread(download_terabox_video, link, temp_dir, task_id)
            if not final_path:
                msg = "Terabox download failed. API Error."
                update_task(task_id, "failed", message=msg)
                await send_error_to_user(client, chat_id, msg)
                return

        elif "t.me" in link:
            # (Existing Telegram logic kept identical)
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
                    return

                def telegram_progress(current, total):
                    p = (current / total) * 100
                    update_task(task_id, "processing", phase="downloading", progress=p)

                final_path = await client.download_media(message, file=target_file_path, progress_callback=telegram_progress)
            except Exception:
                return
        
        if not final_path:
             update_task(task_id, "failed", message="Download failed")
             return

        # Upload Phase
        try:
            update_task(task_id, "processing", phase="uploading", progress=0)
            attrs = get_file_attributes(final_path)
            
            async def upload_progress(current, total):
                p = (current / total) * 100
                update_task(task_id, "processing", phase="uploading", progress=p)

            await client.send_file(
                chat_id,
                final_path,
                caption="Here is your media 📂",
                attributes=attrs,
                supports_streaming=True,
                progress_callback=upload_progress
            )
            update_task(task_id, "completed", phase="done", progress=100, message="Sent to Telegram")
            
        except Exception as e:
            err_msg = f"Upload failed: {str(e)}"
            update_task(task_id, "failed", message=err_msg)
            await send_error_to_user(client, chat_id, err_msg)

    except Exception as e:
        logger.error(traceback.format_exc())
        update_task(task_id, "failed", message="Internal Server Error")
    finally:
        if client: await client.disconnect()
        safe_delete(final_path)
        safe_delete(target_file_path)
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
