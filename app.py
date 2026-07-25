import os
import uuid
import json
import time
import logging
import requests
import io
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, render_template, request, url_for, jsonify, abort, render_template_string
from flask_cors import CORS
from werkzeug.utils import secure_filename
from werkzeug.exceptions import RequestEntityTooLarge
from PIL import Image

# Cloudinary Imports
import cloudinary
import cloudinary.uploader
import cloudinary.api
from dotenv import load_dotenv

# Import MusicClient (Restored)
from utils.music_client import MusicClient

# Load environment variables from .env file
load_dotenv()

# ---------------- LOGGING CONFIGURATION ----------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# ---------------- CONFIGURATION ----------------
class Config:
    # Cloudinary Config
    CLOUDINARY_CLOUD_NAME = os.getenv('CLOUDINARY_CLOUD_NAME')
    CLOUDINARY_API_KEY = os.getenv('CLOUDINARY_API_KEY')
    CLOUDINARY_API_SECRET = os.getenv('CLOUDINARY_API_SECRET')
    
    SECRET_KEY = os.environ.get('SECRET_KEY', 'default-dev-key-please-change')
    MAX_CONTENT_LENGTH = 100 * 1024 * 1024 
    ALLOWED_IMAGES = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
    ALLOWED_MUSIC = {'mp3', 'wav', 'ogg'}
    MAX_GALLERY = 8
    MAX_IMAGE_SIZE = (1024, 1024)

# Initialize App
app = Flask(__name__, static_url_path="/static", static_folder="static", template_folder="templates")
app.config.from_object(Config)
logger.info("Starting WishCraft App - Version: 2026-03-16-v1")

# Configure Cloudinary
if Config.CLOUDINARY_CLOUD_NAME and Config.CLOUDINARY_API_KEY and Config.CLOUDINARY_API_SECRET:
    cloudinary.config(
        cloud_name=Config.CLOUDINARY_CLOUD_NAME,
        api_key=Config.CLOUDINARY_API_KEY,
        api_secret=Config.CLOUDINARY_API_SECRET
    )
    logger.info("Cloudinary configured successfully.")
else:
    logger.warning("Cloudinary credentials missing! App will fail to upload.")

CORS(app, resources={r"/*": {"origins": "*"}})

# Thread pool
executor = ThreadPoolExecutor(max_workers=4)
music_client = MusicClient()

# ---------------- SECURITY HEADERS ----------------
@app.after_request
def add_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    # Allow iframe embedding for simulator support
    # response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    return response

# ---------------- ERROR HANDLERS ----------------
@app.errorhandler(413)
@app.errorhandler(RequestEntityTooLarge)
def file_too_large(e):
    return jsonify({"error": "File is too large. Maximum limit is 100MB."}), 413

@app.errorhandler(404)
def page_not_found(e):
    return jsonify({"error": "Resource not found"}), 404

@app.errorhandler(500)
def internal_error(e):
    logger.error(f"Server Error: {e}")
    return jsonify({"error": "Internal server error"}), 500

# ---------------- UTILITIES ----------------
def allowed(filename, types):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in types

def upload_image_task(file_storage, public_id, folder, uid):
    """
    Worker function to upload images to Cloudinary AND save locally as fallback.
    """
    local_url = None
    # 1. Save Locally First
    try:
        # static/uploads/<uid>/<public_id>.png
        ext = file_storage.filename.rsplit('.', 1)[1].lower() if '.' in file_storage.filename else 'png'
        local_dir = os.path.join("static", "uploads", uid)
        os.makedirs(local_dir, exist_ok=True)
        local_filename = f"{public_id}.{ext}"
        local_path = os.path.join(local_dir, local_filename)
        
        file_storage.seek(0)
        file_storage.save(local_path)
        local_url = f"/static/uploads/{uid}/{local_filename}"
        logger.info(f"Image saved locally: {local_path}")
    except Exception as e:
        logger.error(f"Failed to save image locally: {e}")

    # 2. Upload to Cloudinary
    try:
        file_storage.seek(0)
        result = cloudinary.uploader.upload(
            file_storage,
            public_id=public_id,
            folder=folder,
            resource_type="image",
            transformation=[
                {'width': 1024, 'height': 1024, 'crop': 'limit'},
                {'quality': 'auto', 'fetch_format': 'auto'}
            ]
        )
        return result.get('secure_url') or local_url
    except Exception as e:
        logger.error(f"Cloudinary image upload failed ({public_id}): {str(e)}")
        return local_url

def upload_raw_task(data, public_id, folder, uid):
    """
    Uploads JSON data as a raw file to Cloudinary AND saves it locally as fallback.
    """
    # 1. Local Fallback (CRITICAL for dev/missing keys)
    try:
        local_dir = os.path.join("generated", uid)
        os.makedirs(local_dir, exist_ok=True)
        local_path = os.path.join(local_dir, f"manifest_{uid}.json")
        with open(local_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        logger.info(f"Manifest saved locally: {local_path}")
    except Exception as e:
        logger.error(f"Failed to save manifest locally: {e}")

    # 2. Cloudinary Upload
    try:
        # Convert dict to JSON string bytes
        json_data = json.dumps(data).encode('utf-8')
        
        result = cloudinary.uploader.upload(
            json_data,
            public_id=public_id,
            folder=folder,
            resource_type="raw",
            format="json"
        )
        return result.get('secure_url')
    except Exception as e:
        logger.error(f"Failed to upload manifest to Cloudinary: {str(e)}")
        return None

def get_manifest(uid):
    """
    Priority: 1. Local File System, 2. Cloudinary
    """
    # 1. Try Local File System
    try:
        local_path = os.path.join("generated", uid, f"manifest_{uid}.json")
        if os.path.exists(local_path):
            with open(local_path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Error reading local manifest for {uid}: {e}")

    # 2. Try Cloudinary
    try:
        cloud_name = Config.CLOUDINARY_CLOUD_NAME
        if not cloud_name:
            return None
            
        # URL format: https://res.cloudinary.com/<cloud_name>/raw/upload/<folder>/<public_id>
        url = f"https://res.cloudinary.com/{cloud_name}/raw/upload/birthday_app/{uid}/manifest_{uid}.json"
        
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            return response.json()
        else:
            logger.warning(f"Manifest not found in Cloudinary for {uid}")
            return None
            
    except Exception as e:
        logger.error(f"Error fetching manifest from Cloudinary for {uid}: {e}")
        return None

def background_upload_and_update_manifest(uid, local_image_paths, local_music_path, manifest_data):
    """
    Worker function running in a background thread.
    Uploads local files to Cloudinary and updates the manifest JSON file locally and on Cloudinary.
    """
    logger.info(f"[{uid}] Starting background upload task to Cloudinary")
    folder_name = f"birthday_app/{uid}"
    
    # Track if anything changed to avoid useless writes/uploads
    changed = False

    # 1. Upload Main Image
    main_path = local_image_paths.get('main_image')
    if main_path and os.path.exists(main_path):
        try:
            filename = os.path.basename(main_path).rsplit('.', 1)[0]
            public_id = f"main_{filename}"
            logger.info(f"[{uid}] Background uploading main image: {main_path}")
            result = cloudinary.uploader.upload(
                main_path,
                public_id=public_id,
                folder=folder_name,
                resource_type="image",
                transformation=[
                    {'width': 1024, 'height': 1024, 'crop': 'limit'},
                    {'quality': 'auto', 'fetch_format': 'auto'}
                ]
            )
            secure_url = result.get('secure_url')
            if secure_url:
                manifest_data['context']['main_image'] = secure_url
                changed = True
                logger.info(f"[{uid}] Main image uploaded successfully: {secure_url}")
        except Exception as e:
            logger.error(f"[{uid}] Background main image upload failed: {e}")

    # 2. Upload Gift Image
    gift_path = local_image_paths.get('gift_image')
    if gift_path and os.path.exists(gift_path):
        try:
            filename = os.path.basename(gift_path).rsplit('.', 1)[0]
            public_id = f"gift_{filename}"
            logger.info(f"[{uid}] Background uploading gift image: {gift_path}")
            result = cloudinary.uploader.upload(
                gift_path,
                public_id=public_id,
                folder=folder_name,
                resource_type="image",
                transformation=[
                    {'width': 1024, 'height': 1024, 'crop': 'limit'},
                    {'quality': 'auto', 'fetch_format': 'auto'}
                ]
            )
            secure_url = result.get('secure_url')
            if secure_url:
                manifest_data['context']['gift_image'] = secure_url
                changed = True
                logger.info(f"[{uid}] Gift image uploaded successfully: {secure_url}")
        except Exception as e:
            logger.error(f"[{uid}] Background gift image upload failed: {e}")

    # 3. Upload Gallery Images
    gallery_paths = local_image_paths.get('gallery', [])
    cloudinary_gallery_urls = []
    if gallery_paths:
        for i, path in enumerate(gallery_paths):
            if path and os.path.exists(path):
                try:
                    filename = os.path.basename(path).rsplit('.', 1)[0]
                    public_id = f"gallery_{i}_{filename}"
                    logger.info(f"[{uid}] Background uploading gallery image {i}: {path}")
                    result = cloudinary.uploader.upload(
                        path,
                        public_id=public_id,
                        folder=folder_name,
                        resource_type="image",
                        transformation=[
                            {'width': 1024, 'height': 1024, 'crop': 'limit'},
                            {'quality': 'auto', 'fetch_format': 'auto'}
                        ]
                    )
                    secure_url = result.get('secure_url')
                    if secure_url:
                        cloudinary_gallery_urls.append(secure_url)
                        logger.info(f"[{uid}] Gallery image {i} uploaded successfully: {secure_url}")
                    else:
                        local_url = f"/static/uploads/{uid}/{os.path.basename(path)}"
                        cloudinary_gallery_urls.append(local_url)
                except Exception as e:
                    logger.error(f"[{uid}] Background gallery image {i} upload failed: {e}")
                    local_url = f"/static/uploads/{uid}/{os.path.basename(path)}"
                    cloudinary_gallery_urls.append(local_url)
        
        if cloudinary_gallery_urls:
            manifest_data['context']['gallery_images'] = cloudinary_gallery_urls
            changed = True

    # 4. Upload Music
    music_path = local_music_path
    if music_path and os.path.exists(music_path):
        try:
            filename = os.path.basename(music_path).rsplit('.', 1)[0]
            public_id = f"music_{filename}"
            logger.info(f"[{uid}] Background uploading music: {music_path}")
            result = cloudinary.uploader.upload(
                music_path,
                public_id=public_id,
                folder=folder_name,
                resource_type="video"
            )
            secure_url = result.get('secure_url')
            if secure_url:
                manifest_data['context']['music'] = secure_url
                changed = True
                logger.info(f"[{uid}] Music uploaded successfully: {secure_url}")
        except Exception as e:
            logger.error(f"[{uid}] Background music upload failed: {e}")

    # 5. Save updated manifest locally and upload it to Cloudinary
    if changed:
        try:
            local_dir = os.path.join("generated", uid)
            os.makedirs(local_dir, exist_ok=True)
            local_path = os.path.join(local_dir, f"manifest_{uid}.json")
            with open(local_path, "w", encoding="utf-8") as f:
                json.dump(manifest_data, f, indent=2)
            logger.info(f"[{uid}] Background thread: Updated manifest saved locally: {local_path}")
        except Exception as e:
            logger.error(f"[{uid}] Background thread: Failed to save updated manifest locally: {e}")

        # Upload manifest to Cloudinary
        try:
            json_bytes = json.dumps(manifest_data).encode('utf-8')
            cloudinary.uploader.upload(
                json_bytes,
                public_id=f"manifest_{uid}.json",
                folder=folder_name,
                resource_type="raw",
                format="json"
            )
            logger.info(f"[{uid}] Background thread: Uploaded updated manifest to Cloudinary")
        except Exception as e:
            logger.error(f"[{uid}] Background thread: Failed to upload updated manifest to Cloudinary: {e}")
    else:
        # Upload the initial manifest to Cloudinary anyway (so it exists in both places)
        try:
            json_bytes = json.dumps(manifest_data).encode('utf-8')
            cloudinary.uploader.upload(
                json_bytes,
                public_id=f"manifest_{uid}.json",
                folder=folder_name,
                resource_type="raw",
                format="json"
            )
            logger.info(f"[{uid}] Background thread: Uploaded initial manifest to Cloudinary (no files uploaded)")
        except Exception as e:
            logger.error(f"[{uid}] Background thread: Failed to upload initial manifest to Cloudinary: {e}")

# ---------------- ROUTES ----------------

@app.route('/health')
def health():
    return jsonify({
        "status": "healthy",
        "cloudinary": bool(Config.CLOUDINARY_CLOUD_NAME),
        "spotify": bool(os.getenv('SPOTIFY_CLIENT_ID')),
        "storage": "hybrid (local + cloudinary)"
    }), 200

# Restored Music Search Route
@app.route('/api/search-music', methods=['POST'])
def search_music():
    try:
        data = request.json
        query = data.get('query')
        if not query:
            return jsonify({"error": "Query is required"}), 400
        
        results = music_client.search(query)
        return jsonify({"results": results})
    except Exception as e:
        logger.error(f"Search API Error: {e}")
        return jsonify({"error": "Failed to search music"}), 500

@app.route('/')
def landing():
    return render_template("index.html")

@app.route('/generate', methods=['POST'])
def generate():
    start_time = time.time()
    request_id = uuid.uuid4().hex[:8]
    logger.info(f"[{request_id}] Starting generation request")

    try:
        # --- Input Validation & Setup ---
        name = request.form.get('name', 'Friend')
        user_title = request.form.get('title', "").strip()
        raw_messages = request.form.get('messages', "")
        messages = [m.strip() for m in raw_messages.split("\n") if m.strip()][:20]
        template = request.form.get('template', 'birthday.html')

        # Safe title logic
        if not user_title:
            title_map = {
                'birthday.html': "🎉 Happy Birthday",
                'anniversary.html': "💖 Happy Anniversary",
                'birthday2.html': "🎊 Happy Birthday",
                'birthday3.html': "🎊 Happy Birthday",
                'birthday_v2.html': "✨ Happy Birthday",
                'birthday_emotional.html': "✨ Happy Birthday",
                'birthday_premium.html': "✨ Grand Premium Celebration",
                'birthday_diary.html': "💖 LDR Anniversary Keepsake Diary",
                'birthday_movie.html': "🎬 THE MOVIE — A Cinematic Celebration",
                'sorry_emotional.html': "✨ From the Heart",
                'valentine.html': "💖 Happy Valentine's Day"
            }
            title = title_map.get(template, "🎉 Celebration")
        else:
            title = user_title

        # Unique ID for this page
        uid = uuid.uuid4().hex[:10]
        folder_name = f"birthday_app/{uid}"

        # --- Local Asset Saving ---
        local_dir = os.path.join("static", "uploads", uid)
        os.makedirs(local_dir, exist_ok=True)
        
        local_image_paths = {
            'gallery': []
        }
        local_music_path = None

        def save_file_locally(file_storage, prefix):
            if file_storage and file_storage.filename:
                ext = file_storage.filename.rsplit('.', 1)[1].lower() if '.' in file_storage.filename else 'png'
                safe_name = secure_filename(file_storage.filename).rsplit('.', 1)[0]
                local_filename = f"{prefix}_{safe_name}.{ext}"
                local_path = os.path.join(local_dir, local_filename)
                
                file_storage.seek(0)
                file_storage.save(local_path)
                
                local_url = f"/static/uploads/{uid}/{local_filename}"
                return local_path, local_url
            return None, None

        # 1. Main Image
        main_file = request.files.get("main_image")
        main_url = None
        if main_file and allowed(main_file.filename, Config.ALLOWED_IMAGES):
            local_path, local_url = save_file_locally(main_file, "main")
            if local_path:
                local_image_paths['main_image'] = local_path
                main_url = local_url
        if not main_url:
            main_url = request.form.get("main_image_selected")

        # 2. Gift Image
        gift_file = request.files.get("gift_image")
        gift_url = None
        if gift_file and allowed(gift_file.filename, Config.ALLOWED_IMAGES):
            local_path, local_url = save_file_locally(gift_file, "gift")
            if local_path:
                local_image_paths['gift_image'] = local_path
                gift_url = local_url
        if not gift_url:
            gift_url = request.form.get("gift_image_selected") or "/static/default_gift.png"

        # 3. Gallery Images
        gallery_urls = []
        for i, g_file in enumerate(request.files.getlist("gallery")[:Config.MAX_GALLERY]):
            if g_file and allowed(g_file.filename, Config.ALLOWED_IMAGES):
                local_path, local_url = save_file_locally(g_file, f"gallery_{i}")
                if local_path:
                    local_image_paths['gallery'].append(local_path)
                    gallery_urls.append(local_url)

        # 4. Music File
        music_file = request.files.get("music")
        music_url = "/static/default_music.mp3"
        if music_file and allowed(music_file.filename, Config.ALLOWED_MUSIC):
            local_path, local_url = save_file_locally(music_file, "music")
            if local_path:
                local_music_path = local_path
                music_url = local_url
        else:
            music_url = request.form.get('music_selected') or request.form.get('music_option') or music_url

        # --- Payment Tracking ---
        payment_method = request.form.get("payment_method", "none")
        payment_status = "pending verification" if payment_method in ["upi", "paypal"] else "not required"
        upi_name = request.form.get("upi_name", "")
        upi_utr = request.form.get("upi_utr", "")

        # --- Create Manifest ---
        manifest_data = {
            "template": template,
            "created_at": time.time(),
            "uid": uid,
            "payment_method": payment_method,
            "payment_status": payment_status,
            "context": {
                "name": name,
                "title": title,
                "messages": messages,
                "main_image": main_url,
                "gift_image": gift_url,
                "music": music_url,
                "gallery_link": url_for("gallery_page", uid=uid, _external=True),
                "gallery_images": gallery_urls,
                "movie_title": request.form.get("movie_title", "").strip(),
                "movie_subtitle": request.form.get("movie_subtitle", "").strip(),
                "movie_genre": request.form.get("movie_genre", "Friendship").strip(),
                "story_how_met": request.form.get("story_how_met", "").strip(),
                "story_funny_memories": request.form.get("story_funny_memories", "").strip(),
                "story_important_moments": request.form.get("story_important_moments", "").strip(),
                "story_personal_message": request.form.get("story_personal_message", "").strip(),
                "story_future_wishes": request.form.get("story_future_wishes", "").strip()
            }
        }

        # Log Payment locally and push to Firebase
        if payment_method in ["upi", "paypal"]:
            payment_record = {
                "uid": uid,
                "name": name,
                "template": template,
                "payment_method": payment_method,
                "payment_status": payment_status,
                "upi_name": upi_name,
                "upi_utr": upi_utr,
                "timestamp": time.time(),
                "date": time.strftime('%Y-%m-%d %H:%M:%S')
            }
            
            try:
                payments_file = "payments.json"
                payments = []
                if os.path.exists(payments_file):
                    with open(payments_file, "r") as f:
                        payments = json.load(f)
                payments.append(payment_record)
                with open(payments_file, "w") as f:
                    json.dump(payments, f, indent=4)
            except Exception as e:
                logger.error(f"Failed to log payment locally for UID {uid}: {e}")

            # Push to Firebase Firestore
            try:
                firebase_project_id = "wishcraft-cb4b6"
                firebase_api_key = "AIzaSyAyof4JXS10wiMyJVNFoDpSxs713bId3Ug"
                
                import urllib.parse
                safe_name = urllib.parse.quote((upi_name or name).replace(" ", "_"))
                doc_id = f"{safe_name}_{uid[:6]}"
                
                firestore_url = f"https://firestore.googleapis.com/v1/projects/{firebase_project_id}/databases/(default)/documents/payments?documentId={doc_id}&key={firebase_api_key}"
                
                firestore_payload = {
                    "fields": {
                        "uid": {"stringValue": payment_record["uid"]},
                        "name": {"stringValue": payment_record["name"]},
                        "template": {"stringValue": payment_record["template"]},
                        "payment_method": {"stringValue": payment_record["payment_method"]},
                        "payment_status": {"stringValue": payment_record["payment_status"]},
                        "upi_name": {"stringValue": payment_record["upi_name"]},
                        "upi_utr": {"stringValue": payment_record["upi_utr"]},
                        "timestamp": {"doubleValue": payment_record["timestamp"]},
                        "date": {"stringValue": payment_record["date"]}
                    }
                }
                
                def push_to_firebase(url, payload):
                    try:
                        requests.post(url, json=payload, timeout=5)
                    except Exception as e:
                        logger.error(f"Firebase push failed: {e}")
                
                executor.submit(push_to_firebase, firestore_url, firestore_payload)
            except Exception as e:
                logger.error(f"Failed to initiate Firebase push for UID {uid}: {e}")

        # Save manifest locally first (so it can be served instantly)
        try:
            local_manifest_dir = os.path.join("generated", uid)
            os.makedirs(local_manifest_dir, exist_ok=True)
            local_manifest_path = os.path.join(local_manifest_dir, f"manifest_{uid}.json")
            with open(local_manifest_path, "w", encoding="utf-8") as f:
                json.dump(manifest_data, f, indent=2)
            logger.info(f"Initial manifest saved locally: {local_manifest_path}")
        except Exception as e:
            logger.error(f"Failed to save initial manifest locally: {e}")

        # Spawn the background worker to upload files and update manifest on Cloudinary
        executor.submit(background_upload_and_update_manifest, uid, local_image_paths, local_music_path, manifest_data)

        duration = time.time() - start_time
        logger.info(f"[{request_id}] Local generation complete in {duration:.2f}s. Spawning background upload task. UID: {uid}")
        
        link = request.host_url.rstrip('/') + f"/generated/{uid}/"
        return jsonify({"link": link, "uid": uid})

    except Exception as e:
        logger.error(f"[{request_id}] Generation failed: {str(e)}", exc_info=True)
        return jsonify({"error": "An error occurred while generating your page."}), 500

# ---------------- PREVIEW ROUTES ----------------

@app.route('/preview/<template_name>')
def preview_template(template_name):
    # Ensure template_name is safe
    allowed_templates = {
        'birthday.html', 'birthday2.html', 'birthday3.html',
        'birthday_emotional.html', 'birthday_premium.html',
        'birthday_v2.html', 'birthday_diary.html', 'sorry_emotional.html',
        'valentine.html', 'anniversary.html', 'birthday_movie.html'
    }
    if template_name not in allowed_templates:
        abort(404)
        
    mock_context = {
        "title": "🎉 Happy Birthday!",
        "name": "Jane Doe",
        "messages": [
            "Wishing you a day filled with love and laughter! 🎉",
            "May all your dreams and wishes come true! ✨",
            "You are an amazing friend! Cheers to another great year! 🥂",
            "Hope this year brings you success and happiness! ❤️"
        ],
        "main_image": "/static/previews/anime/hbd_privew.png",
        "gift_image": "/static/previews/hbd-gift4.png",
        "music": "/static/music/hb1.mp3",
        "gallery_link": "/preview/gallery",
        "gallery_images": [
            "/static/previews/anime/hbd_privew.png",
            "/static/previews/anime/hbd2-preview.png",
            "/static/previews/anime/hbd3_privew.png",
            "/static/previews/anime/emotional_preview.png"
        ]
    }
    
    if template_name == 'sorry_emotional.html':
        mock_context["title"] = "🙏 Sincere Apology"
        mock_context["name"] = "Alex"
        mock_context["messages"] = [
            "I'm truly sorry for what happened. 🙏",
            "Our bond means the world to me.",
            "Please accept my sincere apology. ❤️",
            "Hoping we can start fresh. 🌸"
        ]
        mock_context["main_image"] = "/static/previews/anime/sorry_preview.png"
        mock_context["gift_image"] = "/static/previews/hbd-gift3.png"
        mock_context["music"] = "/static/music/hb2.mp3"
    elif template_name == 'valentine.html':
        mock_context["title"] = "💖 Happy Valentine's Day!"
        mock_context["name"] = "My Love"
        mock_context["messages"] = [
            "You make my heart skip a beat. 💓",
            "Every day is valentine's day when I am with you.",
            "To the most beautiful person in the world! 💕",
            "I love you to the moon and back! 🌙❤️"
        ]
        mock_context["main_image"] = "/static/previews/anime/valentine_preview.png"
        mock_context["gift_image"] = "/static/previews/hbd-gift2.png"
        mock_context["music"] = "/static/music/hb3.mp3"
    elif template_name == 'anniversary.html':
        mock_context["title"] = "💑 Happy Anniversary!"
        mock_context["name"] = "Sweetheart"
        mock_context["messages"] = [
            "Happy Anniversary to my better half! 💑",
            "Thank you for another year of beautiful memories.",
            "Looking forward to an eternity with you. ❤️",
            "Happy Anniversary! 🥂✨"
        ]
        mock_context["main_image"] = "/static/previews/anime/anversary-preview.png"
        mock_context["gift_image"] = "/static/previews/hbd-gift1.png"
        mock_context["music"] = "/static/music/hb4.mp3"
    elif template_name == 'birthday_premium.html':
        mock_context["main_image"] = "/static/previews/anime/premium_cake_preview.png"
        mock_context["gift_image"] = "/static/previews/hbd-gift1.png"
    elif template_name == 'birthday_v2.html':
        mock_context["main_image"] = "/static/previews/anime/birthday_v2_preview.png"
    elif template_name == 'birthday_emotional.html':
        mock_context["main_image"] = "/static/previews/anime/emotional_preview.png"
    elif template_name == 'birthday_diary.html':
        mock_context["main_image"] = "/static/previews/diary_preview.png"
        mock_context["gift_image"] = "/static/previews/hbd-gift1.png"
    elif template_name == 'birthday_movie.html':
        mock_context["main_image"] = "/static/previews/anime/premium_cake_preview.png"
        mock_context["gift_image"] = "/static/previews/hbd-gift1.png"
        mock_context["music"] = "/static/music/hb1.mp3"
        mock_context["movie_title"] = "The Story of Jane"
        mock_context["movie_subtitle"] = "A story worth remembering"
        mock_context["movie_genre"] = "Friendship"
        mock_context["story_how_met"] = "We met back in high school math class, bonding over how much we both hated calculus."
        mock_context["story_funny_memories"] = "That one time we tried to bake a cake, completely forgot the baking powder, and ended up with a sweet brick."
        mock_context["story_important_moments"] = "Traveling together across the country, laughing at silly road signs, and talking until sunrise."
        mock_context["story_personal_message"] = "You have been my rock, my partner in crime, and the absolute best person I know."
        mock_context["story_future_wishes"] = "May this next chapter bring you endless laughter, success, and all the happiness in the world."

    return render_template(template_name, **mock_context)

@app.route('/preview/gallery')
def preview_gallery():
    mock_images = [
        "/static/previews/anime/hbd_privew.png",
        "/static/previews/anime/hbd2-preview.png",
        "/static/previews/anime/hbd3_privew.png",
        "/static/previews/anime/emotional_preview.png"
    ]
    return render_template(
        "gallery.html",
        name="Gallery Preview",
        title="Sample Memories",
        images=mock_images,
        music="/static/music/hb1.mp3"
    )

# ---------------- PAGE SERVING ----------------


@app.route('/generated/<uid>/')
def generated_page(uid):
    try:
        data = get_manifest(uid)
        
        if not data:
            abort(404)

        return render_template(data['template'], **data['context'])
    except Exception as e:
        logger.error(f"Error serving page {uid}: {e}")
        abort(404)

@app.route('/generated/<uid>/gallery')
def gallery_page(uid):
    try:
        data = get_manifest(uid)
        
        if not data:
            abort(404)
            
        ctx = data['context']
        return render_template(
            "gallery.html",
            name="Gallery",
            title="Memories",
            images=ctx.get('gallery_images', []),
            music=ctx.get('music')
        )
    except Exception:
        abort(404)

@app.route('/admin/payments')
def admin_payments():
    # Simple protection via query parameter (e.g. ?key=secret123)
    # In production, use robust authentication.
    key = request.args.get('key')
    if key != 'admin123':
        abort(403)
        
    payments = []
    if os.path.exists("payments.json"):
        try:
            with open("payments.json", "r") as f:
                payments = json.load(f)
                # Sort newest first
                payments.reverse()
        except Exception as e:
            logger.error(f"Error reading payments: {e}")
            
    # Simple HTML output for the admin panel
    html = '''
    <html>
    <head>
        <title>Payments Dashboard</title>
        <style>
            body { font-family: sans-serif; padding: 20px; background: #f8f9fa; }
            table { width: 100%; border-collapse: collapse; background: white; }
            th, td { padding: 10px; border: 1px solid #ddd; text-align: left; }
            th { background: #343a40; color: white; }
            .upi { color: #10b981; font-weight: bold; }
            .paypal { color: #3b82f6; font-weight: bold; }
            .pending { color: #f59e0b; }
        </style>
    </head>
    <body>
        <h2>Payments Dashboard</h2>
        <table>
            <tr>
                <th>Date</th>
                <th>UID</th>
                <th>Name</th>
                <th>Template</th>
                <th>Method</th>
                <th>UPI Name</th>
                <th>UPI UTR</th>
                <th>Status</th>
            </tr>
            {% for p in payments %}
            <tr>
                <td>{{ p.date }}</td>
                <td><a href="/generated/{{ p.uid }}/" target="_blank">{{ p.uid }}</a></td>
                <td>{{ p.name }}</td>
                <td>{{ p.template }}</td>
                <td class="{{ p.payment_method }}">{{ p.payment_method | upper }}</td>
                <td>{{ p.upi_name or '-' }}</td>
                <td>{{ p.upi_utr or '-' }}</td>
                <td class="pending">{{ p.payment_status }}</td>
            </tr>
            {% endfor %}
            {% if not payments %}
            <tr><td colspan="8" style="text-align: center;">No payments recorded yet.</td></tr>
            {% endif %}
        </table>
    </body>
    </html>
    '''
    return render_template_string(html, payments=payments)

if __name__ == "__main__":
    print("WARNING: Run with Gunicorn in production!")
    app.run(host="0.0.0.0", port=5001, debug=True)