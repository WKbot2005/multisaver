import re
import datetime
import sqlite3
from flask import Flask, request, jsonify
from flask_cors import CORS
import jwt
import bcrypt
import requests
import yt_dlp

app = Flask(__name__)
CORS(app)

SECRET_KEY = "super-secret-change-this-in-production"
DB_NAME = "multisaver.db"

def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                tier TEXT DEFAULT 'free'
            )
        ''')
        conn.commit()

init_db()

def get_user_from_token(token):
    if not token:
        return None
    try:
        data = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT tier FROM users WHERE id = ?", (data["user_id"],))
            user = cursor.fetchone()
            if user:
                return user[0]
    except:
        return None
    return None

# --- AUTHENTICATION API ---
@app.route('/api/register', methods=['POST'])
def register():
    data = request.json or {}
    email = data.get('email', '').strip().lower()
    password = data.get('password', '')
    if not email or not password:
        return jsonify({'success': False, 'error': 'Missing inputs.'})
    hashed_pw = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
    try:
        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO users (email, password) VALUES (?, ?)", (email, hashed_pw))
            conn.commit()
        return jsonify({'success': True})
    except sqlite3.IntegrityError:
        return jsonify({'success': False, 'error': 'Email exists already.'})

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json or {}
    email = data.get('email', '').strip().lower()
    password = data.get('password', '')
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, password, tier FROM users WHERE email = ?", (email,))
        user = cursor.fetchone()
    if user and bcrypt.checkpw(password.encode('utf-8'), user[1].encode('utf-8')):
        token = jwt.encode({'user_id': user[0], 'exp': datetime.datetime.utcnow() + datetime.timedelta(days=7)}, SECRET_KEY, algorithm="HS256")
        return jsonify({'success': True, 'token': token, 'tier': user[2]})
    return jsonify({'success': False, 'error': 'Invalid credentials.'})

# --- EXTRACTION ROUTE ---
@app.route('/get-download-link', methods=['POST'])
def get_link():
    data = request.json or {}
    video_url = data.get('url', '').strip()
    token = data.get('token')
    requested_quality = data.get('quality', 'high')
    
    user_tier = get_user_from_token(token) or 'free'

    if not video_url:
        return jsonify({'success': False, 'error': 'Please paste a valid URL.'})

    # Free tier quality soft cap rules
    if user_tier == 'free' and requested_quality == 'high':
        requested_quality = 'medium'

    # Mapping configurations for yt-dlp
    if requested_quality == 'high':
        ytdlp_format = 'bestvideo+bestaudio/best'
    elif requested_quality == 'medium':
        ytdlp_format = 'best[height<=720]/best'
    else:
        ytdlp_format = 'worst/best[height<=480]'

    try:
        # --- TWITTER / X PIPELINE ---
        if 'twitter.com' in video_url or 'x.com' in video_url:
            clean_url = video_url.replace('x.com', 'twitter.com')
            match = re.search(r"status/(\d+)", clean_url)
            if not match:
                return jsonify({'success': False, 'error': 'Invalid X link pattern.'})
            
            res = requests.get(f"https://api.vxtwitter.com/status/{match.group(1)}", timeout=10).json()
            
            if 'media_extended' in res and len(res['media_extended']) > 0:
                media = res['media_extended'][0]
                media_type = media.get('type', 'video')
                
                if media_type == 'image':
                    img_url = media.get('url', '')
                    if "?" in img_url:
                        img_url = img_url.split('?')[0] + "?name=orig"
                    return jsonify({
                        'success': True,
                        'type': 'image',
                        'title': 'HD Photo Post from X',
                        'thumbnail': img_url,
                        'download_url': img_url
                    })
                else:
                    return jsonify({
                        'success': True,
                        'type': 'video',
                        'title': res.get('text', 'X Video')[:50] + "...",
                        'thumbnail': media.get('thumbnail_url', ''),
                        'download_url': media.get('url')
                    })
            return jsonify({'success': False, 'error': 'No video or image asset found in this tweet.'})

        # --- TIKTOK PIPELINE ---
        elif 'tiktok.com' in video_url:
            res = requests.get(f"https://www.tikwm.com/api/?url={video_url}", timeout=10).json()
            if res.get('code') == 0:
                data = res.get('data', {})
                if 'images' in data and data['images']:
                    hd_photo = data['images'][0]
                    return jsonify({
                        'success': True,
                        'type': 'image',
                        'title': data.get('title', 'TikTok Photo Layout')[:50],
                        'thumbnail': hd_photo,
                        'download_url': hd_photo
                    })
                
                dl_url = data.get('play') if user_tier == 'premium' else data.get('wmplay')
                return jsonify({
                    'success': True,
                    'type': 'video',
                    'title': data.get('title', 'TikTok Video'),
                    'thumbnail': data.get('cover', ''),
                    'download_url': dl_url
                })
            return jsonify({'success': False, 'error': 'Could not read TikTok video structure.'})

        # --- UNIVERSAL PIPELINE (YOUTUBE, FACEBOOK, YOUCO, ETC) ---
        else:
            ydl_opts = {
                'format': ytdlp_format,
                'quiet': True,
                'no_warnings': True,
                'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(video_url, download=False)
                
                # Check if yt-dlp extracted an image file instead of video streams
                is_image = info.get('extractor') in ['generic', 'image'] or video_url.lower().endswith(('.png', '.jpg', '.jpeg', '.webp'))
                
                return jsonify({
                    'success': True,
                    'type': 'image' if is_image else 'video',
                    'title': info.get('title', 'Media File Output'),
                    'thumbnail': info.get('thumbnail', info.get('url')),
                    'download_url': info.get('url')
                })

    except Exception as e:
        return jsonify({'success': False, 'error': f"Extraction error: {str(e)}"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)