import boto3
import json
import uuid
import os
import time
import logging
import urllib.parse
import re

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

bedrock = boto3.client("bedrock-runtime", region_name="ap-southeast-2")
transcribe = boto3.client("transcribe", region_name="ap-southeast-2")
s3 = boto3.client("s3", region_name="ap-southeast-2")

def handle_interview(audio_path: str = None, text_input: str = None) -> dict:
    bucket_name = "chatbotbucket-vkt"
    audio_key = None
    transcript_file = None
    output_audio = None
    transcript_key = None
    try:
        # Kiểm tra input
        if audio_path and text_input:
            raise ValueError("Provide either audio_path or text_input, not both")
        if not audio_path and not text_input:
            raise ValueError("Either audio_path or text_input is required")

        # Step 1: Xử lý câu hỏi (từ audio hoặc text)
        if audio_path:
            # Validate audio file
            file_size = os.path.getsize(audio_path)
            logger.debug(f"Audio file size: {file_size} bytes")
            if file_size < 1000:
                raise ValueError("Audio file is too small or invalid")
            supported_formats = ["wav", "mp3"]
            if audio_path.split(".")[-1].lower() not in supported_formats:
                raise ValueError(f"Unsupported audio format. Supported: {supported_formats}")

            # Upload audio lên S3
            audio_key = f"audio/{uuid.uuid4()}.{audio_path.split('.')[-1]}"
            logger.debug(f"Uploading audio to S3: {bucket_name}/{audio_key}")
            s3.upload_file(Filename=audio_path, Bucket=bucket_name, Key=audio_key)
            media_uri = f"s3://{bucket_name}/{audio_key}"

            # Bắt đầu job AWS Transcribe
            job_name = f"transcribe_{uuid.uuid4()}"
            logger.debug(f"Starting Transcribe job: {job_name}")
            transcribe.start_transcription_job(
                TranscriptionJobName=job_name,
                Media={"MediaFileUri": media_uri},
                MediaFormat=audio_path.split(".")[-1],
                LanguageCode="en-US",
                OutputBucketName=bucket_name,
                Settings={"ShowAlternatives": False}
            )

            # Chờ job hoàn tất
            max_attempts = 60
            attempt = 0
            while attempt < max_attempts:
                status = transcribe.get_transcription_job(TranscriptionJobName=job_name)
                job_status = status["TranscriptionJob"]["TranscriptionJobStatus"]
                logger.debug(f"Transcription job status: {job_status}, Full response: {status}")
                if job_status in ["COMPLETED", "FAILED"]:
                    break
                time.sleep(3)
                attempt += 1
            if job_status == "FAILED":
                raise Exception(f"Transcription job failed: {status.get('TranscriptionJob', {}).get('FailureReason', 'Unknown')}")
            if job_status == "COMPLETED":
                time.sleep(5)  # Wait for S3 consistency

            # Lấy đường dẫn transcript từ TranscriptFileUri
            transcript_uri = status["TranscriptionJob"].get("Transcript", {}).get("TranscriptFileUri")
            if not transcript_uri:
                raise Exception("TranscriptFileUri not found in Transcribe response")
            logger.debug(f"Transcript URI: {transcript_uri}")

            # Trích xuất bucket và key từ URI (hỗ trợ cả s3:// và https://)
            parsed_uri = urllib.parse.urlparse(transcript_uri)
            if parsed_uri.scheme == "s3":
                transcript_bucket = parsed_uri.netloc
                transcript_key = parsed_uri.path.lstrip("/")
            elif parsed_uri.scheme == "https" and "amazonaws.com" in parsed_uri.netloc:
                # Handle HTTPS URL: https://s3.<region>.amazonaws.com/<bucket>/<key>
                match = re.match(r"s3\.([a-z0-9-]+)\.amazonaws\.com", parsed_uri.netloc)
                if not match:
                    raise Exception(f"Invalid TranscriptFileUri netloc: {parsed_uri.netloc}")
                transcript_bucket = parsed_uri.path.split("/")[1]
                transcript_key = "/".join(parsed_uri.path.split("/")[2:])
            else:
                raise Exception(f"Invalid TranscriptFileUri scheme or netloc: {transcript_uri}")

            # Validate bucket
            if transcript_bucket != bucket_name:
                raise Exception(f"Transcript bucket mismatch: expected {bucket_name}, got {transcript_bucket}")
            logger.debug(f"Extracted transcript bucket: {transcript_bucket}, key: {transcript_key}")

            # Kiểm tra sự tồn tại của file transcript
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    s3.head_object(Bucket=bucket_name, Key=transcript_key)
                    break
                except s3.exceptions.ClientError as e:
                    if e.response["Error"]["Code"] == "404":
                        if attempt == max_retries - 1:
                            logger.error(f"Transcript file not found after {max_retries} attempts: {bucket_name}/{transcript_key}")
                            raise Exception("Transcript file not found on S3")
                        logger.debug(f"Attempt {attempt + 1}: Transcript not found, retrying...")
                        time.sleep(2)
                    else:
                        raise e

            # Lấy transcript từ S3
            transcript_file = f"/tmp/{uuid.uuid4()}.json"
            logger.debug(f"Downloading transcript: {bucket_name}/{transcript_key}")
            s3.download_file(bucket_name, transcript_key, transcript_file)
            with open(transcript_file, "r") as f:
                transcript_data = json.load(f)
                if not transcript_data["results"]["transcripts"]:
                    raise Exception("No transcript generated")
                question = transcript_data["results"]["transcripts"][0]["transcript"]
            logger.debug(f"Transcribed question: {question}")
        else:
            question = text_input
            logger.debug(f"Text input: {question}")

        # Step 2: Gửi câu hỏi đến Bedrock (Claude 3 Sonnet)
        prompt = f"You are a DevOps technical interview bot. You are also a DevOps expert with DevOps Interview Knowledge. {question}"
        logger.debug(f"Invoking Bedrock with prompt: {prompt}")
        response = bedrock.invoke_model(
            modelId="anthropic.claude-3-sonnet-20240229-v1:0",
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 256,
                "temperature": 0.7,
                "messages": [{"role": "user", "content": prompt}]
            })
        )
        result = json.loads(response["body"].read())
        answer = result["content"][0]["text"]
        logger.debug(f"Bedrock response: {answer}")

        # Step 3: Trả về câu trả lời
        response_data = {"text": answer}
        if audio_path:
            # Tạo audio trả lời bằng Polly
            polly = boto3.client("polly", region_name="ap-southeast-2")
            logger.debug("Synthesizing speech with Polly")
            polly_response = polly.synthesize_speech(
                Text=answer,
                OutputFormat="mp3",
                VoiceId="Joanna",
                Engine="neural"
            )
            output_audio = f"/tmp/{uuid.uuid4()}.mp3"
            with open(output_audio, "wb") as f:
                f.write(polly_response["AudioStream"].read())

            # Upload file MP3 lên S3
            output_audio_key = f"audio/output/{uuid.uuid4()}.mp3"
            logger.debug(f"Uploading MP3 to S3: {bucket_name}/{output_audio_key}")
            s3.upload_file(output_audio, bucket_name, output_audio_key)
            output_audio_url = f"https://{bucket_name}.s3.ap-southeast-2.amazonaws.com/{output_audio_key}"
            response_data["audio_url"] = output_audio_url

        return response_data

    except Exception as e:
        logger.error(f"Processing error: {str(e)}")
        raise Exception(f"Processing error: {str(e)}")
    finally:
        # Dọn dẹp file và object S3
        if audio_path and os.path.exists(audio_path):
            logger.debug(f"Removing local audio file: {audio_path}")
            os.remove(audio_path)
        if transcript_file and os.path.exists(transcript_file):
            logger.debug(f"Removing local transcript file: {transcript_file}")
            os.remove(transcript_file)
        if output_audio and os.path.exists(output_audio):
            logger.debug(f"Removing local output audio: {output_audio}")
            os.remove(output_audio)
        if audio_key:
            try:
                logger.debug(f"Deleting S3 object: {bucket_name}/{audio_key}")
                s3.delete_object(Bucket=bucket_name, Key=audio_key)
            except Exception as e:
                logger.error(f"Failed to delete S3 audio object: {str(e)}")
        if transcript_key:
            try:
                logger.debug(f"Deleting S3 transcript: {bucket_name}/{transcript_key}")
                s3.delete_object(Bucket=bucket_name, Key=transcript_key)
            except Exception as e:
                logger.error(f"Failed to delete S3 transcript object: {str(e)}")
