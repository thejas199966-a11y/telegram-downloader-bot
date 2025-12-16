from flask import Flask, request, jsonify
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import DocumentAttributeVideo
import os
import asyncio
import uuid
import gc
import threading

# Metadata extraction
from hachoir.metadata import extractMetadata
from hachoir.parser import createParser

app = Flask(__name__)

# Credentials
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
SESSION_STR = os.environ.get("SESSION_STR")

# --- HELPER: Memory-Safe Metadata Extraction ---
def get_video_attributes(file_path):
    parser = None
    try:
        parser = createParser(file_path)
        if not parser:
            return None
        metadata = extractMetadata(parser)
        if not metadata:
            return None
            
        duration = metadata.get("duration").seconds if metadata.has("duration") else 0
        width = metadata.get("width") if metadata.has("width") else 0
        height = metadata.get("height") if metadata.has("height") else 0

        return DocumentAttributeVideo(
            duration=duration,
            w=width,
            h=height,
            supports_streaming=True
        )
    except Exception as e:
        print(f"Metadata error: {e}")
        return None
    finally:
        if parser:
            parser.close()

# --- WORKER: Background Process ---
async def process_video(link, chat_id, task_id):
    """
    Runs in background. 
    Sends Video if success. 
    Sends Text Message if failed.
    """
    print(f"[{task_id}] Background task started for: {link}")
    
    # Create a new client instance for this thread
    async with TelegramClient(StringSession(SESSION_STR), API_ID, API_HASH) as client:
        path = None
        try:
            # 1. Parse Link
            if '/c/' in link:
                parts = link.split('/')
                channel_id = int("-100" + parts[-2])
                msg_id = int(parts[-1])
                entity = await client.get_entity(channel_id)
            else:
                parts = link.split('/')
                entity = parts[-2]
                msg_id = int(parts[-1])

            # 2. Get Message
            message = await client.get_messages(entity, ids=msg_id)
            if not message or not message.media:
                await client.send_message(chat_id, "⚠️ Error: No video found at that link.")
                return

            # 3. Download to disk
            path = f"/tmp/{task_id}.mp4"
            print(f"[{task_id}] Downloading to {path}...")
            await client.download_media(message, file=path)
            
            # 4. Extract Metadata
            video_attr = get_video_attributes(path)
            attrs = [video_attr] if video_attr else []

            # Force clean RAM
            gc.collect()

            # 5. UPLOAD (2MB chunks for speed + safety)
            print(f"[{task_id}] Uploading safely (2048KB chunks)...")
            uploaded_file = await client.upload_file(
                path, 
                part_size_kb=2048
            )

            # 6. SEND VIDEO (Success Case)
            print(f"[{task_id}] Sending video to chat {chat_id}...")
            await client.send_file(
                chat_id, 
                uploaded_file, 
                caption="Here is your video 🎥",
                attributes=attrs,
                supports_streaming=True
            )
            print(f"[{task_id}] Success.")

        except Exception as e:
            # 7. SEND ERROR MESSAGE (Failure Case)
            print(f"[{task_id}] Failed: {e}")
            error_msg = f"❌ **Download Failed**\n\nReason: `{str(e)}`"
            try:
                await client.send_message(chat_id, error_msg)
            except:
                print(f"[{task_id}] Could not send error message to user.")
            
        finally:
            # 8. Cleanup
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except:
                    pass
            gc.collect()

def run_async_background(link, chat_id, task_id):
    """Wrapper to run async function in a thread"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(process_video(link, chat_id, task_id))
    finally:
        loop.close()

# --- ROUTES ---
@app.route('/download', methods=['POST'])
def handle_download():
    data = request.json
    link = data.get('link')
    chat_id = data.get('chat_id')
    
    if not link or not chat_id:
        return jsonify({"error": "Missing link or chat_id"}), 400

    # Generate Task ID
    task_id = str(uuid.uuid4())

    # Start Background Thread
    # This keeps n8n happy with a fast response
    t = threading.Thread(target=run_async_background, args=(link, chat_id, task_id))
    t.start()
    
    # Immediate response to n8n
    return jsonify({
        "status": "queued",
        "message": "Processing started. You will receive the video (or an error) in Telegram shortly.",
        "task_id": task_id
    }), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
