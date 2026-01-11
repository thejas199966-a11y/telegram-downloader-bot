import os
import asyncio
import uuid
import gc
import re
import logging
import traceback
import concurrent.futures
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

# --- THREAD POOL (Background Worker) ---
# strictly set to 1 worker to prevent RAM explosion on Render Free Tier
executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

# --- SAFETY HELPER: File Deletion ---
def safe_delete(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
            logger.info(f"Deleted temp file: {path}")
    except Exception as e:
        logger.error(f"Failed to delete {path}: {e}")

# --- SAFETY HELPER: Send Error ---
async def send_error_to_user(client, chat_id, message):
    try:
        if client and client.is_connected():
            await client.send_message(chat_id, f"⚠️ **Task Failed**\nReason: {message}")
    except Exception as e:
        logger.error(f"Could not send error message to user: {e}")

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
def download_instagram_video(link, output_path):
    try:
        ua = UserAgent()
        random_ua = ua.random
        logger.info(f"Using Fake User-Agent: {random_ua}")

        ydl_opts = {
            'outtmpl': output_path,
            'format': 'best[ext=mp4]/best',
            'quiet': True,
            'no_warnings': True,
            'ignoreerrors': True,
            'nocheckcertificate': True,
            'user_agent': random_ua,
            'socket_timeout': 15,  # FIX: Prevent hanging indefinitely
            'http_headers': {
                'Accept-Language': 'en-US,en;q=0.9',
                'Sec-Fetch-Mode': 'navigate',
            }
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([link])
            
        if os.path.exists(output_path): return output_path
        
        # Check variations
        base = output_path.rsplit('.', 1)[0]
        for ext in ['.mp4', '.mkv', '.webm']:
            if os.path.exists(base + ext):
                return base + ext
                
        return None
    except Exception as e:
        logger.error(f"Instagram Download Error: {e}")
        return None

# --- ASYNC WORKER LOGIC ---
async def process_task_async(link, chat_id, task_id):
    """The actual heavy lifting logic."""
    logger.info(f"[{task_id}] Processing Started: {link}")
    client = None
    file_path = f"/tmp/{task_id}.mp4"
    final_path = None

    try:
        # 1. Initialize Client (Per-task isolation for thread safety)
        client = TelegramClient(StringSession(SESSION_STR), API_ID, API_HASH)
        await client.connect()
        
        if not await client.is_user_authorized():
            logger.error(f"[{task_id}] Bot Unauthorized")
            return

        # 2. Download Phase
        if "instagram.com" in link:
            final_path = await asyncio.to_thread(download_instagram_video, link, file_path)
            if not final_path:
                await send_error_to_user(client, chat_id, "Instagram download failed (Private/Blocked).")
                return

        elif "t.me" in link:
            try:
                if "/c/" in link: 
                    match = re.search(r'/c/(\d+)/(\d+)', link)
                    if not match: raise ValueError("Invalid Link")
                    # Fix: Handle private channel ID correctly
                    cid = int("-100" + match.group(1))
                    entity = await client.get_entity(cid)
                    mid = int(match.group(2))
                else: 
                    match = re.search(r't\.me/([^/]+)/(\d+)', link)
                    if not match: raise ValueError("Invalid Link")
                    entity = await client.get_entity(match.group(1))
                    mid = int(match.group(2))
                
                message = await client.get_messages(entity, ids=mid)
                if not message or not message.media:
                    await send_error_to_user(client, chat_id, "No media found in message.")
                    return

                final_path = await client.download_media(message, file=file_path)
            except Exception as e:
                await send_error_to_user(client, chat_id, f"Telegram error: {str(e)}")
                return
        else:
            await send_error_to_user(client, chat_id, "Unsupported link type")
            return

        # 3. Upload Phase
        try:
            logger.info(f"[{task_id}] Uploading...")
            attrs = get_file_attributes(final_path)
            
            # Send file logic
            await client.send_file(
                chat_id,
                final_path,
                caption="Here is your media 📂",
                attributes=attrs,
                supports_streaming=True,
                force_document=False
            )
            logger.info(f"[{task_id}] Success")
        except Exception as e:
            logger.error(f"[{task_id}] Upload Failed: {e}")
            await send_error_to_user(client, chat_id, f"Upload failed: {str(e)}")

    except Exception as e:
        logger.critical(f"[{task_id}] CRITICAL FAIL: {traceback.format_exc()}")
        if client and client.is_connected():
            await send_error_to_user(client, chat_id, "Internal Server Error")

    finally:
        if client: await client.disconnect()
        if final_path: safe_delete(final_path)
        safe_delete(file_path)
        gc.collect()

# --- THREAD WRAPPER ---
def run_background_process(link, chat_id, task_id):
    """
    Creates a new event loop for this thread and runs the async task.
    This runs completely independent of the Flask request.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(process_task_async(link, chat_id, task_id))
    except Exception as e:
        logger.error(f"Thread Error: {e}")
    finally:
        loop.close()

# --- API ROUTES ---
@app.route('/download', methods=['POST'])
def handle_download():
    try:
        data = request.json
        if not data:
            return jsonify({"status": "error", "message": "No JSON data"}), 400

        link = data.get('link', '').strip()
        chat_id = data.get('chat_id')

        # Basic Validation
        if not link or not chat_id:
            return jsonify({"status": "error", "message": "Missing link or chat_id"}), 400
        
        # Filter Bot Echoes
        if "Here is your media" in link or link.startswith("⚠️"):
             return jsonify({"status": "ignored"}), 200

        task_id = str(uuid.uuid4())
        
        # FIX: Fire and Forget!
        # Submit the task to the thread pool and return IMMEDIATELY.
        executor.submit(run_background_process, link, chat_id, task_id)
        
        logger.info(f"[{task_id}] Task Queued for: {link}")
        
        # Return success immediately to keep n8n happy
        return jsonify({
            "status": "queued", 
            "message": "Task started in background", 
            "task_id": task_id
        }), 200

    except Exception as e:
        logger.error(f"API Route Error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/', methods=['GET'])
def health_check():
    return "Bot is Alive", 200

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
