from flask import Flask, request, jsonify
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import DocumentAttributeVideo
import os
import asyncio
import uuid
import gc
import threading
import re
import yt_dlp # Added for Instagram

# Metadata extraction
from hachoir.metadata import extractMetadata
from hachoir.parser import createParser

app = Flask(__name__)

# --- CONFIGURATION ---
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
SESSION_STR = os.environ.get("SESSION_STR")

# Limit concurrent downloads to save 1GB RAM
MAX_CONCURRENT_JOBS = 2
job_semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)

# --- HELPER: Metadata ---
def get_video_attributes(file_path):
    parser = None
    try:
        parser = createParser(file_path)
        if not parser: return None
        metadata = extractMetadata(parser)
        if not metadata: return None
        
        duration = metadata.get("duration").seconds if metadata.has("duration") else 0
        width = metadata.get("width") if metadata.has("width") else 0
        height = metadata.get("height") if metadata.has("height") else 0

        return DocumentAttributeVideo(duration=duration, w=width, h=height, supports_streaming=True)
    except:
        return None
    finally:
        if parser: parser.close()

# --- HELPER: Telegram Link Parser ---
def parse_link_robust(link):
    """
    Parses Telegram link. Returns (entity, msg_id, is_private).
    Returns NONE if invalid.
    """
    link = link.strip()
    
    # 1. Private Links: t.me/c/123456/100
    private_match = re.search(r'/c/(\d+)/(\d+)', link)
    if private_match:
        channel_id_str = private_match.group(1)
        msg_id = int(private_match.group(2))
        channel_id = int("-100" + channel_id_str) if not channel_id_str.startswith("-100") else int(channel_id_str)
        return channel_id, msg_id, True

    # 2. Public Links: t.me/username/100
    public_match = re.search(r't\.me/([^/]+)/(\d+)', link)
    if public_match:
        username = public_match.group(1)
        if username == 'c': return None 
        msg_id = int(public_match.group(2))
        return username, msg_id, False

    return None

# --- HELPER: Instagram Downloader ---
def download_instagram_video(link, output_path):
    """
    Downloads video from Instagram using yt-dlp.
    """
    # yt-dlp options
    ydl_opts = {
        'outtmpl': output_path,     # Force specific filename
        'format': 'best[ext=mp4]/best', # Prefer MP4
        'quiet': True,              # Don't spam logs
        'no_warnings': True,
        # 'cookiefile': 'cookies.txt' # Uncomment if you ever add cookies for private reels
    }
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([link])

# --- WORKER: Background Process ---
async def process_video_task(link, chat_id, task_id):
    async with job_semaphore:
        print(f"[{task_id}] Processing: {link}")
        
        # Use a fresh client for every thread
        async with TelegramClient(StringSession(SESSION_STR), API_ID, API_HASH) as client:
            path = f"/tmp/{task_id}.mp4"
            try:
                # --- FORK: Check if Instagram or Telegram ---
                
                if "instagram.com" in link:
                    # === INSTAGRAM PATH ===
                    print(f"[{task_id}] Detected Instagram link. Downloading via yt-dlp...")
                    # Run sync function in this async context (safe because we are in a dedicated thread)
                    download_instagram_video(link, path)
                    
                    if not os.path.exists(path):
                        raise Exception("Download failed (file not created). Link might be private or invalid.")

                else:
                    # === TELEGRAM PATH ===
                    parsed = parse_link_robust(link)
                    if not parsed:
                        print(f"[{task_id}] Ignored invalid Telegram link.")
                        return

                    entity_input, msg_id, is_private = parsed
                    
                    # Resolve Entity
                    try:
                        entity = await client.get_entity(entity_input)
                    except Exception as e:
                        if is_private:
                            raise Exception("I am not a member of this private chat.")
                        else:
                            raise Exception(f"Channel not found: {entity_input}")

                    # Get Message
                    message = await client.get_messages(entity, ids=msg_id)
                    if not message or not message.media:
                        raise Exception("No video found in this message.")

                    # Download
                    print(f"[{task_id}] Downloading from Telegram...")
                    await client.download_media(message, file=path)


                # === COMMON PATH (Upload & Send) ===
                
                # 1. Metadata
                print(f"[{task_id}] extracting metadata...")
                video_attr = get_video_attributes(path)
                attrs = [video_attr] if video_attr else []
                gc.collect()
                
                # 2. Upload (512KB chunk size)
                print(f"[{task_id}] Uploading...")
                uploaded_file = await client.upload_file(path, part_size_kb=512)

                # 3. Send
                await client.send_file(
                    chat_id, 
                    uploaded_file, 
                    caption=f"Here is your video 🎥", 
                    attributes=attrs,
                    supports_streaming=True
                )
                print(f"[{task_id}] Success.")

            except Exception as e:
                print(f"[{task_id}] FAILED: {e}")
                error_str = str(e)
                # Ignore invalid link errors to prevent loops
                if "invalid link" not in error_str.lower():
                    try:
                        await client.send_message(chat_id, f"❌ **Error:** `{error_str}`")
                    except:
                        pass

            finally:
                if os.path.exists(path):
                    try: os.remove(path)
                    except: pass
                gc.collect()

def run_async_background(link, chat_id, task_id):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(process_video_task(link, chat_id, task_id))
    finally:
        loop.close()

# --- API ROUTE ---
@app.route('/download', methods=['POST'])
def handle_download():
    data = request.json
    link = data.get('link', '').strip()
    chat_id = data.get('chat_id')
    
    # --- SAFETY CHECKS ---
    
    # 1. Check for Bot Echoes
    if link.startswith("❌") or "Here is your video" in link:
        return jsonify({"status": "ignored", "reason": "Bot message detected"}), 200

    # 2. Check for Valid Domain (Telegram OR Instagram)
    is_telegram = "t.me" in link
    is_instagram = "instagram.com" in link
    
    if not link or (not is_telegram and not is_instagram):
        print(f"Ignored unsupported input: {link[:50]}...")
        return jsonify({"status": "ignored", "reason": "Not a Telegram or Instagram link"}), 200

    # --- END SAFETY CHECKS ---

    task_id = str(uuid.uuid4())
    t = threading.Thread(target=run_async_background, args=(link, chat_id, task_id))
    t.start()
    
    return jsonify({"status": "queued", "task_id": task_id}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
