import os
import asyncio
import uuid
import gc
import re
import logging
import traceback
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

# --- WORKER: Main Logic (Returns Result Dict) ---
async def process_task(link, chat_id, task_id):
    logger.info(f"[{task_id}] Processing: {link}")
    client = None
    file_path = f"/tmp/{task_id}.mp4"
    final_path = None
    result = {"status": "failed", "message": "Unknown error"}

    try:
        # 1. Initialize Client
        client = TelegramClient(StringSession(SESSION_STR), API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            return {"status": "error", "message": "Bot unauthorized (Check SESSION_STR)"}

        # 2. Download Phase
        if "instagram.com" in link:
            final_path = await asyncio.to_thread(download_instagram_video, link, file_path)
            if not final_path:
                msg = "Instagram download failed (Private/Blocked)."
                await send_error_to_user(client, chat_id, msg)
                return {"status": "error", "message": msg}

        elif "t.me" in link:
            try:
                if "/c/" in link: 
                    match = re.search(r'/c/(\d+)/(\d+)', link)
                    if not match: raise ValueError("Invalid Link")
                    entity = await client.get_entity(int("-100" + match.group(1)))
                    mid = int(match.group(2))
                else: 
                    match = re.search(r't\.me/([^/]+)/(\d+)', link)
                    if not match: raise ValueError("Invalid Link")
                    entity = await client.get_entity(match.group(1))
                    mid = int(match.group(2))
                
                message = await client.get_messages(entity, ids=mid)
                if not message or not message.media:
                    msg = "No media found in message."
                    await send_error_to_user(client, chat_id, msg)
                    return {"status": "error", "message": msg}

                final_path = await client.download_media(message, file=file_path)
            except Exception as e:
                msg = f"Telegram error: {str(e)}"
                await send_error_to_user(client, chat_id, msg)
                return {"status": "error", "message": msg}
        else:
            return {"status": "error", "message": "Unsupported link type"}

        # 3. Upload Phase
        try:
            attrs = get_file_attributes(final_path)
            await client.send_file(
                chat_id,
                final_path,
                caption="Here is your media 📂",
                attributes=attrs,
                supports_streaming=True,
                force_document=False
            )
            result = {"status": "success", "message": "Media sent successfully"}
        except Exception as e:
            msg = f"Upload failed: {str(e)}"
            await send_error_to_user(client, chat_id, msg)
            result = {"status": "error", "message": msg}

    except Exception as e:
        logger.critical(f"[{task_id}] CRITICAL: {traceback.format_exc()}")
        if client and client.is_connected():
            await send_error_to_user(client, chat_id, "Internal Server Error")
        result = {"status": "error", "message": str(e)}

    finally:
        if client: await client.disconnect()
        if final_path: safe_delete(final_path)
        safe_delete(file_path)
        gc.collect()

    return result

# --- SYNC WRAPPER ---
def run_sync_process(link, chat_id, task_id):
    """Runs the async task in a blocking manner."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(process_task(link, chat_id, task_id))
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
        
        # BLOCKING CALL - The API will wait here until process finishes
        result = run_sync_process(link, chat_id, task_id)
        
        # Return the actual result from the process
        status_code = 200 if result['status'] == 'success' else 500
        return jsonify(result), status_code

    except Exception as e:
        logger.error(f"API Route Error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/', methods=['GET'])
def health_check():
    return "Bot is Alive", 200

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
