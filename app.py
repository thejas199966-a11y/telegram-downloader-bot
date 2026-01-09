import os
import asyncio
import uuid
import gc
import threading
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
    """Safely deletes a file without throwing errors."""
    try:
        if path and os.path.exists(path):
            os.remove(path)
            logger.info(f"Deleted temp file: {path}")
    except Exception as e:
        logger.error(f"Failed to delete {path}: {e}")

# --- SAFETY HELPER: Send Error ---
async def send_error_to_user(client, chat_id, message):
    """Safely sends an error message to the user."""
    try:
        if client and client.is_connected():
            await client.send_message(chat_id, f"⚠️ **Task Failed**\nReason: {message}")
    except Exception as e:
        logger.error(f"Could not send error message to user: {e}")

# --- HELPER: Metadata ---
def get_file_attributes(file_path):
    """Extracts attributes (Duration, Width, Height) safely."""
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
        # If metadata fails, we return empty list. We do NOT stop the process.
        return []
    finally:
        if parser: parser.close()

# --- HELPER: Instagram Downloader ---
def download_instagram_video(link, output_path):
    """
    Downloads from Instagram. Returns True if file exists, False otherwise.
    """
    try:
        ua = UserAgent()
        random_ua = ua.random
        logger.info(f"Using Fake User-Agent: {random_ua}")

        ydl_opts = {
            'outtmpl': output_path,
            'format': 'best[ext=mp4]/best',
            'quiet': True,
            'no_warnings': True,
            'ignoreerrors': True, # CRITICAL: Don't crash on minor warnings
            'nocheckcertificate': True,
            'user_agent': random_ua,
            'http_headers': {
                'Accept-Language': 'en-US,en;q=0.9',
                'Sec-Fetch-Mode': 'navigate',
            }
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([link])
            
        # Verify file existence (yt-dlp might append extensions)
        if os.path.exists(output_path): return output_path
        
        # Check variations (.mkv, .webm)
        base = output_path.rsplit('.', 1)[0]
        for ext in ['.mp4', '.mkv', '.webm']:
            if os.path.exists(base + ext):
                return base + ext
                
        return None
    except Exception as e:
        logger.error(f"Instagram Download Error: {e}")
        return None

# --- WORKER: Main Logic ---
async def process_task(link, chat_id, task_id):
    logger.info(f"[{task_id}] Processing: {link}")
    client = None
    file_path = f"/tmp/{task_id}.mp4"
    final_path = None

    try:
        # 1. Initialize Client
        try:
            client = TelegramClient(StringSession(SESSION_STR), API_ID, API_HASH)
            await client.connect()
            if not await client.is_user_authorized():
                logger.error("Session Invalid")
                # We can't even tell the user because we aren't authorized
                return 
        except Exception as e:
            logger.critical(f"[{task_id}] Telethon Connection Failed: {e}")
            return # Stop if we can't connect

        # 2. Download Phase
        if "instagram.com" in link:
            logger.info(f"[{task_id}] Type: Instagram")
            # Run blocking code in thread
            final_path = await asyncio.to_thread(download_instagram_video, link, file_path)
            
            if not final_path:
                await send_error_to_user(client, chat_id, "Instagram download failed. Content might be private or server IP is blocked.")
                return

        elif "t.me" in link:
            logger.info(f"[{task_id}] Type: Telegram")
            
            # Link Parsing
            try:
                if "/c/" in link: # Private
                    # Regex: t.me/c/CHANNEL_ID/MSG_ID
                    match = re.search(r'/c/(\d+)/(\d+)', link)
                    if not match: raise ValueError("Invalid Private Link format")
                    cid = int("-100" + match.group(1))
                    mid = int(match.group(2))
                    entity = await client.get_entity(cid)
                else: # Public
                    # Regex: t.me/USERNAME/MSG_ID
                    match = re.search(r't\.me/([^/]+)/(\d+)', link)
                    if not match: raise ValueError("Invalid Public Link format")
                    user = match.group(1)
                    mid = int(match.group(2))
                    entity = await client.get_entity(user)
                
                message = await client.get_messages(entity, ids=mid)
                
                if not message or not message.media:
                    await send_error_to_user(client, chat_id, "No media found in that message.")
                    return

                final_path = await client.download_media(message, file=file_path)
                if not final_path:
                    raise Exception("Download completed but file is missing.")

            except ValueError as ve:
                await send_error_to_user(client, chat_id, "Invalid Link Format.")
                return
            except errors.rpcerrorlist.ChannelPrivateError:
                await send_error_to_user(client, chat_id, "I cannot access this private channel. Make sure I am a member.")
                return
            except Exception as e:
                await send_error_to_user(client, chat_id, f"Telegram Download Error: {str(e)}")
                return
        
        else:
            await send_error_to_user(client, chat_id, "Unsupported Link.")
            return

        # 3. Upload Phase
        try:
            logger.info(f"[{task_id}] Extracting metadata...")
            attrs = get_file_attributes(final_path)

            logger.info(f"[{task_id}] Uploading to Telegram...")
            await client.send_file(
                chat_id,
                final_path,
                caption="Here is your media 📂",
                attributes=attrs,
                supports_streaming=True,
                force_document=False
            )
            logger.info(f"[{task_id}] Success!")

        except errors.rpcerrorlist.EntityTooLargeError:
            await send_error_to_user(client, chat_id, "File is too large for Telegram API (Limit is 2GB).")
        except Exception as e:
            await send_error_to_user(client, chat_id, f"Upload Failed: {str(e)}")

    except Exception as e:
        # Catch-all for unforeseen crashes
        logger.critical(f"[{task_id}] CRITICAL FAILURE: {traceback.format_exc()}")
        if client and client.is_connected():
            await send_error_to_user(client, chat_id, "Internal Server Error.")

    finally:
        # 4. Cleanup Phase
        if client:
            await client.disconnect()
        
        if final_path: safe_delete(final_path)
        safe_delete(file_path) # Clean initial path just in case
        gc.collect()

# --- THREAD WRAPPER ---
def run_background_loop(link, chat_id, task_id):
    """Creates a fresh asyncio loop for the thread."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(process_task(link, chat_id, task_id))
    except Exception as e:
        logger.error(f"Thread loop crashed: {e}")
    finally:
        loop.close()

# --- API ROUTES ---
@app.route('/download', methods=['POST'])
def handle_download():
    try:
        data = request.json
        if not data:
            return jsonify({"status": "error", "reason": "No JSON data"}), 400

        link = data.get('link', '').strip()
        chat_id = data.get('chat_id')

        # Basic Validation
        if not link or not chat_id:
            return jsonify({"status": "ignored", "reason": "Missing link or chat_id"}), 200
        
        # Filter Bot Echoes
        if "Here is your media" in link or link.startswith("⚠️"):
             return jsonify({"status": "ignored"}), 200

        task_id = str(uuid.uuid4())
        
        # Start Background Thread
        t = threading.Thread(target=run_background_loop, args=(link, chat_id, task_id))
        t.start()
        
        return jsonify({"status": "queued", "task_id": task_id}), 200

    except Exception as e:
        logger.error(f"API Route Error: {e}")
        return jsonify({"status": "error", "reason": str(e)}), 500

@app.route('/', methods=['GET'])
def health_check():
    return "Bot is Alive", 200

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
