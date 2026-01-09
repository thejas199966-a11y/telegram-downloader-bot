from flask import Flask, request, jsonify
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import DocumentAttributeVideo, DocumentAttributeAudio
import os
import asyncio
import uuid
import gc
import threading
import re
import yt_dlp
from fake_useragent import UserAgent

# Metadata extraction
from hachoir.metadata import extractMetadata
from hachoir.parser import createParser

app = Flask(__name__)

# --- CONFIGURATION ---
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
SESSION_STR = os.environ.get("SESSION_STR")

# --- HELPER: Metadata ---
def get_file_attributes(file_path):
    """
    Extracts video/audio attributes for Telegram.
    """
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
            
            # Add Video Attribute
            if width > 0:
                attrs.append(DocumentAttributeVideo(
                    duration=duration, w=width, h=height, supports_streaming=True
                ))
            # Add Audio Attribute (if it looks like audio)
            else:
                attrs.append(DocumentAttributeAudio(
                    duration=duration, title="Downloaded Media"
                ))
        return attrs
    except:
        return []
    finally:
        if parser: parser.close()

# --- HELPER: Telegram Link Parser ---
def parse_link_robust(link):
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
    Downloads media from Instagram using yt-dlp with anti-blocking measures.
    """
    # Generate a random user agent to mimic a real browser
    ua = UserAgent()
    random_ua = ua.random

    ydl_opts = {
        'outtmpl': output_path,
        'format': 'best[ext=mp4]/best', # Prioritize MP4
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'ignoreerrors': True, # Keep going even if some parts fail
        
        # --- ANTI-BLOCKING CONFIGURATION ---
        'user_agent': random_ua,
        'referer': 'https://www.instagram.com/',
        'http_headers': {
            'Accept-Language': 'en-US,en;q=0.9',
            'Sec-Fetch-Mode': 'navigate',
        }
    }
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([link])

# --- WORKER: Background Process ---
async def process_video_task(link, chat_id, task_id):
    print(f"[{task_id}] Processing started: {link}")
    
    # Use a fresh client session for isolation
    client = TelegramClient(StringSession(SESSION_STR), API_ID, API_HASH)
    
    path = f"/tmp/{task_id}.mp4" # Default extension, yt-dlp might change it
    
    try:
        await client.connect()
        if not await client.is_user_authorized():
             raise Exception("Client not authorized. Check SESSION_STR.")

        # --- DOWNLOAD PHASE ---
        if "instagram.com" in link:
            print(f"[{task_id}] Downloading Instagram media...")
            # Run sync yt-dlp in a separate thread to not block the asyncio loop
            await asyncio.to_thread(download_instagram_video, link, path)
            
            # Handle case where yt-dlp saves as .mkv or other extension
            if not os.path.exists(path):
                # Check for other extensions
                base_name = f"/tmp/{task_id}"
                for ext in ['.mkv', '.webm', '.mp4']:
                    if os.path.exists(base_name + ext):
                        path = base_name + ext
                        break
            
            if not os.path.exists(path):
                raise Exception("Download failed. Link might be private or IP blocked.")

        else:
            # === TELEGRAM PATH ===
            parsed = parse_link_robust(link)
            if not parsed:
                print(f"[{task_id}] Invalid Telegram link.")
                return

            entity_input, msg_id, is_private = parsed
            
            try:
                entity = await client.get_entity(entity_input)
                message = await client.get_messages(entity, ids=msg_id)
                
                if not message or not message.media:
                    raise Exception("No media found in this message.")

                print(f"[{task_id}] Downloading Telegram media...")
                
                # Download with progress callback (optional, kept simple here)
                path = await client.download_media(message, file=f"/tmp/{task_id}")
                if not path:
                    raise Exception("Download returned no file path.")
                    
            except Exception as e:
                if is_private:
                    raise Exception("Cannot access private chat. Bot must be a member.")
                raise e

        # --- UPLOAD PHASE ---
        print(f"[{task_id}] Extracting metadata for {path}...")
        attrs = get_file_attributes(path)
        
        print(f"[{task_id}] Uploading to Telegram...")
        
        # Allow unlimited upload size (Telethon handles chunking)
        # We use a custom caption
        await client.send_file(
            chat_id, 
            path, 
            caption=f"Here is your media 📂", 
            attributes=attrs,
            force_document=False, # Let Telegram decide (video vs file)
            supports_streaming=True
        )
        print(f"[{task_id}] Task Completed.")

    except Exception as e:
        print(f"[{task_id}] FAILED: {e}")
        try:
            await client.send_message(chat_id, f"❌ **Error:** `{str(e)}`")
        except:
            pass

    finally:
        # Cleanup
        await client.disconnect()
        if os.path.exists(path):
            try: os.remove(path)
            except: pass
        # Clean up possible variations
        try:
            for f in os.listdir("/tmp"):
                if f.startswith(task_id):
                    os.remove(os.path.join("/tmp", f))
        except: pass
        gc.collect()

def run_async_background(link, chat_id, task_id):
    """
    Thread entry point. Creates a new event loop for this thread.
    """
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
    
    # 1. Filter junk
    if not link or "Here is your media" in link or link.startswith("❌"):
        return jsonify({"status": "ignored"}), 200

    # 2. Validation
    is_telegram = "t.me" in link
    is_instagram = "instagram.com" in link
    
    if not (is_telegram or is_instagram):
        return jsonify({"status": "ignored", "reason": "Unsupported link"}), 200

    # 3. Queue Task
    task_id = str(uuid.uuid4())
    print(f"[{task_id}] Queued: {link}")
    
    # Daemon thread ensures it doesn't block Flask shutdown if needed, 
    # but for Render web services, it keeps running until the request times out or finishes.
    t = threading.Thread(target=run_async_background, args=(link, chat_id, task_id))
    t.start()
    
    return jsonify({"status": "queued", "task_id": task_id}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
