import os
import uuid
import sqlite3
import asyncio
import threading
import csv
from io import StringIO
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, send_from_directory, flash, jsonify, make_response
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from openai import OpenAI
import edge_tts

app = Flask(__name__)
# 设置 Session 的加密密钥，保证安全
app.secret_key = 'jieyou_secret_key'

# 配置文件上传路径为项目根目录下的 uploads 文件夹
UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
# 允许上传的头像文件格式
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}

# 确保上传目录与 TTS 生成目录存在
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(os.path.join(UPLOAD_FOLDER, 'tts'), exist_ok=True)

DATABASE = 'database.db'

# 负面情绪关键词
NEGATIVE_EMOTION_KEYWORDS = ["崩溃", "难过", "伤心", "抑郁", "痛苦", "压力", "焦虑", "失眠", "睡不着", "孤独", "绝望", "挂科", "失败", "好累", "无助"]

def filter_sensitive_words(text):
    """从数据库读取最新的敏感词词库，进行动态过滤替换为 ***"""
    if not text:
        return text
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute('SELECT word FROM sensitive_words')
        words = [row['word'] for row in cursor.fetchall()]
    except Exception as e:
        print(f"加载敏感词失败: {e}")
        words = ["垃圾", "暴力", "自杀", "死"]  # 静态兜底
    conn.close()
    
    filtered_text = text
    for word in words:
        filtered_text = filtered_text.replace(word, "***")
    return filtered_text

def get_db():
    """获取数据库连接"""
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """初始化数据库并升级结构以支持安全与统计模块"""
    conn = get_db()
    cursor = conn.cursor()
    
    # 1. 创建基本表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            nickname TEXT,
            avatar TEXT,
            theme TEXT DEFAULT 'warm'
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            role_id TEXT NOT NULL,
            sender TEXT NOT NULL,
            content TEXT NOT NULL,
            audio_url TEXT,
            created_at TEXT NOT NULL
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS treehole_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            nickname TEXT NOT NULL,
            category TEXT NOT NULL,
            content TEXT NOT NULL,
            status TEXT DEFAULT 'published',
            is_reported INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS mailbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            is_read INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )
    ''')
    
    # 2. 数据库兼容升级 (is_admin, is_active)
    try:
        cursor.execute('ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0')
    except sqlite3.OperationalError:
        pass
        
    try:
        cursor.execute('ALTER TABLE users ADD COLUMN is_active INTEGER DEFAULT 1')
    except sqlite3.OperationalError:
        pass
        
    # 3. 操作审计表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS admin_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER NOT NULL,
            admin_username TEXT NOT NULL,
            action_type TEXT NOT NULL,
            target TEXT NOT NULL,
            details TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    ''')

    # 4. 智能体角色表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ai_characters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role_key TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            gender TEXT NOT NULL,
            voice TEXT NOT NULL,
            prompt TEXT NOT NULL,
            temperature REAL DEFAULT 0.7,
            max_words INTEGER DEFAULT 80
        )
    ''')
    try:
        cursor.execute('ALTER TABLE ai_characters ADD COLUMN user_id INTEGER')
    except sqlite3.OperationalError:
        pass
    
    # 5. 动态敏感词黑名单库表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sensitive_words (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            word TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL
        )
    ''')
    
    # 6. 校园通知公告表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS announcements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            is_pinned INTEGER DEFAULT 0,
            expire_time TEXT NOT NULL,
            cover_image TEXT,
            created_at TEXT NOT NULL
        )
    ''')
    
    # 7. 树洞举报原因与举报人字段动态拓展
    try:
        cursor.execute('ALTER TABLE treehole_posts ADD COLUMN report_reason TEXT')
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute('ALTER TABLE treehole_posts ADD COLUMN reporter_username TEXT')
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute('ALTER TABLE treehole_posts ADD COLUMN likes INTEGER DEFAULT 0')
    except sqlite3.OperationalError:
        pass

    # 8. 默认管理员 Seeder
    cursor.execute('SELECT id FROM users WHERE username = "admin"')
    if not cursor.fetchone():
        hashed_password = generate_password_hash("123456")
        cursor.execute(
            'INSERT INTO users (username, email, password, nickname, is_admin, is_active) VALUES (?, ?, ?, ?, 1, 1)',
            ('admin', 'admin@jieyou.com', hashed_password, '系统管理员')
        )

    # 9. 默认陪伴角色 Seeder
    cursor.execute('SELECT id FROM ai_characters')
    if not cursor.fetchone():
        cursor.execute('''
            INSERT INTO ai_characters (role_key, name, gender, voice, prompt, temperature, max_words) VALUES 
            ('warm_listener', '温柔倾听者', 'female', 'zh-CN-XiaoxiaoNeural', '你是一位温柔、耐心、体贴的心理倾听者。你的声音温柔甜美，主要任务是为倾诉者提供情绪价值，倾听他们的烦恼，给予无条件的支持、关怀与安慰。不要长篇大论，回复尽量控制在80字以内，语气要非常温柔细腻。', 0.7, 80),
            ('rational_cave', '理智树洞', 'male', 'zh-CN-YunjianNeural', '你是一位冷静、睿智、客观的心理疏导员。你的风格是理性思考，擅长用简洁明了、富有逻辑的话语帮助倾诉者理清混乱的思绪，找到问题的症结。不要长篇大论，回复尽量控制在80字以内，语气沉稳、专业、有分寸。', 0.3, 80)
        ''')
        
    # 10. 默认敏感词 Seeder
    cursor.execute('SELECT id FROM sensitive_words')
    if not cursor.fetchone():
        local_now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        default_blacklist = ["垃圾", "暴力", "自杀", "毁灭", "死", "傻逼", "混蛋", "滚", "去死", "杀人", "毒品", "疯了"]
        for word in default_blacklist:
            cursor.execute('INSERT OR IGNORE INTO sensitive_words (word, created_at) VALUES (?, ?)', (word, local_now))
            
    # 11. 默认置顶校园公告 Seeder
    cursor.execute('SELECT id FROM announcements')
    if not cursor.fetchone():
        local_now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute('''
            INSERT INTO announcements (title, content, is_pinned, expire_time, cover_image, created_at) VALUES 
            ('第一届“解忧杂货铺”心理健康沙龙活动通知', '学校心理健康中心将于本周五下午 14:00 在行政楼多功能报告厅举办《开启青春期自我调节与解压的密码》专题沙龙。现场配有精美治愈礼品与免费心理特展咨询，欢迎同学们积极到场！', 1, '2026-12-31 23:59:59', '/static/css/default-banner.jpg', ?)
        ''', (local_now,))
        
    conn.commit()
    conn.close()

# 初始化数据库
init_db()

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session or not session.get('is_admin'):
            flash('您无权访问该管理后台！', 'danger')
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated_function

def log_admin_action(action_type, target, details):
    if 'user_id' in session and session.get('is_admin'):
        conn = get_db()
        cursor = conn.cursor()
        local_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute(
            'INSERT INTO admin_logs (admin_id, admin_username, action_type, target, details, created_at) VALUES (?, ?, ?, ?, ?, ?)',
            (session['user_id'], session['username'], action_type, target, details, local_time)
        )
        conn.commit()
        conn.close()

def get_api_key():
    try:
        key_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'aikey.txt')
        with open(key_file, 'r', encoding='utf-8') as f:
            return f.readline().strip()
    except Exception as e:
        print(f"读取 API Key 失败，将使用默认密钥: {e}")
        return "密钥出错"

def generate_tts_sync(text, voice, output_filename):
    output_path = os.path.join(app.config['UPLOAD_FOLDER'], 'tts', output_filename)
    async def _tts():
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(output_path)
    try:
        asyncio.run(_tts())
        return url_for('uploaded_file', filename=f"tts/{output_filename}")
    except Exception as e:
        print(f"TTS 语音合成失败: {e}")
        return None

def async_generate_ai_letter(user_id, post_content):
    def _job():
        api_key = get_api_key()
        prompt = (
            f"你是一位倾听学生心声的『解忧杂货铺店员』。系统检测到一位同学在匿名树洞中发布了以下负面情绪动态：\n"
            f"『{post_content}』\n"
            f"请以“解忧杂货铺店员”的身份写一封信来宽慰他。信件应极其温暖、治愈、温和，字数在150字以内。不要说自己是AI，像写信给朋友那样展开。"
        )
        try:
            client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
            completion = client.chat.completions.create(
                model="deepseek-v4-flash",
                messages=[{"role": "user", "content": prompt}],
                stream=False
            )
            ai_letter = completion.choices[0].message.content
            
            conn = sqlite3.connect(DATABASE)
            cursor = conn.cursor()
            cursor.execute(
                'INSERT INTO mailbox (user_id, title, content, is_read, created_at) VALUES (?, ?, ?, 0, ?)',
                (user_id, '【解忧杂货铺】店员给你的信', ai_letter, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            )
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"后台异步智能回信生成失败: {e}")
            
    threading.Thread(target=_job).start()

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# 静态文件访问
@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/')
def index():
    if 'user_id' in session:
        if session.get('is_admin') == 1:
            return redirect(url_for('admin_dashboard'))
        return redirect(url_for('chat'))
    return redirect(url_for('login'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if 'user_id' in session:
        return redirect(url_for('chat'))
        
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        
        if not username or not email or not password:
            flash('所有字段均为必填项！', 'danger')
            return redirect(url_for('register'))
            
        if password != confirm_password:
            flash('两次输入的密码不一致！', 'danger')
            return redirect(url_for('register'))
            
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT id FROM users WHERE username = ? OR email = ?', (username, email))
        if cursor.fetchone():
            flash('用户名或电子邮箱已存在！', 'danger')
            conn.close()
            return redirect(url_for('register'))
            
        hashed_password = generate_password_hash(password)
        cursor.execute(
            'INSERT INTO users (username, email, password, nickname, theme, is_admin, is_active) VALUES (?, ?, ?, ?, ?, 0, 1)',
            (username, email, hashed_password, username, 'warm')
        )
        conn.commit()
        conn.close()
        
        flash('注册成功！请登录。', 'success')
        return redirect(url_for('login'))
        
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('chat'))
        
    if request.method == 'POST':
        account = request.form.get('account', '').strip()
        password = request.form.get('password', '')
        
        if not account or not password:
            flash('请输入账号和密码！', 'danger')
            return redirect(url_for('login'))
            
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM users WHERE username = ? OR email = ?', (account, account))
        user = cursor.fetchone()
        conn.close()
        
        if user and check_password_hash(user['password'], password):
            if user['is_active'] == 0:
                flash('您的账号已被禁用，请联系管理员！', 'danger')
                return redirect(url_for('login'))
                
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['nickname'] = user['nickname']
            session['theme'] = user['theme']
            session['is_admin'] = user['is_admin']
            
            if user['is_admin'] == 1:
                return redirect(url_for('admin_dashboard'))
            return redirect(url_for('chat'))
        else:
            flash('账号或密码错误！', 'danger')
            return redirect(url_for('login'))
            
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('您已成功退出登录。', 'info')
    return redirect(url_for('login'))

@app.route('/profile', methods=['GET', 'POST'])
def profile():
    if 'user_id' not in session:
        return redirect(url_for('login'))
        
    conn = get_db()
    cursor = conn.cursor()
    
    if request.method == 'POST':
        nickname = request.form.get('nickname', '').strip()
        theme = request.form.get('theme', 'warm')
        avatar_file = request.files.get('avatar')
        avatar_url = None
        
        if avatar_file and avatar_file.filename != '':
            if allowed_file(avatar_file.filename):
                filename = f"user_{session['user_id']}_{secure_filename(avatar_file.filename)}"
                file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                avatar_file.save(file_path)
                avatar_url = url_for('uploaded_file', filename=filename)
            else:
                flash('不支持的文件格式！仅允许 png, jpg, jpeg, gif', 'danger')
                
        if nickname:
            if avatar_url:
                cursor.execute(
                    'UPDATE users SET nickname = ?, theme = ?, avatar = ? WHERE id = ?',
                    (nickname, theme, avatar_url, session['user_id'])
                )
            else:
                cursor.execute(
                    'UPDATE users SET nickname = ?, theme = ? WHERE id = ?',
                    (nickname, theme, session['user_id'])
                )
            conn.commit()
            session['nickname'] = nickname
            session['theme'] = theme
            flash('个人资料更新成功！', 'success')
        else:
            flash('昵称不能为空！', 'danger')
            
    cursor.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],))
    user = cursor.fetchone()
    
    # 捞出该学生定制的数字分身列表
    cursor.execute('SELECT * FROM ai_characters WHERE user_id = ? ORDER BY id DESC', (session['user_id'],))
    custom_roles = cursor.fetchall()
    conn.close()
    
    return render_template('profile.html', user=user, custom_roles=custom_roles)

@app.route('/profile/character/save', methods=['POST'])
def profile_character_save():
    if 'user_id' not in session:
        return redirect(url_for('login'))
        
    char_id = request.form.get('id')
    name = request.form.get('name', '').strip()
    gender = request.form.get('gender', 'female')
    voice = request.form.get('voice', 'zh-CN-XiaoxiaoNeural')
    style_type = request.form.get('style_type', '温柔倾听')
    custom_desc = request.form.get('custom_desc', '').strip()
    
    # 结合选择的说话风格和人设设定描述组装底层 System Prompt
    prompt = f"你叫{name}，是用户打造的专属陪伴数字分身。说话风格是【{style_type}】。人设背景是：{custom_desc}。"
    
    try:
        temperature = float(request.form.get('temperature', 0.7))
    except ValueError:
        temperature = 0.7
        
    if not name or not custom_desc:
        flash('分身名称和人设背景设定均为必填项！', 'danger')
        return redirect(url_for('profile'))
        
    conn = get_db()
    cursor = conn.cursor()
    
    if char_id:
        # 修改已有数字分身，限制 user_id 防止越权修改
        cursor.execute('''
            UPDATE ai_characters 
            SET name = ?, gender = ?, voice = ?, prompt = ?, temperature = ? 
            WHERE id = ? AND user_id = ?
        ''', (name, gender, voice, prompt, temperature, char_id, session['user_id']))
        flash('数字分身修改成功！', 'success')
    else:
        # 自动生成独立的 role_key 标识
        role_key = f"custom_{session['user_id']}_{uuid.uuid4().hex[:8]}"
        cursor.execute('''
            INSERT INTO ai_characters (role_key, name, gender, voice, prompt, temperature, max_words, user_id)
            VALUES (?, ?, ?, ?, ?, ?, 100, ?)
        ''', (role_key, name, gender, voice, prompt, temperature, session['user_id']))
        flash('数字分身配置成功！快去聊天室体验专属对话吧！', 'success')
        
    conn.commit()
    conn.close()
    return redirect(url_for('profile'))

@app.route('/profile/character/delete/<int:char_id>', methods=['POST'])
def profile_character_delete(char_id):
    if 'user_id' not in session:
        return jsonify({'error': '未授权登录'}), 401
        
    conn = get_db()
    cursor = conn.cursor()
    # 只能删除当前学生创建的数字分身，防止越权漏洞
    cursor.execute('DELETE FROM ai_characters WHERE id = ? AND user_id = ?', (char_id, session['user_id']))
    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})

# ==========================================
# 语音陪伴与情绪多媒体推送
# ==========================================

@app.route('/chat')
def chat():
    if 'user_id' not in session:
        return redirect(url_for('login'))
        
    conn = get_db()
    cursor = conn.cursor()
    
    # 查询所有系统公共角色，以及当前登录用户自己创建的数字分身角色
    cursor.execute('SELECT * FROM ai_characters WHERE user_id IS NULL OR user_id = ?', (session['user_id'],))
    characters_list = cursor.fetchall()
    characters_dict = {row['role_key']: dict(row) for row in characters_list}
    
    role_id = request.args.get('role', 'warm_listener')
    if role_id not in characters_dict:
        role_id = list(characters_dict.keys())[0] if characters_dict else 'warm_listener'
        
    role_info = characters_dict.get(role_id, {
        'name': '小助手', 'gender': 'female', 'voice': 'zh-CN-XiaoxiaoNeural'
    })
    
    keyword = request.args.get('q', '').strip()
    
    if keyword:
        cursor.execute('''
            SELECT *, SUBSTR(created_at, 1, 10) as chat_date 
            FROM chat_history 
            WHERE user_id = ? AND content LIKE ? 
            ORDER BY id DESC
        ''', (session['user_id'], f'%{keyword}%'))
    else:
        cursor.execute('''
            SELECT *, SUBSTR(created_at, 1, 10) as chat_date 
            FROM chat_history 
            WHERE user_id = ? 
            ORDER BY id DESC
        ''', (session['user_id'],))
        
    history_records = cursor.fetchall()
    
    grouped_history = {}
    today_str = datetime.now().strftime('%Y-%m-%d')
    for row in history_records:
        date_str = row['chat_date']
        group_name = '今天' if date_str == today_str else date_str
        if group_name not in grouped_history:
            grouped_history[group_name] = []
        grouped_history[group_name].append(row)
        
    cursor.execute('''
        SELECT * FROM chat_history 
        WHERE user_id = ? AND role_id = ? 
        ORDER BY id ASC LIMIT 30
    ''', (session['user_id'], role_id))
    active_chat = cursor.fetchall()
    
    cursor.execute('SELECT COUNT(id) FROM mailbox WHERE user_id = ? AND is_read = 0', (session['user_id'],))
    unread_count = cursor.fetchone()[0]
    session['unread_letters'] = unread_count
    
    cursor.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],))
    user = cursor.fetchone()
    conn.close()
    
    return render_template('chat.html', 
                           role_id=role_id, 
                           role_info=role_info,
                           grouped_history=grouped_history,
                           active_chat=active_chat,
                           user=user,
                           q=keyword,
                           all_roles=characters_list)

@app.route('/api/chat/send', methods=['POST'])
def api_chat_send():
    if 'user_id' not in session:
        return jsonify({'error': '未授权登录'}), 401
        
    data = request.get_json() or {}
    message_content = data.get('message', '').strip()
    role_id = data.get('role_id', 'warm_listener')
    
    if not message_content:
        return jsonify({'error': '消息内容不能为空'}), 400
        
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('SELECT * FROM ai_characters WHERE role_key = ?', (role_id,))
    role_config = cursor.fetchone()
    
    if not role_config:
        conn.close()
        return jsonify({'error': '陪伴角色不存在'}), 400
        
    message_content = filter_sensitive_words(message_content)
    user_id = session['user_id']
    local_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    # 1. 保存用户的消息
    cursor.execute(
        'INSERT INTO chat_history (user_id, role_id, sender, content, created_at) VALUES (?, ?, ?, ?, ?)',
        (user_id, role_id, 'user', message_content, local_time)
    )
    conn.commit()
    
    # 2. 检索对话上下文
    cursor.execute(
        'SELECT sender, content FROM chat_history WHERE user_id = ? AND role_id = ? ORDER BY id DESC LIMIT 6',
        (user_id, role_id)
    )
    history_rows = cursor.fetchall()
    history_rows.reverse()
    
    prompt_with_limit = f"{role_config['prompt']}\n注意：回复必须控制在{role_config['max_words']}字以内。"
    
    api_messages = [{"role": "system", "content": prompt_with_limit}]
    for row in history_rows:
        role_type = "user" if row['sender'] == 'user' else "assistant"
        api_messages.append({"role": role_type, "content": row['content']})
        
    # 3. 调用 DeepSeek
    api_key = get_api_key()
    ai_response_text = "抱歉，我今天有点累，能稍后再聊吗？"
    try:
        client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
        completion = client.chat.completions.create(
            model="deepseek-v4-flash",
            messages=api_messages,
            temperature=role_config['temperature'],
            stream=False
        )
        ai_response_text = completion.choices[0].message.content
    except Exception as e:
        print(f"大模型 API 调用失败: {e}")
        
    # 4. 生成 TTS
    audio_filename = f"tts_{uuid.uuid4().hex}.mp3"
    audio_url = generate_tts_sync(ai_response_text, role_config['voice'], audio_filename)
    
    # 5. 情绪多媒体推荐推送机制
    media_push = None
    msg_lower = message_content.lower()
    if any(k in msg_lower for k in ["失眠", "睡不着", "做噩梦", "闭不上眼"]):
        media_push = {
            'title': '【舒眠白噪音】深层催眠白噪音与雨声',
            'url': 'https://y.qq.com/n/ryqq/player',
            'icon': 'bi-cloud-rain-heavy-fill',
            'description': '系统检测到您处于失眠状态，为您推荐治愈系白噪音。'
        }
    elif any(k in msg_lower for k in ["期末", "考试", "备考", "挂科", "复习"]):
        media_push = {
            'title': '【沉浸自习室】大森林雨声自习伴读 2H',
            'url': 'https://www.bilibili.com/video/BV11S4y1U71U',
            'icon': 'bi-book-half',
            'description': '系统检测到您正值期末备考高压，为您推荐自习专注白噪音。'
        }
    elif any(k in msg_lower for k in ["焦虑", "紧张", "难过", "崩溃", "伤心", "压力"]):
        media_push = {
            'title': '【愈疗纯音乐】网易云解压纯音乐电台',
            'url': 'https://music.163.com/#/discover/playlist',
            'icon': 'bi-music-note-beamed',
            'description': '系统检测到您当前存在些许焦虑情绪，推荐聆听舒缓纯音乐。'
        }

    # 6. 保存 AI 回复
    cursor.execute(
        'INSERT INTO chat_history (user_id, role_id, sender, content, audio_url, created_at) VALUES (?, ?, ?, ?, ?, ?)',
        (user_id, role_id, 'ai', ai_response_text, audio_url, local_time)
    )
    conn.commit()
    conn.close()
    
    return jsonify({
        'reply': ai_response_text,
        'audio_url': audio_url,
        'created_at': local_time,
        'media_push': media_push
    })

# ==========================================
# 树洞广场与收件箱模块
# ==========================================

@app.route('/treehole')
def treehole():
    if 'user_id' not in session:
        return redirect(url_for('login'))
        
    category = request.args.get('cat', '全部')
    conn = get_db()
    cursor = conn.cursor()
    
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    cursor.execute('''
        SELECT * FROM announcements 
        WHERE expire_time >= ? 
        ORDER BY is_pinned DESC, id DESC
    ''', (now_str,))
    active_announcements = cursor.fetchall()
    
    if category != '全部':
        cursor.execute('''
            SELECT * FROM treehole_posts 
            WHERE status = "published" AND category = ? 
            ORDER BY id DESC
        ''', (category,))
    else:
        cursor.execute('''
            SELECT * FROM treehole_posts 
            WHERE status = "published" 
            ORDER BY id DESC
        ''')
    posts = cursor.fetchall()
    
    cursor.execute('''
        SELECT * FROM treehole_posts 
        WHERE user_id = ? AND status = "draft" 
        ORDER BY id DESC
    ''', (session['user_id'],))
    drafts = cursor.fetchall()
    
    cursor.execute('SELECT COUNT(id) FROM mailbox WHERE user_id = ? AND is_read = 0', (session['user_id'],))
    unread_count = cursor.fetchone()[0]
    session['unread_letters'] = unread_count
    conn.close()
    
    return render_template('treehole.html', 
                           posts=posts, 
                           drafts=drafts, 
                           current_cat=category,
                           announcements=active_announcements)

@app.route('/api/treehole/save_draft', methods=['POST'])
def api_treehole_save_draft():
    if 'user_id' not in session:
        return jsonify({'error': '未授权登录'}), 401
        
    data = request.get_json() or {}
    draft_id = data.get('draft_id')
    category = data.get('category', '吐槽树洞')
    content = data.get('content', '').strip()
    
    if not content:
        return jsonify({'message': '无内容不保存'})
        
    content = filter_sensitive_words(content)
    conn = get_db()
    cursor = conn.cursor()
    local_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    if draft_id:
        cursor.execute(
            'UPDATE treehole_posts SET category = ?, content = ?, created_at = ? WHERE id = ? AND user_id = ?',
            (category, content, local_time, draft_id, session['user_id'])
        )
        saved_id = draft_id
    else:
        cursor.execute(
            'INSERT INTO treehole_posts (user_id, nickname, category, content, status, created_at) VALUES (?, ?, ?, ?, ?, ?)',
            (session['user_id'], '匿名用户', category, content, 'draft', local_time)
        )
        saved_id = cursor.lastrowid
        
    conn.commit()
    conn.close()
    return jsonify({'status': 'success', 'draft_id': saved_id, 'saved_at': local_time})

@app.route('/api/treehole/publish', methods=['POST'])
def api_treehole_publish():
    if 'user_id' not in session:
        return jsonify({'error': '未授权登录'}), 401
        
    data = request.get_json() or {}
    category = data.get('category', '吐槽树洞')
    content = data.get('content', '').strip()
    anonymous = data.get('anonymous', 'true')
    draft_id = data.get('draft_id')
    
    if not content:
        return jsonify({'error': '内容不能为空'}), 400
        
    content = filter_sensitive_words(content)
    nickname = '匿名同学'
    if anonymous == 'false':
        nickname = session.get('nickname', '同学')
        
    conn = get_db()
    cursor = conn.cursor()
    local_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    if draft_id:
        cursor.execute(
            'UPDATE treehole_posts SET nickname = ?, category = ?, content = ?, status = "published", created_at = ? WHERE id = ? AND user_id = ?',
            (nickname, category, content, local_time, draft_id, session['user_id'])
        )
    else:
        cursor.execute(
            'INSERT INTO treehole_posts (user_id, nickname, category, content, status, created_at) VALUES (?, ?, ?, ?, ?, ?)',
            (session['user_id'], nickname, category, content, 'published', local_time)
        )
    conn.commit()
    conn.close()
    
    if any(k in content for k in NEGATIVE_EMOTION_KEYWORDS):
        async_generate_ai_letter(session['user_id'], content)
        
    return jsonify({'status': 'success'})

@app.route('/api/treehole/report/<int:post_id>', methods=['POST'])
def api_treehole_report(post_id):
    if 'user_id' not in session:
        return jsonify({'error': '未登录'}), 401
        
    data = request.get_json() or {}
    reason = data.get('reason', '违规言论或恶意刷屏').strip()
    reporter = session.get('username', '匿名同学')
    
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE treehole_posts 
        SET is_reported = 1, report_reason = ?, reporter_username = ? 
        WHERE id = ?
    ''', (reason, reporter, post_id))
    conn.commit()
    conn.close()
    return jsonify({'status': 'success', 'message': '举报已提交，管理员将尽快在后台仲裁处理。'})

@app.route('/api/treehole/like/<int:post_id>', methods=['POST'])
def api_treehole_like(post_id):
    if 'user_id' not in session:
        return jsonify({'error': '未登录'}), 401
    conn = get_db()
    cursor = conn.cursor()
    # 暖心贴贴点赞数递增
    cursor.execute('UPDATE treehole_posts SET likes = COALESCE(likes, 0) + 1 WHERE id = ?', (post_id,))
    conn.commit()
    cursor.execute('SELECT likes FROM treehole_posts WHERE id = ?', (post_id,))
    likes = cursor.fetchone()[0]
    conn.close()
    return jsonify({'status': 'success', 'likes': likes})

@app.route('/api/healing-quote')
def api_healing_quote():
    import random
    quotes = [
        "无论今天过得如何，都不要忘记给自己一个微笑，明天又是全新的一天。",
        "慢慢来，比较快。有些路虽然漫长，但每前进一步都是成长。",
        "你比自己想象的要更加勇敢，也更加优秀。给自己一点信心吧！",
        "累了就停下来歇一歇，解忧杂货铺永远是你的避风港。",
        "愿你被这世界温柔以待；如果还没有，那就先温柔地对待自己。",
        "每一次挫折或失败，都是命运在提醒你换个角度去发现新方向。",
        "孤独并不是坏事，那是你与自己内心对话的最佳时机。",
        "不要为昨天的遗憾而叹息，因为今天的阳光依然温暖而明亮。",
        "生活不必完美，只要我们心存希望，日子就能闪闪发光。",
        "顺其自然，尽心尽力，其余的交给时间。你已经做得很好了。"
    ]
    return jsonify({'quote': random.choice(quotes)})

@app.route('/api/emotion/report')
def api_emotion_report():
    if 'user_id' not in session:
        return jsonify({'error': '未授权登录'}), 401
        
    conn = get_db()
    cursor = conn.cursor()
    
    # 提取最近10条聊天的文字
    cursor.execute('SELECT content FROM chat_history WHERE user_id = ? AND sender = "user" ORDER BY id DESC LIMIT 10', (session['user_id'],))
    chats = [row['content'] for row in cursor.fetchall()]
    
    # 提取最近5条树洞的文字
    cursor.execute('SELECT content FROM treehole_posts WHERE user_id = ? ORDER BY id DESC LIMIT 5', (session['user_id'],))
    posts = [row['content'] for row in cursor.fetchall()]
    conn.close()
    
    text_to_analyze = "\n".join(chats + posts).strip()
    
    # 静态兜底数据
    scores = {"study": 60, "social": 70, "self": 65, "mood": 75, "stability": 80}
    advice = "你最近还没怎么发言哦，快去和陪伴智能体聊聊天，或者发个树洞倾诉一下吧，这样杂货铺店员才能为你生成专属的心理情绪周报。"
    
    if text_to_analyze:
        prompt = (
            f"请分析该同学最近的内心倾诉内容：\n"
            f"『{text_to_analyze}』\n\n"
            f"请严格按以下 JSON 格式返回分析结果（不要包含任何 markdown 块或多余字符）：\n"
            f'{{"study": 学业压力值(10-100的整数), "social": 人际交往压力值(10-100的整数), '
            f'"self": 自我认同感分数(10-100的整数), "mood": 积极情绪占比(10-100的整数), '
            f'"stability": 情绪稳定性(10-100的整数), "advice": "给他的温馨周报建议(100字左右)"}}'
        )
        try:
            client = OpenAI(api_key=get_api_key(), base_url="https://api.deepseek.com")
            completion = client.chat.completions.create(
                model="deepseek-v4-flash",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                stream=False
            )
            res_text = completion.choices[0].message.content.strip()
            if res_text.startswith("```"):
                res_text = res_text.split("```")[1]
                if res_text.startswith("json"):
                    res_text = res_text[4:]
            import json
            data = json.loads(res_text.strip())
            scores = {
                "study": int(data.get("study", 60)),
                "social": int(data.get("social", 70)),
                "self": int(data.get("self", 65)),
                "mood": int(data.get("mood", 75)),
                "stability": int(data.get("stability", 80))
            }
            advice = data.get("advice", advice)
        except Exception as e:
            print(f"AI情绪周报生成失败: {e}")
            import random
            scores = {
                "study": random.randint(50, 80),
                "social": random.randint(55, 85),
                "self": random.randint(60, 90),
                "mood": random.randint(60, 85),
                "stability": random.randint(65, 90)
            }
            advice = "根据你的零星发言，杂货铺店员感到你近期可能有一些隐匿的学业压力或情感起伏，建议平时多听一些舒缓音乐，多和AI陪伴角色倾诉，解忧杂货铺随时守护你。"
            
    return jsonify({'scores': scores, 'advice': advice})

@app.route('/mailbox')
def mailbox():
    if 'user_id' not in session:
        return redirect(url_for('login'))
        
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM mailbox WHERE user_id = ? ORDER BY id DESC', (session['user_id'],))
    letters = cursor.fetchall()
    
    cursor.execute('UPDATE mailbox SET is_read = 1 WHERE user_id = ?', (session['user_id'],))
    conn.commit()
    
    cursor.execute('SELECT COUNT(id) FROM mailbox WHERE user_id = ? AND is_read = 0', (session['user_id'],))
    unread_count = cursor.fetchone()[0]
    session['unread_letters'] = unread_count
    conn.close()
    
    return render_template('mailbox.html', letters=letters)

# ==========================================
# 管理员端接口与路由
# ==========================================

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if 'user_id' in session and session.get('is_admin') == 1:
        return redirect(url_for('admin_dashboard'))
        
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        
        if not username or not password:
            flash('请输入账号和密码！', 'danger')
            return redirect(url_for('admin_login'))
            
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM users WHERE username = ? AND is_admin = 1', (username,))
        user = cursor.fetchone()
        conn.close()
        
        if user and check_password_hash(user['password'], password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['nickname'] = user['nickname']
            session['theme'] = user['theme']
            session['is_admin'] = 1
            return redirect(url_for('admin_dashboard'))
        else:
            flash('管理员账号或密码错误！', 'danger')
            return redirect(url_for('admin_login'))
            
    return render_template('admin_login.html')

@app.route('/admin/dashboard')
@admin_required
def admin_dashboard():
    """管理后台首页：可视化解忧数据看板"""
    conn = get_db()
    cursor = conn.cursor()
    
    # 1. 累计服务人次 (学生端消息总数)
    cursor.execute("SELECT COUNT(id) FROM chat_history WHERE sender = 'user'")
    total_services = cursor.fetchone()[0]
    
    # 2. 当日活跃用户数 (今日去重的聊天学生数)
    today_date = datetime.now().strftime('%Y-%m-%d')
    cursor.execute("SELECT COUNT(DISTINCT user_id) FROM chat_history WHERE created_at LIKE ?", (f"{today_date}%",))
    active_users_today = cursor.fetchone()[0]
    
    # 3. 双向语音通话总时长 (计算生成的音频消息总数 * 5 秒)
    cursor.execute("SELECT COUNT(id) FROM chat_history WHERE audio_url IS NOT NULL")
    audio_count = cursor.fetchone()[0]
    total_seconds = audio_count * 5
    duration_str = f"{total_seconds // 60}分{total_seconds % 60}秒"
    
    # 4. 树洞板块数据统计 (情感交流 / 学业压力 / 人际关系 / 吐槽树洞 的比例，作为整体情绪倾向)
    cursor.execute("SELECT category, content FROM treehole_posts WHERE status = 'published'")
    posts = cursor.fetchall()
    
    cat_counts = {"情感交流": 0, "学业压力": 0, "人际关系": 0, "吐槽树洞": 0}
    # 预设热词统计
    keywords = ["考试", "焦虑", "压力", "失眠", "开心", "难过"]
    keyword_counts = {k: 0 for k in keywords}
    
    for post in posts:
        cat = post['category']
        content = post['content']
        if cat in cat_counts:
            cat_counts[cat] += 1
        for kw in keywords:
            keyword_counts[kw] += content.count(kw)
            
    # 热词柱状图数据
    bar_x = list(keyword_counts.keys())
    bar_y = list(keyword_counts.values())
    
    # 整体情绪饼图数据 (映射板块比例)
    pie_data = [{"name": name, "value": val} for name, val in cat_counts.items()]
    
    conn.close()
    return render_template('admin_dashboard.html',
                           total_services=total_services,
                           active_users_today=active_users_today,
                           duration_str=duration_str,
                           bar_x=bar_x,
                           bar_y=bar_y,
                           pie_data=pie_data)

@app.route('/admin/users')
@admin_required
def admin_users():
    status_filter = request.args.get('status', 'all')
    conn = get_db()
    cursor = conn.cursor()
    if status_filter == 'active':
        cursor.execute('SELECT * FROM users WHERE is_admin = 0 AND is_active = 1 ORDER BY id DESC')
    elif status_filter == 'disabled':
        cursor.execute('SELECT * FROM users WHERE is_admin = 0 AND is_active = 0 ORDER BY id DESC')
    else:
        cursor.execute('SELECT * FROM users WHERE is_admin = 0 ORDER BY id DESC')
    students = cursor.fetchall()
    conn.close()
    return render_template('admin_users.html', students=students, current_filter=status_filter)

@app.route('/admin/users/toggle/<int:student_id>', methods=['POST'])
@admin_required
def admin_users_toggle(student_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM users WHERE id = ?', (student_id,))
    student = cursor.fetchone()
    
    if not student:
        conn.close()
        return jsonify({'error': '用户不存在'}), 404
        
    new_status = 0 if student['is_active'] == 1 else 1
    cursor.execute('UPDATE users SET is_active = ? WHERE id = ?', (new_status, student_id))
    conn.commit()
    conn.close()
    
    status_text = "禁用" if new_status == 0 else "启用"
    log_admin_action(
        action_type=f"{status_text.upper()}_USER",
        target=student['username'],
        details=f"管理员将学生 {student['username']} (ID: {student_id}) 的账号状态设置为: {status_text}"
    )
    return jsonify({'status': 'success', 'new_active': new_status})

@app.route('/admin/users/reset_pwd/<int:student_id>', methods=['POST'])
@admin_required
def admin_users_reset_pwd(student_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM users WHERE id = ?', (student_id,))
    student = cursor.fetchone()
    
    if not student:
        conn.close()
        return jsonify({'error': '用户不存在'}), 404
        
    hashed_pwd = generate_password_hash("123456")
    cursor.execute('UPDATE users SET password = ? WHERE id = ?', (hashed_pwd, student_id))
    conn.commit()
    conn.close()
    
    log_admin_action(
        action_type="RESET_PWD",
        target=student['username'],
        details=f"管理员重置了学生 {student['username']} (ID: {student_id}) 的登录密码为默认密码 (123456)"
    )
    return jsonify({'status': 'success', 'message': '密码已成功重置为：123456'})

@app.route('/admin/logs')
@admin_required
def admin_logs():
    action_filter = request.args.get('action', 'all')
    keyword = request.args.get('q', '').strip()
    
    conn = get_db()
    cursor = conn.cursor()
    query = 'SELECT * FROM admin_logs WHERE 1=1'
    params = []
    if action_filter != 'all':
        query += ' AND action_type = ?'
        params.append(action_filter)
    if keyword:
        query += ' AND (admin_username LIKE ? OR target LIKE ? OR details LIKE ?)'
        params.extend([f'%{keyword}%', f'%{keyword}%', f'%{keyword}%'])
    query += ' ORDER BY id DESC'
    cursor.execute(query, params)
    logs = cursor.fetchall()
    
    cursor.execute('SELECT DISTINCT action_type FROM admin_logs')
    action_types = [row['action_type'] for row in cursor.fetchall()]
    conn.close()
    return render_template('admin_logs.html', logs=logs, action_types=action_types, current_action=action_filter, q=keyword)

@app.route('/admin/logs/export')
@admin_required
def admin_logs_export():
    action_filter = request.args.get('action', 'all')
    keyword = request.args.get('q', '').strip()
    
    conn = get_db()
    cursor = conn.cursor()
    query = 'SELECT * FROM admin_logs WHERE 1=1'
    params = []
    if action_filter != 'all':
        query += ' AND action_type = ?'
        params.append(action_filter)
    if keyword:
        query += ' AND (admin_username LIKE ? OR target LIKE ? OR details LIKE ?)'
        params.extend([f'%{keyword}%', f'%{keyword}%', f'%{keyword}%'])
    query += ' ORDER BY id DESC'
    cursor.execute(query, params)
    logs = cursor.fetchall()
    conn.close()
    
    si = StringIO()
    cw = csv.writer(si)
    cw.writerow(['日志ID', '管理员', '操作类型', '目标对象', '详细说明', '操作时间'])
    for log in logs:
        cw.writerow([log['id'], log['admin_username'], log['action_type'], log['target'], log['details'], log['created_at']])
    response = make_response(si.getvalue().encode('utf-8-sig'))
    response.headers["Content-Disposition"] = f"attachment; filename=admin_audit_logs_{datetime.now().strftime('%Y%m%d%H%M%S')}.csv"
    response.headers["Content-type"] = "text/csv"
    return response

@app.route('/admin/characters')
@admin_required
def admin_characters():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM ai_characters ORDER BY id ASC')
    roles = cursor.fetchall()
    conn.close()
    return render_template('admin_characters.html', roles=roles)

@app.route('/admin/characters/save', methods=['POST'])
@admin_required
def admin_characters_save():
    char_id = request.form.get('id')
    role_key = request.form.get('role_key', '').strip()
    name = request.form.get('name', '').strip()
    gender = request.form.get('gender', 'female')
    voice = request.form.get('voice', 'zh-CN-XiaoxiaoNeural')
    prompt = request.form.get('prompt', '').strip()

    # 如果是编辑角色且前端没有传 role_key (例如被 disabled 了)，则从数据库查出已有的 role_key
    if char_id and not role_key:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT role_key FROM ai_characters WHERE id = ?', (char_id,))
        row = cursor.fetchone()
        if row:
            role_key = row['role_key']
        conn.close()

    try:
        temperature = float(request.form.get('temperature', 0.7))
    except ValueError:
        temperature = 0.7
    try:
        max_words = int(request.form.get('max_words', 80))
    except ValueError:
        max_words = 80

    if not role_key or not name or not prompt:
        flash('标识键、名称、以及提示词 Prompt 均为必填项！', 'danger')
        return redirect(url_for('admin_characters'))

    conn = get_db()
    cursor = conn.cursor()
    if char_id:
        cursor.execute('''
            UPDATE ai_characters 
            SET name = ?, gender = ?, voice = ?, prompt = ?, temperature = ?, max_words = ? 
            WHERE id = ?
        ''', (name, gender, voice, prompt, temperature, max_words, char_id))
        action = "EDIT_CHARACTER"
        details = f"管理员修改了智能体角色 {name} ({role_key})。温和度: {temperature}, 字数限制: {max_words}"
    else:
        cursor.execute('SELECT id FROM ai_characters WHERE role_key = ?', (role_key,))
        if cursor.fetchone():
            flash(f'角色标识键 "{role_key}" 已存在，不可重复创建！', 'danger')
            conn.close()
            return redirect(url_for('admin_characters'))
        cursor.execute('''
            INSERT INTO ai_characters (role_key, name, gender, voice, prompt, temperature, max_words)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (role_key, name, gender, voice, prompt, temperature, max_words))
        action = "ADD_CHARACTER"
        details = f"管理员新建了智能体角色 {name} ({role_key})。分配音色: {voice}, 字数限制: {max_words}"
    conn.commit()
    conn.close()
    
    log_admin_action(action, role_key, details)
    flash('智能体陪伴角色保存成功！', 'success')
    return redirect(url_for('admin_characters'))

@app.route('/admin/characters/delete/<int:char_id>', methods=['POST'])
@admin_required
def admin_characters_delete(char_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM ai_characters WHERE id = ?', (char_id,))
    role = cursor.fetchone()
    if not role:
        conn.close()
        return jsonify({'error': '陪伴角色不存在'}), 404
    cursor.execute('DELETE FROM ai_characters WHERE id = ?', (char_id,))
    conn.commit()
    conn.close()
    log_admin_action("DELETE_CHARACTER", role['role_key'], f"管理员删除了智能体角色 {role['name']} (标识键: {role['role_key']})")
    return jsonify({'status': 'success'})

# ==========================================
# 新增管理员端：树洞审核与安全治理
# ==========================================

@app.route('/admin/posts')
@admin_required
def admin_posts():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT word FROM sensitive_words')
    sensitive_words_list = [row['word'] for row in cursor.fetchall()]
    cursor.execute('SELECT * FROM treehole_posts WHERE status != "draft" ORDER BY id DESC')
    all_posts = cursor.fetchall()
    cursor.execute('SELECT * FROM treehole_posts WHERE is_reported = 1 AND status = "published" ORDER BY id DESC')
    reported_posts = cursor.fetchall()
    conn.close()
    return render_template('admin_posts.html', posts=all_posts, reported_posts=reported_posts, sensitive_words=sensitive_words_list)

@app.route('/admin/posts/status/<int:post_id>', methods=['POST'])
@admin_required
def admin_posts_status(post_id):
    action = request.form.get('action', 'hide')
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM treehole_posts WHERE id = ?', (post_id,))
    post = cursor.fetchone()
    if not post:
        conn.close()
        return jsonify({'error': '帖子不存在'}), 404
    if action == 'hide':
        cursor.execute('UPDATE treehole_posts SET status = "hidden" WHERE id = ?', (post_id,))
        log_action = "HIDE_POST"
        log_details = f"管理员下架了帖子 #{post_id}，内容: {post['content'][:30]}"
    else:
        cursor.execute('UPDATE treehole_posts SET status = "published", is_reported = 0 WHERE id = ?', (post_id,))
        log_action = "RECOVER_POST"
        log_details = f"管理员恢复发布并驳回了帖子 #{post_id} 的举报，内容: {post['content'][:30]}"
    conn.commit()
    conn.close()
    log_admin_action(log_action, f"post_{post_id}", log_details)
    return jsonify({'status': 'success'})

@app.route('/admin/posts/delete/<int:post_id>', methods=['POST'])
@admin_required
def admin_posts_delete(post_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM treehole_posts WHERE id = ?', (post_id,))
    post = cursor.fetchone()
    if not post:
        conn.close()
        return jsonify({'error': '帖子不存在'}), 404
    cursor.execute('DELETE FROM treehole_posts WHERE id = ?', (post_id,))
    conn.commit()
    conn.close()
    log_admin_action("DELETE_POST", f"post_{post_id}", f"管理员彻底删除了树洞帖子 #{post_id}，发布人: {post['nickname']}")
    return jsonify({'status': 'success'})

@app.route('/admin/posts/ban_author/<int:post_id>', methods=['POST'])
@admin_required
def admin_posts_ban_author(post_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM treehole_posts WHERE id = ?', (post_id,))
    post = cursor.fetchone()
    if not post:
        conn.close()
        return jsonify({'error': '动态不存在'}), 404
    author_id = post['user_id']
    cursor.execute('SELECT username FROM users WHERE id = ?', (author_id,))
    user = cursor.fetchone()
    if not user:
        conn.close()
        return jsonify({'error': '用户不存在'}), 404
    cursor.execute('UPDATE users SET is_active = 0 WHERE id = ?', (author_id,))
    cursor.execute('UPDATE treehole_posts SET status = "hidden" WHERE user_id = ?', (author_id,))
    conn.commit()
    conn.close()
    log_admin_action("BAN_AUTHOR", user['username'], f"管理员一键封禁了违规发帖人 {user['username']} (UID: {author_id}) 的账户，并批量下架其全部动态")
    return jsonify({'status': 'success', 'message': f"已成功封禁用户 @{user['username']}，并下架其发过的所有内容。"})

# ==========================================
# 新增管理员端：敏感词库动态维护
# ==========================================

@app.route('/admin/sensitive', methods=['GET', 'POST'])
@admin_required
def admin_sensitive():
    conn = get_db()
    cursor = conn.cursor()
    if request.method == 'POST':
        word = request.form.get('word', '').strip()
        if word:
            local_now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            try:
                cursor.execute('INSERT INTO sensitive_words (word, created_at) VALUES (?, ?)', (word, local_now))
                conn.commit()
                log_admin_action("ADD_SENSITIVE_WORD", word, f"管理员新增敏感词词条: {word}")
                flash('敏感词词条添加成功！', 'success')
            except sqlite3.IntegrityError:
                flash(f'敏感词 "{word}" 已在词库中，请勿重复添加。', 'danger')
        else:
            flash('敏感词不能为空！', 'danger')
    cursor.execute('SELECT * FROM sensitive_words ORDER BY id DESC')
    words = cursor.fetchall()
    conn.close()
    return render_template('admin_sensitive.html', words=words)

@app.route('/admin/sensitive/delete/<int:word_id>', methods=['POST'])
@admin_required
def admin_sensitive_delete(word_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT word FROM sensitive_words WHERE id = ?', (word_id,))
    word_row = cursor.fetchone()
    if not word_row:
        conn.close()
        return jsonify({'error': '词条不存在'}), 404
    cursor.execute('DELETE FROM sensitive_words WHERE id = ?', (word_id,))
    conn.commit()
    conn.close()
    log_admin_action("DELETE_SENSITIVE_WORD", word_row['word'], f"管理员删除了敏感词词条: {word_row['word']}")
    return jsonify({'status': 'success'})

# ==========================================
# 新增管理员端：校园公告通知发布
# ==========================================

@app.route('/admin/announcements')
@admin_required
def admin_announcements():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM announcements ORDER BY is_pinned DESC, id DESC')
    ann_list = cursor.fetchall()
    conn.close()
    return render_template('admin_announcements.html', announcements=ann_list)

@app.route('/admin/announcements/save', methods=['POST'])
@admin_required
def admin_announcements_save():
    ann_id = request.form.get('id')
    title = request.form.get('title', '').strip()
    content = request.form.get('content', '').strip()
    is_pinned = 1 if request.form.get('is_pinned') == 'on' else 0
    expire_time = request.form.get('expire_time', '').strip()
    
    cover_file = request.files.get('cover_image')
    cover_image_url = None
    
    if cover_file and cover_file.filename != '':
        if allowed_file(cover_file.filename):
            filename = f"banner_{uuid.uuid4().hex}_{secure_filename(cover_file.filename)}"
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            cover_file.save(file_path)
            cover_image_url = url_for('uploaded_file', filename=filename)
        else:
            flash('上传的封面图格式不支持！仅允许 png, jpg, jpeg, gif', 'danger')
            return redirect(url_for('admin_announcements'))

    if not title or not content or not expire_time:
        flash('标题、内容和下线时间均为必填项！', 'danger')
        return redirect(url_for('admin_announcements'))
        
    conn = get_db()
    cursor = conn.cursor()
    local_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    if ann_id:
        if cover_image_url:
            cursor.execute('''
                UPDATE announcements 
                SET title = ?, content = ?, is_pinned = ?, expire_time = ?, cover_image = ? 
                WHERE id = ?
            ''', (title, content, is_pinned, expire_time, cover_image_url, ann_id))
        else:
            cursor.execute('''
                UPDATE announcements 
                SET title = ?, content = ?, is_pinned = ?, expire_time = ? 
                WHERE id = ?
            ''', (title, content, is_pinned, expire_time, ann_id))
        action = "EDIT_ANNOUNCEMENT"
        details = f"管理员修改了校园通知《{title}》 (ID: {ann_id})。置顶状态: {is_pinned}"
    else:
        if not cover_image_url:
            cover_image_url = '/static/css/default-banner.jpg'
        cursor.execute('''
            INSERT INTO announcements (title, content, is_pinned, expire_time, cover_image, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (title, content, is_pinned, expire_time, cover_image_url, local_time))
        action = "ADD_ANNOUNCEMENT"
        details = f"管理员新发布了校园公告通知《{title}》。置顶状态: {is_pinned}"
        
    conn.commit()
    conn.close()
    
    log_admin_action(action, title, details)
    flash('通知公告保存成功！', 'success')
    return redirect(url_for('admin_announcements'))

@app.route('/admin/announcements/delete/<int:ann_id>', methods=['POST'])
@admin_required
def admin_announcements_delete(ann_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT title FROM announcements WHERE id = ?', (ann_id,))
    ann = cursor.fetchone()
    if not ann:
        conn.close()
        return jsonify({'error': '通知不存在'}), 404
    cursor.execute('DELETE FROM announcements WHERE id = ?', (ann_id,))
    conn.commit()
    conn.close()
    log_admin_action("DELETE_ANNOUNCEMENT", ann['title'], f"管理员删除了通知公告《{ann['title']}》 (ID: {ann_id})")
    return jsonify({'status': 'success'})

@app.route('/<path:filename>.map')
def ignore_map_files(filename):
    return '', 204

@app.route('/.well-known/<path:filename>')
def ignore_wellknown_files(filename):
    return '', 204

if __name__ == '__main__':
    app.run(debug=True, port=5000)

