import os
import json
import base64
import asyncio
import audioop
from websockets.client import connect
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.websockets import WebSocketDisconnect
from twilio.twiml.voice_response import VoiceResponse, Connect, Say, Stream
from dotenv import load_dotenv
from system_prompt import SYSTEM_MESSAGE


load_dotenv() 

# Configurations
openai_key = os.getenv("OPENAI_API_KEY")
pinecone_key = os.getenv("PINECONE_API_KEY")
pinecone_env = os.getenv("PINECONE_ENV")

PORT = os.getenv('PORT', 5050)

VOICE = "alloy"
LOG_EVENT_TYPES = [
    'error', 'response.content.done', 'rate_limits.updated',
    'response.done', 'input_audio_buffer.committed',
    'input_audio_buffer.speech_stopped', 'input_audio_buffer.speech_started',
    'session.created'
]
SHOW_TIMING_MATH = False

app = FastAPI()

if not openai_key:
    raise ValueError("OPEN API KEY MISSING")

@app.get("/", response_class=JSONResponse)
async def index_page():
    return {"message": "Twilio Media Stream Server is running!"}

@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    """Handle incoming call and return TwiML response to connect to Media Stream."""
    response = VoiceResponse()
    # CHANGE THIS TO BE DYNANMIC BUT ITS HARDCODED RIGHT NOW
    response.say("Hello, this is a AI voice assistant for Ming House")
    host = request.url.hostname

    connect = Connect()
    connect.stream(url=f'wss://{host}/media-stream')
    response.append(connect)
    return HTMLResponse(content=str(response), media_type="application/xml")

@app.websocket("/media-stream")
async def handle_websocket(websocket: WebSocket):
    """Handle WebSocket connections between Twilio and OpenAI."""
    print("client connected")
    await websocket.accept()

    async with connect(
        'wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01',
        extra_headers=[
            ("Authorization", f"Bearer {openai_key}"),
            ("openai-beta", "realtime=v1")       # ← REQUIRED for realtime preview
        ]
    ) as openai_ws:
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
                    # DEBUG: log every Twilio event
                    print("🔵 Twilio event:", data.get("event"), data)

                    if data['event'] == 'media' and openai_ws.open:
                        latest_media_timestamp = int(data['media']['timestamp'])
                        # DEBUG: show size of incoming payload
                        print(f"  → media.payload length: {len(data['media']['payload'])} chars")

                        audio_append = {
                            "type": "input_audio_buffer.append",
                            "audio": data['media']['payload']
                        }
                        await openai_ws.send(json.dumps(audio_append))

                    elif data['event'] == 'start':
                        stream_sid = data['start']['streamSid']
                        print(f"  → stream started: {stream_sid}")
                        response_start_timestamp_twilio = None
                        latest_media_timestamp = 0
                        last_assistant_item = None

                    elif data['event'] == 'mark' and mark_queue:
                        _ = mark_queue.pop(0)
            except WebSocketDisconnect:
                print("🔴 Twilio disconnected")
                if openai_ws.open:
                    await openai_ws.close()


        async def send_twilio():
            nonlocal stream_sid, last_assistant_item, response_start_timestamp_twilio
            try:
                async for openai_message in openai_ws:
                    response = json.loads(openai_message)
                    # DEBUG: log every OpenAI event type
                    ev = response.get("type")
                    print("🟢 OpenAI event:", ev)

                    if ev == 'response.audio.delta' and 'delta' in response:
                        print(f"  → got audio.delta of {len(response['delta'])} chars")
                        raw_pcm = base64.b64decode(response['delta'])
                        ulaw_bytes = audioop.lin2ulaw(raw_pcm, 2)
                        audio_payload = base64.b64encode(ulaw_bytes).decode('utf-8')

                        await websocket.send_json({
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"payload": audio_payload}
                        })
                        print("  → forwarded μ-law chunk to Twilio")

                        if response_start_timestamp_twilio is None:
                            response_start_timestamp_twilio = latest_media_timestamp
                            print("  → first chunk timestamp:", response_start_timestamp_twilio)

                        if response.get('item_id'):
                            last_assistant_item = response['item_id']

                        await send_mark(websocket, stream_sid)

                    elif ev == 'response.done':
                        print("  → OpenAI response done")

                    elif ev == 'error':
                        print("  → OpenAI error:", response.get('error'))

                    # interruption logic unchanged
                    if ev == 'input_audio_buffer.speech_started' and last_assistant_item:
                        print("  → speech_started, truncating…")
                        await handle_speech_started_event()

            except Exception as e:
                print("❌ Error in send_twilio:", e)

    async def handle_speech_started_event():
        """Interrupts openAI when the caller speech starts"""
        nonlocal response_start_timestamp_twilio, last_assistant_item
        print("Handling speech started event.")

        if mark_queue and response_start_timestamp_twilio is not None:
            elapsed_time = latest_media_timestamp - response_start_timestamp_twilio
            if SHOW_TIMING_MATH:
                print(f"Calculating elapsed time for truncation: {latest_media_timestamp} - {response_start_timestamp_twilio} = {elapsed_time}ms")
            
            if last_assistant_item:
                if SHOW_TIMING_MATH:
                    print(f"Truncating item with ID: {last_assistant_item}, Truncated at: {elapsed_time}ms")

            truncate_event = {
                        "type": "conversation.item.truncate",
                        "item_id": last_assistant_item,
                        "content_index": 0,
                        "audio_end_ms": elapsed_time
                    }
            await openai_ws.send(json.dumps(truncate_event))

            await websocket.send_json({
                    "event": "clear",
                    "streamSid": stream_sid
                })

            mark_queue.clear()
            last_assistant_item = None
            response_start_timestamp_twilio = None

    async def send_mark(connection, stream_sid):
        if stream_sid: 
            mark_event = {
                "event": "mark", 
                "streamSID": stream_sid,
                "mark": {"name": "responsePart"}
            }

            await connection.send_json(mark_event)
            mark_queue.append('responsePart')

    await asyncio.gather(receive_twilio(), send_twilio())

async def initialize_session(openai_ws):
    """Initial session setup with OpenAI, now using conversation.item.create + response.create."""
    # 1) Send session.update as before
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
    print('Sending session update:', json.dumps(session_update))
    await openai_ws.send(json.dumps(session_update))

    # 2) Create the initial user message item
    await openai_ws.send(json.dumps({
        "type": "conversation.item.create",    # add the user prompt to the conversation :contentReference[oaicite:0]{index=0}
        "item": {
            "type": "message",
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": "Hello, this is a AI voice assistant for Ming House. How may I help you today?"
                }
            ]
        }
    }))

    # 3) Tell the API to start generating a response (audio) :contentReference[oaicite:1]{index=1}
    await openai_ws.send(json.dumps({
        "type": "response.create",
        "response": {
            "modalities": ["audio"],  
            # you can also do ["audio","text"] if you want a transcript too
        }
    }))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)