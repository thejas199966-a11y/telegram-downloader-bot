from flask import Flask, request
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import DocumentAttributeVideo
import os
import asyncio
import uuid

# New libraries for metadata extraction
from hachoir.metadata import extractMetadata
from hachoir.parser import createParser

app = Flask(__name__)

# Credentials
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
SESSION_STR = os.environ.get("SESSION_STR")

# --- HELPER FUNCTION TO FIX ASPECT RATIO ---
def get_video_attributes(file_path):
    try:
        parser = createParser(file_path)
        if not parser:
            return None
        metadata = extractMetadata(parser)
        if not metadata:
            return None
            
        # Extract details
        duration = 0
        if metadata.has("duration"):
            duration = metadata.get("duration").seconds
            
        width = 0
        if metadata.has("width"):
            width = metadata.get("width")
            
        height = 0
        if metadata.has("height"):
            height = metadata.get("height")

        # Create the Telegram attribute that defines the ratio
        return DocumentAttributeVideo(
            duration=duration,
            w=width,
            h=height,
            supports_streaming=True
        )
    except Exception as e:
        print(f"Metadata extraction failed: {e}")
        return None
# -------------------------------------------

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

            # 3. Download
            filename = f"/tmp/{uuid.uuid4()}.mp4"
            print(f"Downloading to {filename}...")
            path = await client.download_media(message, file=filename)
            
            # 4. Extract Metadata (The Fix)
            print("Extracting metadata...")
            video_attributes = get_video_attributes(path)
            
            attrs = []
            if video_attributes:
                attrs.append(video_attributes)

            # 5. Upload with Attributes
            print(f"Uploading to {chat_id}...")
            await client.send_file(
                chat_id, 
                path, 
                caption="Here is your video 🎥",
                attributes=attrs, # <--- This fixes the ratio
                supports_streaming=True
            )
            
            return "Success"

        except Exception as e:
            return f"Error: {str(e)}"
            
        finally:
            # 6. Cleanup
            if path and os.path.exists(path):
                os.remove(path)

@app.route('/download', methods=['POST'])
def handle_download():
    data = request.json
    link = data.get('link')
    chat_id = data.get('chat_id')
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result = loop.run_until_complete(download_and_send(link, chat_id))
    
    return {"status": result}

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
