import os
import json
import asyncio
from typing import List, Dict, Any
import aiohttp
from websockets.client import connect  
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
    'session.created', 'response.audio.delta', 'response.text.delta'
]

# --- FastAPI app and state ---
app = FastAPI()
log_clients: List[WebSocket] = []

# Session data structure following Twilio's approach
class SessionData:
    def __init__(self):
        self.conversation: List[Dict[str, Any]] = []
        self.current_response_tokens: List[str] = []
        self.tokens_spoken: int = 0
        self.is_ai_speaking: bool = False
        self.current_response_id: str = None
        self.response_in_progress: bool = False
        self.speech_detected: bool = False
        self.interrupt_pending: bool = False

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

def handle_interrupt(session_data: SessionData):
    """Handle interruption following Twilio's approach"""
    
    if session_data.current_response_tokens and session_data.tokens_spoken > 0:
        # Get the tokens that were actually spoken before interruption
        spoken_tokens = session_data.current_response_tokens[:session_data.tokens_spoken]
        partial_response = " ".join(spoken_tokens)
        
        # Update conversation with only the spoken portion
        if session_data.conversation:
            last_message = session_data.conversation[-1]
            if last_message.get("role") == "assistant":
                last_message["content"] = partial_response

            else:
                # Add the partial response as a new assistant message
                session_data.conversation.append({
                    "role": "assistant",
                    "content": partial_response
                })

    # Reset response state
    session_data.current_response_tokens = []
    session_data.tokens_spoken = 0
    session_data.is_ai_speaking = False
    session_data.current_response_id = None
    session_data.response_in_progress = False
    session_data.interrupt_pending = False

@app.websocket("/call")
async def call_ws(twilio_ws: WebSocket):
    await twilio_ws.accept()
    openai_url = 'wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-12-17'
    headers = [("Authorization", f"Bearer {openai_key}"), ("openai-beta", "realtime=v1")]

    async with connect(openai_url, extra_headers=headers) as openai_ws:
        # Initialize session data following Twilio's approach
        session_data = SessionData()
        stream_sid = None
        
        # Track audio timing for interruption detection
        last_audio_time = 0
        audio_buffer_count = 0
        
        # Initialize session
        await openai_ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.6,  # Balanced sensitivity
                    "prefix_padding_ms": 300,
                    "silence_duration_ms": 700
                },
                "input_audio_format": "g711_ulaw",
                "output_audio_format": "g711_ulaw",
                "voice": VOICE,
                "instructions": SYSTEM_MESSAGE,
                "modalities": ["text", "audio"],
                "temperature": 0.8
            }
        }))

        async def forward_twilio():
            nonlocal stream_sid, last_audio_time, audio_buffer_count
            try:
                async for msg in twilio_ws.iter_text():
                    data = json.loads(msg)
                    ev = data.get("event")
                    
                    if ev == "start":
                        stream_sid = data["start"]["streamSid"]
                        
                    elif ev == "media":
                        current_time = asyncio.get_event_loop().time()
                        last_audio_time = current_time
                        audio_buffer_count += 1
                        
                        # Twilio-style interruption detection
                        if (session_data.is_ai_speaking and 
                            session_data.speech_detected and 
                            audio_buffer_count > 3):  # Small buffer to avoid false positives
                            
                            if not session_data.interrupt_pending:
                                session_data.interrupt_pending = True
                                
                                # Cancel current response
                                await openai_ws.send(json.dumps({
                                    "type": "response.cancel"
                                }))
                                
                                # Handle the interruption with conversation tracking
                                handle_interrupt(session_data)
                                
                                # Clear input buffer
                                await openai_ws.send(json.dumps({
                                    "type": "input_audio_buffer.clear"
                                }))
                                
                                # Broadcast interruption
                                for client in log_clients:
                                    await client.send_text(json.dumps({
                                        "type": "interruption",
                                        "tokens_spoken": session_data.tokens_spoken,
                                        "timestamp": current_time
                                    }))
                        
                        # Always append new audio
                        await openai_ws.send(json.dumps({
                            "type": "input_audio_buffer.append",
                            "audio": data["media"]["payload"]
                        }))
                        
            except WebSocketDisconnect:
                await openai_ws.close()

        async def forward_openai():
            nonlocal audio_buffer_count
            try:
                async for msg in openai_ws:
                    data = json.loads(msg)
                    ev_type = data.get("type")

                    # Broadcast logs
                    if ev_type in LOG_EVENT_TYPES:
                        for client in log_clients:
                            await client.send_text(json.dumps(data))

                    # TOKEN STREAMING - Following Twilio's approach
                    if ev_type == 'response.text.delta':
                        delta_text = data.get('delta', '')
                        response_id = data.get('response_id')
                        
                        # Accumulate tokens for conversation tracking
                        if delta_text.strip():  # Only add non-empty tokens
                            session_data.current_response_tokens.append(delta_text)
                        
                        # Track when AI starts speaking for first token
                        if not session_data.is_ai_speaking and delta_text.strip():
                            session_data.is_ai_speaking = True
                            session_data.current_response_id = response_id
                            session_data.tokens_spoken = 0
                        
                        # Broadcast token streaming
                        for client in log_clients:
                            await client.send_text(json.dumps({
                                "type": "token_stream",
                                "delta": delta_text,
                                "token_count": len(session_data.current_response_tokens),
                                "response_id": response_id
                            }))

                    # AUDIO STREAMING - Track spoken tokens
                    elif ev_type == 'response.audio.delta' and stream_sid is not None:
                        # Increment spoken tokens counter (approximate)
                        # This is a simplified approach - in production you'd want more precise timing
                        if session_data.current_response_tokens:
                            session_data.tokens_spoken = min(
                                session_data.tokens_spoken + 1,
                                len(session_data.current_response_tokens)
                            )
                        
                        await twilio_ws.send_json({
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"payload": data['delta']}
                        })

                    # RESPONSE COMPLETION - Following Twilio's approach
                    elif ev_type == 'response.audio.done':
                        session_data.is_ai_speaking = False
                        
                    elif ev_type == 'response.done':
                        response_id = data.get('response_id')
                        
                        # Add complete response to conversation if not interrupted
                        if not session_data.interrupt_pending and session_data.current_response_tokens:
                            full_response = " ".join(session_data.current_response_tokens)
                            session_data.conversation.append({
                                "role": "assistant",
                                "content": full_response
                            })
                        
                        # Reset response state
                        session_data.current_response_tokens = []
                        session_data.tokens_spoken = 0
                        session_data.is_ai_speaking = False
                        session_data.current_response_id = None
                        session_data.response_in_progress = False
                        session_data.interrupt_pending = False

                    # SPEECH DETECTION - Following Twilio's approach
                    elif ev_type == 'input_audio_buffer.speech_started':
                        session_data.speech_detected = True
                        audio_buffer_count = 0  # Reset buffer count
                        
                        for client in log_clients:
                            await client.send_text(json.dumps({
                                "type": "user_speech_started",
                                "timestamp": asyncio.get_event_loop().time()
                            }))
                    
                    elif ev_type == 'input_audio_buffer.speech_stopped':
                        session_data.speech_detected = False
                        audio_buffer_count = 0
                        
                        # Only trigger response if AI is not speaking
                        if not session_data.is_ai_speaking and not session_data.response_in_progress:
                            print("🚀 Triggering AI response")
                            session_data.response_in_progress = True
                            session_data.current_response_tokens = []
                            session_data.tokens_spoken = 0
                            
                            # Create response with conversation context
                            await openai_ws.send(json.dumps({
                                "type": "response.create",
                                "response": {"modalities": ["text", "audio"]}
                            }))
                            
                            for client in log_clients:
                                await client.send_text(json.dumps({
                                    "type": "response_triggered",
                                    "conversation_length": len(session_data.conversation),
                                    "timestamp": asyncio.get_event_loop().time()
                                }))
        
                    # ENHANCED ERROR HANDLING
                    elif ev_type == 'error':
                        error_code = data.get('error', {}).get('code', '')
                        
                        if error_code != 'conversation_already_has_active_response':
                            session_data.is_ai_speaking = False
                            session_data.current_response_id = None
                            session_data.response_in_progress = False
                            session_data.current_response_tokens = []
                            session_data.tokens_spoken = 0
                            session_data.interrupt_pending = False
                        
                        for client in log_clients:
                            await client.send_text(json.dumps({
                                "type": "error_handled",
                                "error": data,
                                "timestamp": asyncio.get_event_loop().time()
                            }))

                    # SESSION MANAGEMENT
                    elif ev_type == 'session.created':
                        for client in log_clients:
                            await client.send_text(json.dumps({
                                "type": "session_initialized",
                                "timestamp": asyncio.get_event_loop().time()
                            }))

                    # CONVERSATION COMMITTED - Add user message to conversation
                    elif ev_type == 'input_audio_buffer.committed':
                        # This happens when user speech is processed
                        # In a full implementation, you'd extract the user's text here
                        # For now, we'll add a placeholder
                        print("📝 User input committed to conversation")

            except Exception as e:
                # Reset state on exception
                session_data.is_ai_speaking = False
                session_data.current_response_id = None
                session_data.response_in_progress = False
                session_data.current_response_tokens = []
                session_data.tokens_spoken = 0
                session_data.speech_detected = False
                session_data.interrupt_pending = False

        await asyncio.gather(forward_twilio(), forward_openai())

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)