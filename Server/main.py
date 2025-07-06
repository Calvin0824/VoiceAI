import os
import asyncio
from typing import Optional, List

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, JSONResponse
import aiohttp
import orjson
from twilio.twiml.voice_response import VoiceResponse, Connect
from dotenv import load_dotenv

from system_prompt import SYSTEM_MESSAGE

load_dotenv()
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_KEY:
    raise RuntimeError("Missing OPENAI_API_KEY")

PORT = int(os.getenv("PORT", 5050))
VOICE = "alloy"
LOG_EVENT_TYPES = {
    "error",
    "response.content.done",
    "rate_limits.updated",
    "response.done",
    "input_audio_buffer.committed",
    "input_audio_buffer.speech_stopped",
    "input_audio_buffer.speech_started",
    "session.created",
    "response.cancelled",
    "response.created",
}

app = FastAPI()
log_clients: List[WebSocket] = []


def dumps_str(obj) -> str:
    return orjson.dumps(obj).decode("utf-8")


@app.get("/", response_class=JSONResponse)
async def healthcheck():
    return {"message": "Twilio Media Stream Server is running!"}


@app.api_route("/incoming-call", methods=["GET", "POST"])
async def twiml_endpoint(request: Request):
    resp = VoiceResponse()
    resp.say("This is Ming House.")
    conn = Connect()
    host = request.headers.get("host")
    conn.stream(url=f"wss://{host}/call")
    resp.append(conn)
    resp.say("Thank you much.")
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


async def twilio_to_openai(
    twilio_ws: WebSocket,
    openai_ws: aiohttp.ClientWebSocketResponse,
    stream_sid_holder: dict,
):
    try:
        async for raw in twilio_ws.iter_text():
            msg = orjson.loads(raw)
            ev = msg.get("event")
            if ev == "start":
                stream_sid_holder["sid"] = msg["start"]["streamSid"]
            elif ev == "media":
                payload = {
                    "type": "input_audio_buffer.append",
                    "audio": msg["media"]["payload"],
                }
                await openai_ws.send_str(dumps_str(payload))
    except WebSocketDisconnect:
        await openai_ws.close()


async def openai_to_twilio(
    openai_ws: aiohttp.ClientWebSocketResponse,
    twilio_ws: WebSocket,
    stream_sid_holder: dict,
):
    assistant_responding = False
    try:
        async for msg in openai_ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            data = orjson.loads(msg.data)
            ev = data.get("type")

            # broadcast logs
            if ev in LOG_EVENT_TYPES:
                for client in log_clients:
                    try:
                        await client.send_text(msg.data)
                    except:
                        pass

            # on user speech start: clear buffer & cancel AI output immediately
            if ev == "input_audio_buffer.speech_started":
                sid = stream_sid_holder.get("sid")
                if sid:
                    await twilio_ws.send_json({"streamSid": sid, "event": "clear"})
                cancel_payload = {"type": "response.cancel"}
                await openai_ws.send_str(dumps_str(cancel_payload))
                assistant_responding = False
                continue

            # on user speech stop: trigger AI response
            if ev == "input_audio_buffer.speech_stopped" and not assistant_responding:
                create_payload = {
                    "type": "response.create",
                    "response": {"modalities": ["text", "audio"]},
                }
                await openai_ws.send_str(dumps_str(create_payload))
                continue

            #  forward to Twilio
            if ev == "response.audio.delta" and stream_sid_holder.get("sid"):
                await twilio_ws.send_json(
                    {
                        "event": "media",
                        "streamSid": stream_sid_holder["sid"],
                        "media": {"payload": data["delta"]},
                    }
                )
                continue

            # track assistant response lifecycle
            if ev == "response.created":
                assistant_responding = True
            elif ev == "response.cancelled" or ev == "response.done":
                assistant_responding = False

    except Exception:
        pass


@app.websocket("/call")
async def call_ws(ws: WebSocket):
    await ws.accept()
    openai_url = (
        "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-12-17"
    )
    headers = {
        "Authorization": f"Bearer {OPENAI_KEY}",
        "openai-beta": "realtime=v1",
    }

    stream_sid_holder = {}

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(openai_url, headers=headers) as openai_ws:
            init_payload = {
                "type": "session.update",
                "session": {
                    "input_audio_format": "g711_ulaw",
                    "output_audio_format": "g711_ulaw",
                    "voice": VOICE,
                    "instructions": SYSTEM_MESSAGE,
                    "modalities": ["text", "audio"],
                    "temperature": 0.8,
                    "input_audio_transcription": {"model": "whisper-1"},
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.6,
                        "prefix_padding_ms": 300,
                        "silence_duration_ms": 800,
                        "create_response": True,
                    },
                },
            }
            await openai_ws.send_str(dumps_str(init_payload))

            # run both forwarding loops concurrently
            tw_to_ai = asyncio.create_task(
                twilio_to_openai(ws, openai_ws, stream_sid_holder)
            )
            ai_to_tw = asyncio.create_task(
                openai_to_twilio(openai_ws, ws, stream_sid_holder)
            )

            done, pending = await asyncio.wait(
                [tw_to_ai, ai_to_tw], return_when=asyncio.FIRST_EXCEPTION
            )
            for task in pending:
                task.cancel()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
