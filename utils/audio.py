import boto3
import json
import uuid
import os
import logging

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

polly = boto3.client("polly", region_name="ap-southeast-2")
s3 = boto3.client("s3", region_name="ap-southeast-2")

def generate_audio_notification(text: str) -> str:
    """
    Generate an audio notification from text using AWS Polly and upload to S3.
    Returns the S3 URL of the audio file.
    """
    bucket_name = "chatbotbucket-vkt"
    output_audio = None
    output_audio_key = None
    try:
        if not text.strip():
            raise ValueError("Text input cannot be empty")

        logger.debug(f"Generating audio notification for text: {text}")
        polly_response = polly.synthesize_speech(
            Text=text,
            OutputFormat="mp3",
            VoiceId="Joanna",
            Engine="neural"
        )

        # Lưu file MP3 tạm thời
        output_audio = f"/tmp/{uuid.uuid4()}.mp3"
        with open(output_audio, "wb") as f:
            f.write(polly_response["AudioStream"].read())

        # Upload file MP3 lên S3
        output_audio_key = f"audio/notifications/{uuid.uuid4()}.mp3"
        logger.debug(f"Uploading notification MP3 to S3: {bucket_name}/{output_audio_key}")
        s3.upload_file(output_audio, bucket_name, output_audio_key)
        audio_url = f"https://{bucket_name}.s3.ap-southeast-2.amazonaws.com/{output_audio_key}"

        return audio_url

    except Exception as e:
        logger.error(f"Error generating audio notification: {str(e)}")
        raise Exception(f"Error generating audio notification: {str(e)}")
    finally:
        # Dọn dẹp file tạm
        if output_audio and os.path.exists(output_audio):
            logger.debug(f"Removing local audio file: {output_audio}")
            os.remove(output_audio)
        # Note: Không xóa file S3 ngay để frontend có thời gian phát, có thể thêm TTL hoặc cleanup job sau
