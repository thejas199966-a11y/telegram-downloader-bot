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

# Limit concurrent downloads to save your 1GB RAM VM
# Max 2 active downloads. Others will wait in queue.
MAX_CONCURRENT_JOBS = 2
job_semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)

# --- HELPER: Regex Link Parser (The Fix for IndexError) ---
def parse_link_robust(link):
    """
    Extracts (entity, message_id, is_private) from ANY Telegram link.
    Handles: ?single, trailing slashes, http/https, t.me/c/
    """
    link = link.strip()
    
    # 1. Handle Private Links (t.me/c/123456789/100)
    # Regex matches: /c/ followed by digits, slash, digits
    private_match = re.search(r'/c/(\d+)/(\d+)', link)
    if private_match:
        channel_id_str = private_match.group(1)
        msg_id = int(private_match.group(2))
        
        # Telethon requires private channel IDs to start with -100
        # If the ID doesn't start with -100, we prepend it.
        if not channel_id_str.startswith("-100"):
            channel_id = int("-100" + channel_id_str)
        else:
            channel_id = int(channel_id_str)
            
        return channel_id, msg_id, True # True = Private Channel

    # 2. Handle Public Links (t.me/username/100)
    # Regex matches: t.me/ followed by username (not c), slash, digits
    public_match = re.search(r't\.me/([^/]+)/(\d+)', link)
    if public_match:
        username = public_match.group(1)
        msg_id = int(public_match.group(2))
        return username, msg_id, False # False = Public Username

    raise ValueError("Invalid Link Format. Could not find Message ID.")

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

# --- WORKER: The Safe Background Process ---
async def process_video_task(link, chat_id, task_id):
    # Acquire "ticket" to run. If 2 jobs are running, this waits here.
    async with job_semaphore:
        print(f"[{task_id}] Job Started. Parsing: {link}")
        
        # Use a fresh client for every thread to ensure thread-safety
        async with TelegramClient(StringSession(SESSION_STR), API_ID, API_HASH) as client:
            path = f"/tmp/{task_id}.mp4"
            try:
                # 1. Parse Link
                entity_input, msg_id, is_private = parse_link_robust(link)
                
                # 2. Resolve Entity
                try:
                    entity = await client.get_entity(entity_input)
                except ValueError:
                    # Specific error for private chats the bot hasn't joined
                    if is_private:
                        raise Exception("I am not a member of this private chat. Add me first.")
                    else:
                        raise Exception(f"Could not find public channel: {entity_input}")

                # 3. Get Message
                message = await client.get_messages(entity, ids=msg_id)
                if not message or not message.media:
                    raise Exception("Link valid, but no video found inside (maybe text only?).")

                # 4. Download
                print(f"[{task_id}] Downloading...")
                await client.download_media(message, file=path)

                # 5. Metadata & Upload
                print(f"[{task_id}] Uploading...")
                video_attr = get_video_attributes(path)
                attrs = [video_attr] if video_attr else []
                
                gc.collect() # Clear RAM before upload
                
                uploaded_file = await client.upload_file(path, part_size_kb=2048)

                # 6. Send
                await client.send_file(
                    chat_id, 
                    uploaded_file, 
                    caption=f"Here is your video 🎥", 
                    attributes=attrs,
                    supports_streaming=True
                )
                print(f"[{task_id}] Finished Successfully.")

            except Exception as e:
                print(f"[{task_id}] FAILED: {e}")
                error_text = f"❌ **Download Error**\n\nReason: `{str(e)}`"
                try:
                    await client.send_message(chat_id, error_text)
                except:
                    pass # If we can't even send the error, just give up.

            finally:
                # 7. Guaranteed Cleanup
                if os.path.exists(path):
                    try: os.remove(path)
                    except: pass
                gc.collect()

def run_async_background(link, chat_id, task_id):
    """Boilerplate to run asyncio inside a thread"""
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
    link = data.get('link')
    chat_id = data.get('chat_id')
    
    if not link or not chat_id:
        return jsonify({"error": "Missing link or chat_id"}), 400

    task_id = str(uuid.uuid4())

    # Start the thread. The Semaphore inside handles the queuing.
    t = threading.Thread(target=run_async_background, args=(link, chat_id, task_id))
    t.start()
    
    return jsonify({
        "status": "queued",
        "message": "Processing started. You will receive the video (or an error) in Telegram shortly.",
        "task_id": task_id
    }), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
