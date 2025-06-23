from fastapi import FastAPI, UploadFile, File, Form, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from utils.interview import handle_interview
from utils.code_explainer import explain_code
from utils.devops_assistant import review_devops
import os
import uuid
import logging

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://chatbot-frontend.khacthienit.click"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

# Xử lý lỗi chung để đảm bảo header CORS
@app.exception_handler(Exception)
async def custom_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled error: {str(exc)}")
    return JSONResponse(
        status_code=500,
        content={"detail": f"Internal Server Error: {str(exc)}"},
        headers={
            "Access-Control-Allow-Origin": "https://chatbot-frontend.khacthienit.click",
            "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
        },
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

    # Kiểm tra input
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
                    logger.debug(f"Removing temp audio file: {temp_audio_path}")
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
