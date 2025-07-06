import os
import json
import asyncio
from typing import List
import aiohttp
from websockets.client import connect  # pip install websockets
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.websockets import WebSocketDisconnect
from twilio.twiml.voice_response import VoiceResponse, Connect
from dotenv import load_dotenv
from system_prompt import SYSTEM_MESSAGE

load_dotenv()

# --- Configuration ---
openai_key = os.getenv("OPENAI_API_KEY")
if not openai_key:
    raise ValueError("OPENAI_API_KEY is missing")
PORT = int(os.getenv("PORT", 5050))
VOICE = "alloy"
LOG_EVENT_TYPES = [
    'error', 'response.content.done', 'rate_limits.updated',
    'response.done', 'input_audio_buffer.committed',
    'input_audio_buffer.speech_stopped', 'input_audio_buffer.speech_started',
    'session.created'
]
SHOW_TIMING_MATH = False

# --- FastAPI app and state ---
app = FastAPI()
log_clients: List[WebSocket] = []

@app.get("/", response_class=JSONResponse)
async def healthcheck():
    return {"message": "Twilio Media Stream Server is running!"}

@app.api_route("/incoming-call", methods=["GET", "POST"])
async def twiml_endpoint(request: Request):
    resp = VoiceResponse()
    host = request.url.hostname
    resp.say("Hello, this is Ming House. How may I help you?")
    conn = Connect()
    conn.stream(url=f"wss://{host}/call")
    resp.append(conn)
    resp.say("Thank you so much!")
    return HTMLResponse(content=str(resp), media_type="application/xml")

@app.websocket("/logs")
async def logs_ws(ws: WebSocket):
    await ws.accept()
    log_clients.append(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        log_clients.remove(ws)

@app.websocket("/call")
async def call_ws(twilio_ws: WebSocket):
    await twilio_ws.accept()
    openai_url = 'wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-12-17'
    headers = [("Authorization", f"Bearer {openai_key}"), ("openai-beta", "realtime=v1")]

    async with connect(openai_url, extra_headers=headers) as openai_ws:
        # capture the Twilio streamSid so we can send back TTS correctly
        stream_sid = None

        # Initialize session
        await openai_ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "turn_detection": {"type": "server_vad"},
                "input_audio_format": "g711_ulaw",
                "output_audio_format": "g711_ulaw",
                "voice": VOICE,
                "instructions": SYSTEM_MESSAGE,
                "modalities": ["text", "audio"],
                "temperature": 0.8
            }
        }))
        # ─────────── removed: initial response.create ─────────── #

        async def forward_twilio():
            nonlocal stream_sid
            try:
                async for msg in twilio_ws.iter_text():
                    data = json.loads(msg)
                    ev = data.get("event")
                    if ev == "start":
                        # store Twilio's streamSid
                        stream_sid = data["start"]["streamSid"]
                    elif ev == "media":
                        await openai_ws.send(json.dumps({
                            "type": "input_audio_buffer.append",
                            "audio": data["media"]["payload"]
                        }))
            except WebSocketDisconnect:
                await openai_ws.close()

        async def forward_openai():
            try:
                async for msg in openai_ws:
                    data = json.loads(msg)
                    ev_type = data.get("type")

                    # Broadcast logs
                    if ev_type in LOG_EVENT_TYPES:
                        for client in log_clients:
                            await client.send_text(json.dumps(data))

                    # Stream TTS deltas back *with* the correct streamSid
                    if ev_type == 'response.audio.delta' and stream_sid is not None:
                        await twilio_ws.send_json({
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"payload": data['delta']}
                        })

                    # **After the user stops speaking**, ask the model to respond
                    elif ev_type == 'input_audio_buffer.speech_stopped':
                        await openai_ws.send(json.dumps({
                            "type": "response.create",
                            "response": {"modalities": ["text", "audio"]}
                        }))

            except Exception:
                pass

        await asyncio.gather(forward_twilio(), forward_openai())

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
