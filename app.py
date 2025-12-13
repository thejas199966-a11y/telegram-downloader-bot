from flask import Flask, request, jsonify
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import DocumentAttributeVideo
import os
import asyncio
import uuid
import gc
import json

# --- NEW IMPORT FOR TRUECALLER ---
from truecallerpy import search_phonenumber

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
# -----------------------------------------------

async def download_and_send(link, chat_id):
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
                return "Error: No media found."

            # 3. Download to disk
            path = f"/tmp/{uuid.uuid4()}.mp4"
            print(f"Downloading to {path}...")
            await client.download_media(message, file=path)
            
            # 4. Extract Metadata
            print("Extracting metadata...")
            video_attr = get_video_attributes(path)
            attrs = [video_attr] if video_attr else []

            # Force clean RAM
            gc.collect()

            # 5. UPLOAD (The RAM Fix)
            print(f"Uploading safely (512KB chunks)...")
            
            # We use upload_file with a small part_size_kb to keep RAM usage low.
            uploaded_file = await client.upload_file(
                path, 
                part_size_kb=512
            )

            # 6. SEND the file reference
            print(f"Sending to chat {chat_id}...")
            await client.send_file(
                chat_id, 
                uploaded_file, 
                caption="Here is your video 🎥",
                attributes=attrs,
                supports_streaming=True
            )
            
            return "Success"

        except Exception as e:
            print(f"Error: {e}")
            return f"Error: {str(e)}"
            
        finally:
            # 7. Cleanup
            if path and os.path.exists(path):
                os.remove(path)
            gc.collect()

@app.route('/download', methods=['POST'])
def handle_download():
    data = request.json
    link = data.get('link')
    chat_id = data.get('chat_id')
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result = loop.run_until_complete(download_and_send(link, chat_id))
    
    return {"status": result}

# --- NEW ROUTE: TRUECALLER SEARCH ---
@app.route('/api/truecaller', methods=['GET'])
def truecaller_search():
    # 1. Get Phone Number from URL parameters
    phone_number = request.args.get('phone')
    
    # 2. Basic Validation
    if not phone_number:
        return jsonify({"error": "Please provide a phone number"}), 400

    # 3. Get Installation ID from Environment Variable
    installation_id = os.environ.get("TRUECALLER_INSTALLATION_ID")
    
    if not installation_id:
        return jsonify({"error": "Server misconfiguration: Missing Installation ID"}), 500

    try:
        # 4. Perform the Search
        # We default to "IN" (India) if no country code is provided in the logic
        result = search_phonenumber(phone_number, "IN", installation_id)
        
        # The library returns a JSON string, so we parse it to a dict
        if isinstance(result, str):
            result = json.loads(result)
            
        return jsonify(result)

    except Exception as e:
        return jsonify({"error": str(e)}), 500
# ------------------------------------

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
