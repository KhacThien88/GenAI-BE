from fastapi import FastAPI, UploadFile, File, Form, Request, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from utils.interview import handle_interview
from utils.code_explainer import explain_code
from utils.devops_assistant import review_devops
import requests
import os
import uuid
import logging
import json
import time
from dotenv import load_dotenv
import subprocess
from pydub import AudioSegment
import urllib.request
from typing import Set

# Thiết lập logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

app = FastAPI()

# Tải biến môi trường
load_dotenv()
PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")  # Cho Messenger
PAGE_ACCESS_TOKEN_WHATSAPP = os.getenv("PAGE_ACCESS_TOKEN_WHATSAPP")  # Cho WhatsApp
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")  # Cho ElevenLabs
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID")  # Cho ElevenLabs

# URL cho WhatsApp
WHATSAPP_API_URL = "https://graph.facebook.com/v20.0/{phone_number_id}/messages"
MESSENGER_API_URL = "https://graph.facebook.com/v20.0/me/messages"

# Tập hợp để lưu trữ message_id đã xử lý (tạm thời, nên dùng Redis cho môi trường production)
processed_messages: Set[str] = set()

# Middleware CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://chatbot-frontend.khacthienit.click"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

# Xử lý lỗi chung
@app.exception_handler(Exception)
async def custom_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled error: {str(exc)}")
    return JSONResponse(
        status_code=500,
        content={"detail": f"Internal Server Error: {str(exc)}"},
        headers={
            "Access-Control-Allow-Origin": "https://chatbot-frontend.khacthienit.click",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization"
        }
    )

@app.post("/interview")
async def interview_bot(
    audio: UploadFile = File(None),
    text: str = Form(None),
    question: str = Form(None),
    request: Request = None
):
    logger.debug(f"Raw headers: {dict(request.headers)}")
    form_data = await request.form()
    logger.debug(f"Raw form data: {form_data._dict}")
    logger.debug(f"Parsed inputs: audio={audio}, text={text}, question={question}")

    if not audio and not text and not question:
        logger.debug("No audio, text, or question provided")
        raise HTTPException(status_code=422, detail="Either audio file, text, or question input is required")
    if sum(1 for x in [audio, text, question] if x is not None) > 1:
        logger.debug("Multiple inputs provided")
        raise HTTPException(status_code=422, detail="Provide only one of audio file, text, or question")

    try:
        if audio:
            logger.debug(f"Received file: {audio.filename}, Content-Type: {audio.content_type}")
            if not audio.filename.endswith((".wav", ".mp3")):
                logger.debug("Invalid file format")
                raise HTTPException(status_code=422, detail="Only WAV or MP3 files are supported")
            if audio.size > 10 * 1024 * 1024:
                logger.debug("File size too large")
                raise HTTPException(status_code=422, detail="File size exceeds 10 MB")

            temp_audio_path = f"/tmp/{uuid.uuid4()}.{audio.filename.split('.')[-1]}"
            try:
                with open(temp_audio_path, "wb") as f:
                    f.write(await audio.read())
                response = handle_interview(audio_path=temp_audio_path)
                return {"response": response}
            finally:
                if os.path.exists(temp_audio_path):
                    os.remove(temp_audio_path)
        else:
            input_text = text or question
            logger.debug(f"Processing text input: {input_text}")
            response = handle_interview(text_input=input_text)
            return {"response": response}
    except Exception as e:
        logger.error(f"Error processing request: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Processing error: {str(e)}")

@app.post("/explain-code")
def code_explainer(code: str = Form(...)):
    return {"response": explain_code(code)}

@app.post("/devops-assistant")
def devops_helper(content: str = Form(...)):
    return {"response": review_devops(content)}

@app.get("/webhook")
async def verify_webhook(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        logger.info("Webhook verified successfully")
        return JSONResponse(content=int(challenge) if challenge.isdigit() else challenge)
    else:
        logger.error("Webhook verification failed")
        raise HTTPException(status_code=403, detail="Invalid token")

@app.post("/webhook")
async def handle_webhook(request: Request):
    try:
        data = await request.json()
        logger.debug(f"Received webhook data: {json.dumps(data, indent=2)}")

        # Xử lý WhatsApp Business Account
        if data.get("object") == "whatsapp_business_account":
            for entry in data.get("entry", []):
                for change in entry.get("changes", []):
                    value = change.get("value")
                    if value and value.get("messages"):
                        for message in value.get("messages", []):
                            message_id = message.get("id")
                            if not message_id:
                                logger.warning("No message_id found, skipping")
                                continue
                            if message_id in processed_messages:
                                logger.info(f"Message {message_id} already processed, skipping")
                                continue
                            processed_messages.add(message_id)
                            logger.debug(f"Processing new message: {message_id}")

                            sender_id = message.get("from")
                            phone_number_id = value.get("metadata", {}).get("phone_number_id")
                            if not sender_id or not phone_number_id:
                                logger.warning("No sender_id or phone_number_id found")
                                continue

                            # Xử lý tin nhắn văn bản
                            if message.get("type") == "text":
                                text = message.get("text", {}).get("body")
                                logger.info(f"Received WhatsApp text message from {sender_id}: {text}")
                                try:
                                    response = handle_interview(text_input=text)
                                    if PAGE_ACCESS_TOKEN_WHATSAPP:
                                        send_whatsapp_message(phone_number_id, sender_id, response["text"])
                                    if response.get("audio_url") and PAGE_ACCESS_TOKEN_WHATSAPP:
                                        if validate_audio_url(response["audio_url"]):
                                            if not send_whatsapp_audio(phone_number_id, sender_id, response["audio_url"]):
                                                send_whatsapp_message(phone_number_id, sender_id, "Sorry, I couldn't send the audio response.")
                                        else:
                                            logger.error(f"Skipping audio send due to invalid URL: {response['audio_url']}")
                                            send_whatsapp_message(phone_number_id, sender_id, "Sorry, I couldn't send the audio response.")
                                except Exception as e:
                                    logger.error(f"Error processing WhatsApp text: {str(e)}")
                                    if PAGE_ACCESS_TOKEN_WHATSAPP:
                                        send_whatsapp_message(phone_number_id, sender_id, "Sorry, I couldn't process your request.")

                            # Xử lý tin nhắn âm thanh
                            elif message.get("type") == "audio":
                                audio_id = message.get("audio", {}).get("id")
                                logger.info(f"Received WhatsApp audio message from {sender_id}: {audio_id}")
                                try:
                                    if audio_id and PAGE_ACCESS_TOKEN_WHATSAPP:
                                        audio_content = download_whatsapp_audio(audio_id)
                                        if audio_content:
                                            # Lưu file .ogg
                                            temp_audio_path = f"/tmp/{uuid.uuid4()}.ogg"
                                            with open(temp_audio_path, "wb") as f:
                                                f.write(audio_content)
                                            logger.debug(f"Downloaded audio file size: {os.path.getsize(temp_audio_path)} bytes")

                                            # Kiểm tra kích thước file
                                            if os.path.getsize(temp_audio_path) < 1024:
                                                logger.warning("Downloaded file is too small, likely metadata")
                                                raise Exception("Invalid audio file")

                                            # Chuyển đổi .ogg sang .wav
                                            temp_wav_path = f"/tmp/{uuid.uuid4()}.wav"
                                            try:
                                                audio = AudioSegment.from_ogg(temp_audio_path)
                                                audio = audio.set_channels(1).set_frame_rate(16000)
                                                audio.export(temp_wav_path, format="wav")
                                                logger.debug(f"Converted WAV file size: {os.path.getsize(temp_wav_path)} bytes")
                                            except Exception as e:
                                                logger.error(f"pydub conversion failed: {e}")
                                                try:
                                                    subprocess.run(
                                                        ["ffmpeg", "-i", temp_audio_path, "-acodec", "pcm_s16le", "-ac", "1", "-ar", "16000", temp_wav_path, "-y"],
                                                        check=True
                                                    )
                                                    logger.debug(f"Converted WAV file size (ffmpeg): {os.path.getsize(temp_wav_path)} bytes")
                                                except FileNotFoundError:
                                                    logger.error("ffmpeg not found. Please install ffmpeg (sudo apt-get install ffmpeg)")
                                                    raise
                                                except subprocess.CalledProcessError as e:
                                                    logger.error(f"ffmpeg conversion failed: {e}")
                                                    raise

                                            # Xử lý file âm thanh
                                            response = handle_interview(audio_path=temp_wav_path)
                                            if PAGE_ACCESS_TOKEN_WHATSAPP:
                                                send_whatsapp_message(phone_number_id, sender_id, response["text"])
                                            if response.get("audio_url") and PAGE_ACCESS_TOKEN_WHATSAPP:
                                                if validate_audio_url(response["audio_url"]):
                                                    if not send_whatsapp_audio(phone_number_id, sender_id, response["audio_url"]):
                                                        send_whatsapp_message(phone_number_id, sender_id, "Sorry, I couldn't send the audio response.")
                                                else:
                                                    logger.error(f"Skipping audio send due to invalid URL: {response['audio_url']}")
                                                    send_whatsapp_message(phone_number_id, sender_id, "Sorry, I couldn't send the audio response.")
                                        else:
                                            logger.warning("Failed to download audio content")
                                            raise Exception("Failed to download audio content")
                                    else:
                                        logger.warning("No audio ID or token available")
                                        raise Exception("No audio ID or token available")
                                except Exception as e:
                                    logger.error(f"Error processing WhatsApp audio: {str(e)}")
                                    if PAGE_ACCESS_TOKEN_WHATSAPP:
                                        send_whatsapp_message(phone_number_id, sender_id, "Sorry, I couldn't process your audio.")
                                finally:
                                    if 'temp_audio_path' in locals() and os.path.exists(temp_audio_path):
                                        os.remove(temp_audio_path)
                                    if 'temp_wav_path' in locals() and os.path.exists(temp_wav_path):
                                        os.remove(temp_wav_path)

        # Xử lý Messenger
        elif data.get("object") == "page":
            for entry in data.get("entry", []):
                messaging = entry.get("messaging", [])
                for event in messaging:
                    message_id = event.get("message", {}).get("mid")
                    if not message_id:
                        logger.warning("No message_id found for Messenger event, skipping")
                        continue
                    if message_id in processed_messages:
                        logger.info(f"Message {message_id} already processed, skipping")
                        continue
                    processed_messages.add(message_id)
                    logger.debug(f"Processing new Messenger message: {message_id}")

                    sender_id = event.get("sender", {}).get("id")
                    if not sender_id:
                        logger.warning("No sender_id found")
                        continue

                    if event.get("message", {}).get("text"):
                        text = event["message"]["text"]
                        logger.info(f"Received Messenger text message from {sender_id}: {text}")
                        try:
                            response = handle_interview(text_input=text)
                            send_text_message(sender_id, response["text"])
                            if response.get("audio_url"):
                                send_audio_message(sender_id, response["audio_url"])
                        except Exception as e:
                            logger.error(f"Error processing Messenger text: {str(e)}")
                            send_text_message(sender_id, "Sorry, I couldn't process your request.")

                    elif event.get("message", {}).get("attachments"):
                        for attachment in event["message"]["attachments"]:
                            if attachment.get("type") == "audio":
                                audio_url = attachment["payload"]["url"]
                                logger.info(f"Received Messenger audio message from {sender_id}: {audio_url}")
                                try:
                                    audio_path = f"/tmp/{uuid.uuid4()}.mp3"
                                    urllib.request.urlretrieve(audio_url, audio_path)
                                    response = handle_interview(audio_path=audio_path)
                                    send_text_message(sender_id, response["text"])
                                    if response.get("audio_url"):
                                        send_audio_message(sender_id, response["audio_url"])
                                except Exception as e:
                                    logger.error(f"Error processing Messenger audio: {str(e)}")
                                    send_text_message(sender_id, "Sorry, I couldn't process your audio.")
                                finally:
                                    if os.path.exists(audio_path):
                                        os.remove(audio_path)

        return JSONResponse(content={"status": "success"}, status_code=200)
    except Exception as e:
        logger.error(f"Webhook error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

# Hàm kiểm tra URL âm thanh
def validate_audio_url(audio_url, retries=3, delay=2):
    valid_types = [
        "audio/mpeg",
        "audio/ogg",
        "audio/ogg; codecs=opus",
        "audio/amr",
        "audio/mp4",
        "audio/aac"
    ]
    for attempt in range(retries):
        try:
            response = requests.head(audio_url, timeout=5)
            logger.debug(f"HEAD response headers: {response.headers}")
            if response.status_code == 200:
                content_type = response.headers.get("Content-Type", "").lower()
                if any(valid_type in content_type for valid_type in valid_types):
                    logger.debug(f"Valid audio URL: {audio_url}, Content-Type: {content_type}")
                    return True
                logger.error(f"Invalid Content-Type: {content_type} for URL: {audio_url}")
                return False
            logger.warning(f"Attempt {attempt + 1}: HEAD request failed with status {response.status_code}")
        except requests.RequestException as e:
            logger.warning(f"Attempt {attempt + 1}: HEAD request error: {str(e)}")
        time.sleep(delay)
    logger.error(f"Invalid audio URL after {retries} attempts: {audio_url}")
    return False

# Gửi tin nhắn WhatsApp
def send_whatsapp_message(phone_number_id, recipient_id, text):
    if not PAGE_ACCESS_TOKEN_WHATSAPP:
        logger.error("PAGE_ACCESS_TOKEN_WHATSAPP is not configured")
        return False
    url = WHATSAPP_API_URL.format(phone_number_id=phone_number_id)
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient_id,
        "type": "text",
        "text": {"body": text[:4096]}
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {PAGE_ACCESS_TOKEN_WHATSAPP}"
    }
    try:
        logger.debug(f"Sending WhatsApp message with payload: {json.dumps(payload, indent=2)}")
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        logger.debug(f"WhatsApp API response: {response.status_code} {response.text}")
        if response.status_code != 200:
            logger.error(f"Failed to send WhatsApp message: {response.text} | Payload: {json.dumps(payload)}")
            return False
        logger.info(f"WhatsApp message sent to {recipient_id}")
        return True
    except requests.RequestException as e:
        logger.error(f"Error sending WhatsApp message: {str(e)}")
        return False

# Gửi tin nhắn âm thanh WhatsApp
def send_whatsapp_audio(phone_number_id, recipient_id, audio_url):
    if not PAGE_ACCESS_TOKEN_WHATSAPP:
        logger.error("PAGE_ACCESS_TOKEN_WHATSAPP is not configured")
        return False
    url = WHATSAPP_API_URL.format(phone_number_id=phone_number_id)
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient_id,
        "type": "audio",
        "audio": {"link": audio_url}
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {PAGE_ACCESS_TOKEN_WHATSAPP}"
    }
    try:
        logger.debug(f"Sending WhatsApp audio with payload: {json.dumps(payload, indent=2)}")
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        logger.debug(f"WhatsApp API response: {response.status_code} {response.text}")
        if response.status_code != 200:
            logger.error(f"Failed to send WhatsApp audio: {response.text} | Payload: {json.dumps(payload)}")
            return False
        logger.info(f"WhatsApp audio sent to {recipient_id}")
        return True
    except requests.RequestException as e:
        logger.error(f"Error sending WhatsApp audio: {str(e)}")
        return False

# Gửi tin nhắn Messenger
def send_text_message(recipient_id, text):
    if not PAGE_ACCESS_TOKEN:
        logger.error("PAGE_ACCESS_TOKEN is not configured")
        return False
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": text[:640]}
    }
    headers = {"Content-Type": "application/json"}
    params = {"access_token": PAGE_ACCESS_TOKEN}
    try:
        response = requests.post(MESSENGER_API_URL, json=payload, headers=headers, params=params, timeout=10)
        if response.status_code != 200:
            logger.error(f"Failed to send text message: {response.text}")
            return False
        logger.info(f"Text message sent to {recipient_id}")
        return True
    except requests.RequestException as e:
        logger.error(f"Error sending Messenger text: {str(e)}")
        return False

def send_audio_message(recipient_id, audio_url):
    if not PAGE_ACCESS_TOKEN:
        logger.error("PAGE_ACCESS_TOKEN is not configured")
        return False
    payload = {
        "recipient": {"id": recipient_id},
        "message": {
            "attachment": {
                "type": "audio",
                "payload": {"url": audio_url}
            }
        }
    }
    headers = {"Content-Type": "application/json"}
    params = {"access_token": PAGE_ACCESS_TOKEN}
    try:
        response = requests.post(MESSENGER_API_URL, json=payload, headers=headers, params=params, timeout=10)
        if response.status_code != 200:
            logger.error(f"Failed to send audio message: {response.text}")
            return False
        logger.info(f"Audio message sent to {recipient_id}")
        return True
    except requests.RequestException as e:
        logger.error(f"Error sending Messenger audio: {str(e)}")
        return False

# Hàm tải nội dung audio từ WhatsApp
def download_whatsapp_audio(audio_id):
    if not PAGE_ACCESS_TOKEN_WHATSAPP:
        logger.error("PAGE_ACCESS_TOKEN_WHATSAPP is not configured")
        return None
    media_url = f"https://graph.facebook.com/v20.0/{audio_id}"
    headers = {"Authorization": f"Bearer {PAGE_ACCESS_TOKEN_WHATSAPP}"}
    try:
        response = requests.get(media_url, headers=headers, timeout=10)
        if response.status_code == 200:
            metadata = response.json()
            logger.debug(f"Media metadata: {json.dumps(metadata, indent=2)}")
            download_url = metadata.get("url")
            if download_url:
                download_response = requests.get(download_url, headers=headers, stream=True, timeout=10)
                if download_response.status_code == 200:
                    content_type = download_response.headers.get("Content-Type", "").lower()
                    if "audio" not in content_type:
                        logger.error(f"Invalid content type: {content_type}")
                        return None
                    content = download_response.content
                    logger.debug(f"Media download content length: {len(content)} bytes")
                    return content
                else:
                    logger.error(f"Failed to download media content: {download_response.status_code} {download_response.text}")
                    return None
            else:
                logger.error("No download URL found in metadata")
                return None
        else:
            logger.error(f"Failed to get media info: {response.status_code} {response.text}")
            return None
    except (requests.RequestException, json.JSONDecodeError) as e:
        logger.error(f"Error downloading WhatsApp audio: {str(e)}")
        return None
