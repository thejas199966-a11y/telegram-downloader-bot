from flask import Flask, request
from telethon import TelegramClient
from telethon.sessions import StringSession
import os
import asyncio
import uuid

app = Flask(__name__)

# Credentials
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
SESSION_STR = os.environ.get("SESSION_STR")

async def download_and_send(link, chat_id):
    async with TelegramClient(StringSession(SESSION_STR), API_ID, API_HASH) as client:
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
                return "Error: No media found in this message."

            # 3. DOWNLOAD to disk (Crucial Step to bypass restriction)
            # We give it a random name to avoid conflicts
            filename = f"/tmp/{uuid.uuid4()}.mp4"
            
            print(f"Downloading to {filename}...")
            path = await client.download_media(message, file=filename)
            
            # 4. UPLOAD back to user (As a fresh file)
            print(f"Uploading to {chat_id}...")
            await client.send_file(chat_id, path, caption="Here is your video 🎥")
            
            # 5. Clean up (Delete temp file to save space)
            os.remove(path)
            
            return "Success"
        except Exception as e:
            # Clean up if error occurs
            if 'path' in locals() and os.path.exists(path):
                os.remove(path)
            return f"Error: {str(e)}"

@app.route('/download', methods=['POST'])
def handle_download():
    data = request.json
    link = data.get('link')
    chat_id = data.get('chat_id')
    
    # Run the async task
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result = loop.run_until_complete(download_and_send(link, chat_id))
    
    return {"status": result}

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
