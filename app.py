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

# --- HELPER: Robust Link Parser ---
def parse_link_robust(link):
    """
    Parses link. Returns (entity, msg_id, is_private).
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

# --- WORKER: Background Process ---
async def process_video_task(link, chat_id, task_id):
    async with job_semaphore:
        print(f"[{task_id}] Processing: {link}")
        
        async with TelegramClient(StringSession(SESSION_STR), API_ID, API_HASH) as client:
            path = f"/tmp/{task_id}.mp4"
            try:
                # 1. Parse Logic
                parsed = parse_link_robust(link)
                if not parsed:
                    print(f"[{task_id}] Ignored invalid link format.")
                    return

                entity_input, msg_id, is_private = parsed
                
                # 2. Resolve Entity
                try:
                    entity = await client.get_entity(entity_input)
                except Exception as e:
                    if is_private:
                        raise Exception("I am not a member of this private chat.")
                    else:
                        raise Exception(f"Channel not found: {entity_input}")

                # 3. Get Message
                message = await client.get_messages(entity, ids=msg_id)
                if not message or not message.media:
                    raise Exception("No video found in this message.")

                # 4. Download
                print(f"[{task_id}] Downloading...")
                await client.download_media(message, file=path)

                # 5. Metadata & Upload
                print(f"[{task_id}] Uploading...")
                video_attr = get_video_attributes(path)
                attrs = [video_attr] if video_attr else []
                gc.collect()
                
                uploaded_file = await client.upload_file(path, part_size_kb=2048)

                # 6. Send
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
                # ERROR PROTECTION: Only send user-facing error if it's NOT a self-loop
                error_str = str(e)
                if "invalid link" not in error_str.lower():
                    try:
                        # We use a standard prefix so we can filter it out later
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
    
    # --- 🛡️ CRITICAL SAFETY CHECKS 🛡️ ---
    
    # 1. Check for Bot's own messages (Echo Protection)
    # If the text starts with specific bot phrases or emojis, IGNORE IT.
    # This stops the loop even if the text contains a valid link.
    if link.startswith("❌") or "Here is your video" in link or "Processing started" in link:
        print(f"Ignored Bot Echo: {link[:50]}...")
        return jsonify({"status": "ignored", "reason": "Bot message detected"}), 200

    # 2. Check for Link Validity
    if not link or "t.me" not in link:
        print(f"Ignored non-link input: {link[:50]}...")
        return jsonify({"status": "ignored", "reason": "No Telegram link found"}), 200

    # --- END SAFETY CHECKS ---

    task_id = str(uuid.uuid4())
    t = threading.Thread(target=run_async_background, args=(link, chat_id, task_id))
    t.start()
    
    return jsonify({"status": "queued", "task_id": task_id}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
