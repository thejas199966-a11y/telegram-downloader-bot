import os
import asyncio
import uuid
import gc
import re
import logging
import traceback
import concurrent.futures
import time
from flask import Flask, request, jsonify

# Telegram Imports
from telethon import TelegramClient, errors
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

# --- GLOBAL STATE (In-Memory Database) ---
# Structure: { "task_id": { "status": "processing", "phase": "init", "progress": 0, "message": "" } }
TASK_STORE = {}

# --- THREAD POOL ---
executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

# --- HELPER: Update Status ---
def update_task(task_id, status, phase=None, progress=None, message=None):
    """Updates the global task store safely."""
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
            os.remove(path)
    except Exception:
        pass

# --- HELPER: Send Error to Telegram ---
async def send_error_to_user(client, chat_id, message):
    try:
        if client and client.is_connected():
            await client.send_message(chat_id, f"⚠️ **Task Failed**\n{message}")
    except Exception as e:
        logger.error(f"Telegram Error Send Failed: {e}")

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

# --- HELPER: Instagram Downloader (With Progress) ---
def download_instagram_video(link, output_path, task_id):
    try:
        ua = UserAgent()
        
        # progress hook for yt-dlp
        def progress_hook(d):
            if d['status'] == 'downloading':
                try:
                    p = d.get('_percent_str', '0%').replace('%','')
                    update_task(task_id, "processing", phase="downloading", progress=float(p))
                except:
                    pass

        ydl_opts = {
            'outtmpl': output_path,
            'format': 'best[ext=mp4]/best',
            'quiet': True,
            'no_warnings': True,
            'ignoreerrors': True,
            'nocheckcertificate': True,
            'user_agent': ua.random,
            'socket_timeout': 15,
            'progress_hooks': [progress_hook], # Attach hook
            'http_headers': {
                'Accept-Language': 'en-US,en;q=0.9',
                'Sec-Fetch-Mode': 'navigate',
            }
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([link])
            
        if os.path.exists(output_path): return output_path
        
        # Fallback check
        base = output_path.rsplit('.', 1)[0]
        for ext in ['.mp4', '.mkv', '.webm']:
            if os.path.exists(base + ext):
                return base + ext
        return None
    except Exception as e:
        logger.error(f"DL Error: {e}")
        return None

# --- ASYNC WORKER LOGIC ---
async def process_task_async(link, chat_id, task_id):
    update_task(task_id, "processing", phase="initializing", progress=0)
    
    client = None
    file_path = f"/tmp/{task_id}.mp4"
    final_path = None

    try:
        # 1. Connect
        client = TelegramClient(StringSession(SESSION_STR), API_ID, API_HASH)
        await client.connect()
        
        if not await client.is_user_authorized():
            update_task(task_id, "failed", message="Bot Login Failed (Check Server Logs)")
            return

        # 2. Download Phase
        update_task(task_id, "processing", phase="downloading", progress=0)
        
        if "instagram.com" in link:
            final_path = await asyncio.to_thread(download_instagram_video, link, file_path, task_id)
            if not final_path:
                msg = "Download failed. Account might be private or content deleted."
                update_task(task_id, "failed", message=msg)
                await send_error_to_user(client, chat_id, msg)
                return

        elif "t.me" in link:
            try:
                # Telegram Download Logic
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
                    update_task(task_id, "failed", message="No media found in that Telegram link.")
                    await send_error_to_user(client, chat_id, "No media found.")
                    return

                # Telegram Callback for Progress
                def telegram_progress(current, total):
                    p = (current / total) * 100
                    update_task(task_id, "processing", phase="downloading", progress=p)

                final_path = await client.download_media(message, file=file_path, progress_callback=telegram_progress)
            except Exception as e:
                update_task(task_id, "failed", message=f"Telegram Fetch Error: {str(e)}")
                return
        else:
            update_task(task_id, "failed", message="Link type not supported.")
            return

        # 3. Upload Phase
        try:
            update_task(task_id, "processing", phase="uploading", progress=0)
            attrs = get_file_attributes(final_path)
            
            # Telegram Upload Callback
            async def upload_progress(current, total):
                p = (current / total) * 100
                update_task(task_id, "processing", phase="uploading", progress=p)

            await client.send_file(
                chat_id,
                final_path,
                caption="Here is your media 📂",
                attributes=attrs,
                supports_streaming=True,
                force_document=False,
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
        if client: await send_error_to_user(client, chat_id, "Internal Server Error")

    finally:
        if client: await client.disconnect()
        if final_path: safe_delete(final_path)
        safe_delete(file_path)
        gc.collect()

# --- THREAD WRAPPER ---
def run_background_process(link, chat_id, task_id):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(process_task_async(link, chat_id, task_id))
    finally:
        loop.close()

# --- API ROUTES ---

@app.route('/download', methods=['POST'])
def handle_download():
    try:
        data = request.json
        link = data.get('link', '').strip()
        chat_id = data.get('chat_id')

        if not link or not chat_id:
            return jsonify({"status": "error", "message": "Missing link or chat_id"}), 400
        
        # Ignore bot echoes
        if "Here is your media" in link or link.startswith("⚠️"):
             return jsonify({"status": "ignored"}), 200

        task_id = str(uuid.uuid4())
        
        # Initialize Task
        TASK_STORE[task_id] = {
            "status": "queued",
            "phase": "pending",
            "progress": 0,
            "message": "Waiting for worker..."
        }

        # Start Background Task
        executor.submit(run_background_process, link, chat_id, task_id)
        
        # Return ID immediately
        return jsonify({
            "status": "queued",
            "task_id": task_id,
            "message": "Task queued successfully. Check /status/<task_id>"
        }), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/status/<task_id>', methods=['GET'])
def check_status(task_id):
    task = TASK_STORE.get(task_id)
    if not task:
        return jsonify({"status": "not_found", "message": "Task ID not found or expired"}), 404
    
    return jsonify(task), 200

@app.route('/', methods=['GET'])
def health_check():
    return "Bot is Alive", 200

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
