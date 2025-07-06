import os
import json
import base64
import asyncio
from websockets.client import connect  # CHANGED: use client.connect so extra_headers is supported
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.websockets import WebSocketDisconnect
from twilio.twiml.voice_response import VoiceResponse, Connect
from dotenv import load_dotenv
from system_prompt import SYSTEM_MESSAGE

load_dotenv()

# Configurations
openai_key = os.getenv("OPENAI_API_KEY")
if not openai_key:
    raise ValueError("OPEN API KEY MISSING")
PORT = int(os.getenv('PORT', 5050))

VOICE = "alloy"
LOG_EVENT_TYPES = [
    'error', 'response.content.done', 'rate_limits.updated',
    'response.done', 'input_audio_buffer.committed',
    'input_audio_buffer.speech_stopped', 'input_audio_buffer.speech_started',
    'session.created'
]
SHOW_TIMING_MATH = False

app = FastAPI()

@app.get("/", response_class=JSONResponse)
async def index_page():
    return {"message": "Twilio Media Stream Server is running!"}

@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    """Handle incoming call and return TwiML response to connect to Media Stream."""
    response = VoiceResponse()
    host = request.url.hostname

    tw_connect = Connect()
    tw_connect.stream(url=f'wss://{host}/media-stream')
    response.append(tw_connect)
    return HTMLResponse(content=str(response), media_type="application/xml")

@app.websocket("/media-stream")
async def handle_websocket(websocket: WebSocket):
    """Handle WebSocket connections between Twilio and OpenAI."""
    print("client connected")
    await websocket.accept()

    async with connect(
        'wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-12-17',
        extra_headers=[
            ("Authorization", f"Bearer {openai_key}"),
            ("openai-beta", "realtime=v1")
        ]
    ) as openai_ws:
        print("→ Connected to OpenAI realtime API")

        await initialize_session(openai_ws)

        # Connection specific state
        stream_sid = None
        latest_media_timestamp = 0
        last_assistant_item = None
        mark_queue = []
        response_start_timestamp_twilio = None

        async def receive_twilio():
            nonlocal stream_sid, latest_media_timestamp
            try:
                async for message in websocket.iter_text():
                    data = json.loads(message)

                    if data['event'] == 'media' and openai_ws.open:
                        latest_media_timestamp = int(data['media']['timestamp'])
                        audio_append = {
                            "type": "input_audio_buffer.append",
                            "audio": data['media']['payload']
                        }
                        await openai_ws.send(json.dumps(audio_append))

                    elif data['event'] == 'start':
                        stream_sid = data['start']['streamSid']
                        response_start_timestamp_twilio = None
                        latest_media_timestamp = 0
                        last_assistant_item = None

                    elif data['event'] == 'mark' and mark_queue:
                        mark_queue.pop(0)
            except WebSocketDisconnect:
                print("🔴 Twilio disconnected")
                if openai_ws.open:
                    await openai_ws.close()

        async def send_twilio():
            nonlocal stream_sid, last_assistant_item, response_start_timestamp_twilio
            print("▶️ send_twilio starting…")
            try:
                async for openai_message in openai_ws:
                    response = json.loads(openai_message)
                    ev = response.get("type")
                    print("🟢 OpenAI event:", ev)

                    if ev == 'response.audio.delta' and 'delta' in response:
                        # send the API's ulaw payload directly
                        audio_payload = response['delta']

                        await websocket.send_json({
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"payload": audio_payload}
                        })

                        if response_start_timestamp_twilio is None:
                            response_start_timestamp_twilio = latest_media_timestamp

                        if response.get('item_id'):
                            last_assistant_item = response['item_id']

                        await send_mark(websocket, stream_sid)

                    elif ev == 'response.done':
                        print("  → OpenAI response done")
                        await openai_ws.send(json.dumps({
                            "type": "response.create",
                            "response": {"modalities": ["audio","text"]}
                        }))

                    elif ev == 'error':
                        print("  → OpenAI error:", response.get('error'))

                    if ev == 'input_audio_buffer.speech_started' and last_assistant_item:
                        await handle_speech_started_event()

            except Exception as e:
                print("❌ Error in send_twilio:", e)

        async def handle_speech_started_event():
            nonlocal response_start_timestamp_twilio, last_assistant_item
            if mark_queue and response_start_timestamp_twilio is not None:
                elapsed_time = latest_media_timestamp - response_start_timestamp_twilio
                truncate_event = {
                    "type": "conversation.item.truncate",
                    "item_id": last_assistant_item,
                    "content_index": 0,
                    "audio_end_ms": elapsed_time
                }
                await openai_ws.send(json.dumps(truncate_event))
                await websocket.send_json({"event": "clear", "streamSid": stream_sid})
                mark_queue.clear()
                last_assistant_item = None
                response_start_timestamp_twilio = None

        async def send_mark(connection, stream_sid):
            if not stream_sid:
                return
            mark_event = {
                "event": "mark",
                "streamSID": stream_sid,
                "mark": {"name": "responsePart"}
            }
            try:
                await connection.send_json(mark_event)
                mark_queue.append('responsePart')
            except asyncio.CancelledError:
                return
            except Exception as e:
                print(f"⚠️ send_mark() failed: {e}")

        # ensure both coroutines run inside the async with (so openai_ws stays open)
        try:
            await asyncio.gather(
                receive_twilio(),
                send_twilio(),
            )
        except asyncio.CancelledError:
            # normal on shutdown or client disconnect
            pass

async def initialize_session(openai_ws):
    """Initial session setup with OpenAI, using conversation.item.create + response.create."""
    session_update = {
        "type": "session.update",
        "session": {
            "turn_detection": {"type": "server_vad"},
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "voice": VOICE,
            "instructions": SYSTEM_MESSAGE,
            "modalities": ["text", "audio"],
            "temperature": 0.8,
        }
    }
    print('→ Sending session.update')
    await openai_ws.send(json.dumps(session_update))

    print('→ Sending conversation.item.create')
    await openai_ws.send(json.dumps({
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "user",
            "content": [{
                "type": "input_text",
                "text": "Hello, this is a AI voice assistant for Ming House. How may I help you today?"
            }]
        }
    }))

    print('→ Sending response.create')
    await openai_ws.send(json.dumps({
        "type": "response.create",
        "response": {"modalities": ["audio", "text"]}
    }))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
