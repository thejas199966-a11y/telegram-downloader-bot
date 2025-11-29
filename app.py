from flask import Flask, request
from telethon import TelegramClient, events
from telethon.sessions import StringSession
import os
import asyncio

app = Flask(__name__)

# Credentials from Environment Variables
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
SESSION_STR = os.environ.get("SESSION_STR")

async def download_and_send(link, chat_id):
    async with TelegramClient(StringSession(SESSION_STR), API_ID, API_HASH) as client:
        try:
            # 1. Parse Link
            if '/c/' in link:
                # Private: https://t.me/c/CHANNEL_ID/MSG_ID
                parts = link.split('/')
                channel_id = int("-100" + parts[-2])
                msg_id = int(parts[-1])
                entity = await client.get_entity(channel_id)
            else:
                # Public: https://t.me/USERNAME/MSG_ID
                parts = link.split('/')
                entity = parts[-2]
                msg_id = int(parts[-1])

            # 2. Get Message
            message = await client.get_messages(entity, ids=msg_id)

            # 3. Stream Download & Upload directly to user (Bypasses local storage limits)
            # Note: We send the file directly to the user who asked (chat_id)
            await client.send_file(chat_id, message.media, caption="Here is your requested media 🎥")
            return "Success"
        except Exception as e:
            return f"Error: {str(e)}"

@app.route('/download', methods=['POST'])
def handle_download():
    data = request.json
    link = data.get('link')
    chat_id = data.get('chat_id')

    # Run the async Telegram task
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result = loop.run_until_complete(download_and_send(link, chat_id))

    return {"status": result}

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
