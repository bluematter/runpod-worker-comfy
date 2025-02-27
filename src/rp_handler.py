import runpod
from runpod.serverless.utils import rp_upload
import json
import urllib.request
import urllib.parse
import time
import os
import requests
import base64
from io import BytesIO
import redis
from datetime import datetime
import logging
import uuid
import sys

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Add ComfyUI directory to Python path
comfy_path = "/comfyui"
if comfy_path not in sys.path:
    sys.path.append(comfy_path)

# Redis configuration from environment variables
REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("REDIS_PORT", 6379))
r = redis.Redis(
    host='redis-13524.fcrce180.us-east-1-1.ec2.redns.redis-cloud.com',
    port=13524,
    decode_responses=True,
    username="default",
    password="Z8w8yMLSTJ6HZqGUoIw4cnUsb36qQuWf",
)

# Time to wait between API check attempts in milliseconds
COMFY_API_AVAILABLE_INTERVAL_MS = 50
# Maximum number of API check attempts
COMFY_API_AVAILABLE_MAX_RETRIES = 500
# Time to wait between poll attempts in milliseconds
COMFY_POLLING_INTERVAL_MS = int(os.environ.get("COMFY_POLLING_INTERVAL_MS", 250))
# Maximum number of poll attempts
COMFY_POLLING_MAX_RETRIES = int(os.environ.get("COMFY_POLLING_MAX_RETRIES", 500))
# Host where ComfyUI is running
COMFY_HOST = "127.0.0.1:8188"
# Enforce a clean state after each job is done
REFRESH_WORKER = os.environ.get("REFRESH_WORKER", "false").lower() == "true"

def update_redis(job_id, state, workflow=None, result=None, error=None):
    """Update job state in Redis."""
    job = {
        "id": job_id,
        "state": state,
        "workflow": workflow if workflow is not None else {},
        "result": result,
        "error": error,
        "updated_at": datetime.utcnow().isoformat()
    }
    try:
        r.set(f"job:{job_id}", json.dumps(job))
        logger.info(f"Updated job {job_id} state to {state}")
    except Exception as e:
        logger.error(f"Failed to update Redis for job {job_id}: {str(e)}")

def validate_input(job_input):
    """Validates the input for the handler function."""
    if job_input is None:
        return None, "Please provide input"

    if isinstance(job_input, str):
        try:
            job_input = json.loads(job_input)
        except json.JSONDecodeError:
            return None, "Invalid JSON format in input"

    workflow = job_input.get("workflow")
    if workflow is None:
        return None, "Missing 'workflow' parameter"

    images = job_input.get("images")
    if images is not None:
        if not isinstance(images, list) or not all(
            "name" in image and "image" in image for image in images
        ):
            return None, "'images' must be a list of objects with 'name' and 'image' keys"

    return {"workflow": workflow, "images": images}, None

def check_server(url, retries=500, delay=50):
    """Check if a server is reachable via HTTP GET request."""
    for i in range(retries):
        try:
            response = requests.get(url)
            if response.status_code == 200:
                logger.info("ComfyUI API is reachable")
                return True
        except requests.RequestException:
            pass
        time.sleep(delay / 1000)
    logger.error(f"Failed to connect to {url} after {retries} attempts")
    return False

def upload_images(images):
    """Upload a list of base64 encoded images to the ComfyUI server."""
    if not images:
        return {"status": "success", "message": "No images to upload", "details": []}

    responses = []
    upload_errors = []
    logger.info("Starting image upload")

    for image in images:
        name = image["name"]
        image_data = image["image"]
        blob = base64.b64decode(image_data)
        files = {
            "image": (name, BytesIO(blob), "image/png"),
            "overwrite": (None, "true"),
        }
        response = requests.post(f"http://{COMFY_HOST}/upload/image", files=files)
        if response.status_code != 200:
            upload_errors.append(f"Error uploading {name}: {response.text}")
        else:
            responses.append(f"Successfully uploaded {name}")

    if upload_errors:
        logger.error("Image upload completed with errors")
        return {
            "status": "error",
            "message": "Some images failed to upload",
            "details": upload_errors,
        }
    logger.info("Image upload completed successfully")
    return {
        "status": "success",
        "message": "All images uploaded successfully",
        "details": responses,
    }

def queue_workflow(workflow):
    """Queue a workflow to be processed by ComfyUI."""
    data = json.dumps({"prompt": workflow}).encode("utf-8")
    req = urllib.request.Request(f"http://{COMFY_HOST}/prompt", data=data)
    return json.loads(urllib.request.urlopen(req).read())

def get_history(prompt_id):
    """Retrieve the history of a given prompt using its ID."""
    with urllib.request.urlopen(f"http://{COMFY_HOST}/history/{prompt_id}") as response:
        return json.loads(response.read())

def base64_encode(img_path):
    """Returns base64 encoded image."""
    with open(img_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode("utf-8")

def process_output_images(outputs, job_id):
    """Process generated images and return as S3 URL or base64."""
    COMFY_OUTPUT_PATH = os.environ.get("COMFY_OUTPUT_PATH", "/comfyui/output")
    output_images = []

    for node_id, node_output in outputs.items():
        if "images" in node_output:
            for image in node_output["images"]:
                output_images.append(os.path.join(image["subfolder"], image["filename"]))

    logger.info("Image generation completed")
    processed_images = []

    for rel_path in output_images:
        local_image_path = f"{COMFY_OUTPUT_PATH}/{rel_path}"
        logger.info(f"Processing image at {local_image_path}")

        if os.path.exists(local_image_path):
            if os.environ.get("BUCKET_ENDPOINT_URL", False):
                image_result = rp_upload.upload_image(job_id, local_image_path)
                logger.info("Image uploaded to AWS S3")
            else:
                image_result = base64_encode(local_image_path)
                logger.info("Image converted to base64")
            processed_images.append(image_result)
        else:
            logger.error(f"Image not found at {local_image_path}")
            processed_images.append(f"Image not found: {local_image_path}")

    return {
        "status": "success",
        "message": processed_images,
    }

def handler(job):
    """Main handler for processing a job."""
    job_id = job.get("id", str(uuid.uuid4()))  # Use provided job ID or generate one
    job_input = job["input"]

    # Validate input
    validated_data, error_message = validate_input(job_input)
    if error_message:
        update_redis(job_id, "FAILED", error=error_message)
        return {"error": error_message, "job_id": job_id}

    workflow = validated_data["workflow"]
    images = validated_data.get("images")

    # Write initial state to Redis
    update_redis(job_id, "NOT_STARTED", workflow=workflow)

    # Check ComfyUI availability
    if not check_server(f"http://{COMFY_HOST}", COMFY_API_AVAILABLE_MAX_RETRIES, COMFY_API_AVAILABLE_INTERVAL_MS):
        error = "ComfyUI API unavailable"
        update_redis(job_id, "FAILED", error=error)
        return {"error": error, "job_id": job_id}

    # Upload images if provided
    upload_result = upload_images(images)
    if upload_result["status"] == "error":
        update_redis(job_id, "FAILED", error=upload_result["message"])
        return {**upload_result, "job_id": job_id}

    # Queue the workflow
    try:
        update_redis(job_id, "IN_QUEUE")
        queued_workflow = queue_workflow(workflow)
        prompt_id = queued_workflow["prompt_id"]
        logger.info(f"Queued workflow with prompt ID {prompt_id}")
    except Exception as e:
        error = f"Error queuing workflow: {str(e)}"
        update_redis(job_id, "FAILED", error=error)
        return {"error": error, "job_id": job_id}

    # Poll for completion
    logger.info("Polling for image generation completion")
    retries = 0
    try:
        while retries < COMFY_POLLING_MAX_RETRIES:
            history = get_history(prompt_id)
            if prompt_id in history and history[prompt_id].get("outputs"):
                break
            time.sleep(COMFY_POLLING_INTERVAL_MS / 1000)
            retries += 1
        else:
            error = "Max retries reached while waiting for image generation"
            update_redis(job_id, "FAILED", error=error)
            return {"error": error, "job_id": job_id}
    except Exception as e:
        error = f"Error polling for image generation: {str(e)}"
        update_redis(job_id, "FAILED", error=error)
        return {"error": error, "job_id": job_id}

    # Process output images
    images_result = process_output_images(history[prompt_id].get("outputs"), job_id)
    if images_result["status"] == "success":
        update_redis(job_id, "COMPLETED", result=images_result["message"])
    else:
        update_redis(job_id, "FAILED", error="Image processing failed")

    return {
        "job_id": job_id,
        "status": images_result["status"],
        "message": images_result["message"],
        "refresh_worker": REFRESH_WORKER
    }

def preload_weights(checkpoint_name):
    try:
        # Import ComfyUI modules after adding to path
        from comfy.sd import load_checkpoint_guess_config
        import folder_paths
        
        # Load the model
        logger.info(f"Attempting to preload: {checkpoint_name}")
        
        # Use the exact path to the model in the RunPod volume
        model_path = os.path.join('runpod-volume/models/checkpoints', checkpoint_name)
        
        if not os.path.exists(model_path):
            logger.error(f"Model file not found at: {model_path}")
            return None
        
        logger.info(f"Found model at: {model_path}")
        
        # Get the embedding directory from ComfyUI's folder_paths
        embedding_directory = None
        try:
            embedding_directory = folder_paths.get_folder_paths("embeddings")
            logger.info(f"Embedding directory: {embedding_directory}")
        except Exception as e:
            logger.warning(f"Could not get embedding directory: {str(e)}")
        
        # Call the function with the correct arguments as used in ComfyUI
        logger.info(f"Calling load_checkpoint_guess_config with path: {model_path}")
        result = load_checkpoint_guess_config(
            model_path, 
            output_vae=True, 
            output_clip=True, 
            embedding_directory=embedding_directory
        )
        
        # Log the type of result to help with debugging
        logger.info(f"Result type: {type(result)}")
        
        if isinstance(result, tuple):
            logger.info(f"Result has {len(result)} elements")
            # If it's a tuple, it might be (model, clip, vae) or (model, clip, vae, ...)
            if len(result) >= 3:
                model, clip, vae = result[:3]
                logger.info(f"Successfully preloaded: {checkpoint_name}")
                return model, clip, vae
            else:
                logger.error(f"Not enough values returned: expected at least 3, got {len(result)}")
                return None
        else:
            # If it's not a tuple, it might be a single model object
            logger.info(f"Result is not a tuple, treating as single model object")
            return result, None, None
        
    except Exception as e:
        logger.error(f"Error preloading model {checkpoint_name}: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return None

def init():
    """Initialize the handler."""
    # Wait for ComfyUI to start up
    logger.info("Waiting for ComfyUI to initialize...")
    time.sleep(15)  # Give ComfyUI time to start
    
    # Check if models directory exists
    model_dir = 'runpod-volume/models/checkpoints'
    if os.path.exists(model_dir):
        logger.info(f"Model directory exists: {model_dir}")
        try:
            models = os.listdir(model_dir)
            logger.info(f"Available models: {models}")
        except Exception as e:
            logger.error(f"Error listing models: {str(e)}")
    else:
        logger.warning(f"Model directory does not exist: {model_dir}")
    
    # Models to preload
    models_to_preload = [
        "realvisxlV40_v40LightningBakedvae.safetensors",
        "pixelArtDiffusionXL_pixelWorld.safetensors"
    ]
    
    # Preload models
    preloaded = []
    for checkpoint_name in models_to_preload:
        try:
            result = preload_weights(checkpoint_name)
            if result is not None:
                preloaded.append(checkpoint_name)
                logger.info(f"Successfully preloaded: {checkpoint_name}")
        except Exception as e:
            logger.error(f"Failed to preload {checkpoint_name}: {str(e)}")
    
    logger.info(f"Preloaded {len(preloaded)} models: {preloaded}")

# Only initialize if this module is the main program
if __name__ == "__main__":
    # Initialize after a delay to ensure ComfyUI is running
    import threading
    threading.Timer(5.0, init).start()
    
    # Start the handler
    logger.info("Starting RunPod handler")
    runpod.serverless.start({"handler": handler})
else:
    # If imported as a module, don't run init() immediately
    logger.info("rp_handler module imported, initialization will be handled by main process")