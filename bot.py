import asyncio, logging, sqlite3, time, re, os, json, aiohttp, base64
from collections import defaultdict, deque
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, BotCommand

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OWNER_ID = int(os.getenv("OWNER_ID", "5240174256"))
BOT_VERSION = "v2.9.1"

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

chat_history = defaultdict(lambda: deque(maxlen=10))
paused_chats = {}
processed_msg_ids = deque(maxlen=300)
active_chat_locks = set()
chat_rate_limits = defaultdict(lambda: deque(maxlen=20))

MODEL_PRETTY_NAMES = {
    "deepseek/deepseek-chat": "DeepSeek V3 🐳",
    "deepseek/deepseek-r1": "DeepSeek R1 🧠",
    "google/gemini-2.0-flash-001": "Gemini 2.0 Flash ⚡️ (Мультимод.)",
    "google/gemini-pro-1.5": "Gemini 1.5 Pro 💫 (Мультимод.)",
    "meta-llama/llama-3.3-70b-instruct": "Llama 3.3 70B 🦙",
    "anthropic/claude-3.5-sonnet": "Claude 3.5 Sonnet 🎭 (Мультимод.)"
}

def get_pretty_model_name(model_id: str) -> str:
    clean_id = (model_id or "").strip().lower()
    for key, val in MODEL_PRETTY_NAMES.items():
        if clean_id == key.lower():
            return val
    if "/" in clean_id:
        return clean_id.split("/")[-1].replace("-", " ").title() + " 🤖"
    return model_id or "DeepSeek V3 🐳"

def is_multimodal_model(model_id: str) -> bool:
    mid = (model_id or "").lower()
    return any(k in mid for k in ["gemini", "gpt-4o", "claude-3", "sonnet", "vision", "qwen-vl"])

BUSINESS_INTENT_WORDS = ["реклам", "купит", "заказ", "сотруднич", "прайс", "стоимост", "бюджет", "оплат", "интеграц", "тариф", "размещен"]
SPAM_PHRASE_PATTERNS = [
    r"(?:вступ|заход|переход|присоединяй|подпиш|го\s+в|иди\s+в).*(?:флуд|чат|бесед|конф|канал|групп|паблик)",
    r"(?:взаимн(?:ая|ый|о)).*(?:подписк|пиар|фолловинг)",
    r"(?:казино|1win|ставки|схемка|темка|ворк|легкие\s+деньги|сигналы\s+крипт)"
]
GREETING_WORDS = {"привет", "здравствуйте", "здравствуй", "хай", "хеллоу", "ку", "добрый день", "добрый вечер", "доброе утро", "салам", "шо ты", "как дела", "чо как", "йоу", "hello", "hi", "hey"}

class FormStates(StatesGroup):
    waiting_for_prompt = State()
    waiting_for_custom_model = State()
    waiting_for_broadcast = State()

DIRECT_PROMPTS = {
    "ru": (
        "Ты — персональный умный Telegram-бот «Ai Assistant» с долгосрочной памятью.\n"
        "Твоя текущая языковая модель: {model_name}. Если тебя спрашивают, какая ты модель — честно называй {model_name}.\n\n"
        "ВОЗМОЖНОСТИ:\n"
        "• В этом личном чате: персональный помощник. Отвечай на любые вопросы, пиши тексты, код, помогай в проектах.\n"
        "• В бизнес-чатах: умный авто-секретарь для Telegram Business.\n\n"
        "КОМАНДЫ:\n"
        "• /new — начать новый диалог\n"
        "• /memory — посмотреть досье о себе\n"
        "• /forget — стереть память\n"
        "• /settings — панель моделей"
    ),
    "en": (
        "You are 'Ai Assistant', a smart personal Telegram bot with long-term memory.\n"
        "Your model engine is: {model_name}.\n\n"
        "COMMANDS:\n• /new — Fresh topic\n• /memory — View profile\n• /forget — Wipe facts\n• /settings — Settings"
    )
}

PRESETS_RU = {
    "strict": (
        "Ты — строгий и профессиональный ИИ-секретарь владельца этого аккаунта.\n\n"
        "1. ДЕЛОВЫЕ ЗАПРОСЫ И РЕКЛАМА (ВЫСШИЙ ПРИОРИТЕТ):\n"
        "   • Если пользователь пишет о рекламе, покупке, заказе, цене или сотрудничестве — ВСЕГДА принимай заявку, даже со смайликами:\n"
        "     «Ваш запрос принят и передан владельцу аккаунта. Он свяжется с вами при необходимости.»\n"
        "   • Если написали только приветствие без конкретики — вежливо попроси изложить суть делового вопроса в одном сообщении.\n\n"
        "2. СПАМ И ФЛУД:\n"
        "   На бессмысленный флуд, мемы или приглашения в каналы отвечай ИСКЛЮЧИТЕЛЬНО:\n"
        "   «Пожалуйста, удалите этот чат и не тратьте наше время»\n\n"
        "3. Чистый текст БЕЗ Markdown (без ** и #). Кратко (1-2 предложения)."
    ),
    "blogger": "Ты — официальный ИИ-менеджер по рекламе блогера. Запрашивай ссылку, формат, дату и бюджет списком через '• '. На спам отвечай: «Пожалуйста, удалите этот чат и не тратьте наше время». Чистый текст БЕЗ Markdown.",
    "support": "Ты — техническая поддержка. Запрашивай суть проблемы, модель устройства и текст ошибки списком через '• '. Чистый текст БЕЗ Markdown.",
    "vacation": "Ты — секретарь. Владелец аккаунта в отпуске/офлайн. Сообщи, что передашь контакты владельцу по возвращении. Чистый текст БЕЗ Markdown.",
    "consultant": "Ты — деловой ИИ-консультант владельца. Отвечай по делу. Чистый текст БЕЗ Markdown."
}

PRESETS_EN = {
    "strict": "You are a strict AI secretary. 1. Accept business/ad inquiries. 2. For spam reply 'Please delete this chat and do not waste our time.' Plain text without Markdown.",
    "blogger": "You are an Ads Manager. Ask for specs with bullets. On spam reply 'Please delete this chat and do not waste our time.' Plain text.",
    "support": "Tech Support AI. Collect issue details with bullets. Plain text.",
    "vacation": "Secretary AI. Owner is offline. Reply politely. Plain text.",
    "consultant": "Professional AI consultant. Plain text."
}

TEXTS = {
    "ru": {
        "start": "👋 <b>Привет! Я твой персональный ИИ-ассистент с долгосрочной памятью.</b>\n\n• Используй кнопки под клавиатурой для быстрого управления.\n• Для настроек моделей нажми <b>⚙️ Настройки</b>.",
        "panel_title": "⚙️ <b>Панель управления ИИ-секретарём</b> ({role})",
        "role_owner": "👑 Создатель",
        "role_user": "👤 Пользователь",
        "cur_models": "📌 <b>Модели:</b>\n• <b>В ЛС:</b> {dm_model}\n• <b>В Бизнес-чатах:</b> {biz_model}",
        "cur_preset": "🎭 <b>Режим секретаря:</b> {preset}",
        "btn_model": "🤖 Выбрать модели",
        "btn_presets": "🎭 Пресеты промпта",
        "btn_advanced": "⚙️ Доп. настройки",
        "btn_lang": "🌐 Язык (RU/EN)",
        "btn_about": "📖 О боте и справка",
        "btn_stats": "📊 Дайджест и статистика",
        "btn_users": "👥 Пользователи",
        "btn_broadcast": "📢 Рассылка",
        "btn_memory": "🧠 Память ассистента",
        "btn_back": "« Назад",
        "btn_cancel": "❌ Отмена",
        "choose_target_title": "🎯 <b>Для чего вы хотите настроить модель?</b>",
        "btn_target_dm": "💬 Для общения в ЛС",
        "btn_target_biz": "💼 Для Бизнес-чатов (Секретарь)",
        "choose_lang_title": "🌐 <b>Выберите язык интерфейса:</b>",
        "lang_set": "✅ Язык переключён на Русский",
        "model_choose_title": "Выберите модель для <b>{target}</b>:",
        "target_dm_name": "общения в ЛС",
        "target_biz_name": "бизнес-чатов",
        "model_set": "✅ Модель для {target} изменена на {model}",
        "custom_model_prompt": "✍️ <b>Введите точный ID модели с OpenRouter для {target}:</b>",
        "custom_model_set": "✅ Модель для {target} установлена: {model}",
        "presets_menu_title": "🎭 <b>Выберите готовый режим или настройте свой:</b>",
        "preset_set": "✅ Активирован режим: {name}",
        "preset_strict": "💼 Строгий секретарь",
        "preset_blogger": "📢 Менеджер рекламы",
        "preset_support": "🛠 Техподдержка",
        "preset_vacation": "🏖 В отпуске / Сплю",
        "preset_consultant": "🤝 Консультант",
        "preset_custom": "✏️ Свой промпт",
        "prompt_edit_title": "✏️ <b>Отправьте новый System Prompt сообщением:</b>",
        "prompt_set": "✅ System Prompt успешно обновлён!",
        "stats_title": "📊 <b>Дайджест и статистика:</b>\n\n📨 <b>Обработано:</b> {msg_count}\n🪙 <b>Токенов:</b> {tokens}\n🚫 <b>Спама:</b> {spam_count}\n\n📋 <b>Последние контакты:</b>\n{recent_leads}",
        "stats_no_leads": "<i>Обращений пока нет.</i>",
        "users_title": "👥 <b>Список пользователей ({count}):</b>\n\n",
        "users_empty": "👥 Пользователей пока нет.",
        "broadcast_title": "📢 <b>Рассылка</b>\n\nОтправьте текст или фото:",
        "broadcast_empty": "❌ Нет пользователей.",
        "broadcast_started": "⏳ <i>Начинаю рассылку для {count} чел...</i>",
        "broadcast_done": "✅ <b>Рассылка завершена!</b>\n\nУспешно: <b>{success}</b>\nОшибок: <b>{failed}</b>",
        "about_text": f"📖 <b>Справка по боту [{BOT_VERSION}]</b>\n\n🧠 Память через сессии\n⚡️ Live-печать\n🖼 Анализ фото и стикеров",
        "btn_summary": "📋 Выжимка",
        "summary_loading": "⏳ <i>Составляю бриф...</i>",
        "summary_empty": "❌ Нет сообщений в памяти.",
        "summary_header": "📋 <b>Карточка сделки</b>\n\n",
        "btn_mute": "🛑 Заглушить на 24ч",
        "btn_unmute": "▶️ Возобновить",
        "notify_title": "📩 <b>Новое обращение</b>\n👤 <b>От:</b> {user_info} ({username})\n\n💬 <b>Запрос:</b>\n<blockquote>{user_text}</blockquote>\n🤖 <b>Ответ ИИ:</b>\n<blockquote expandable>{ai_reply}</blockquote>",
        "ai_error": "Здравствуйте! Произошла временная ошибка, попробуйте чуть позже.",
        "memory_view": "🧠 <b>Что я помню о вас:</b>\n\n{memory}\n\n<i>Очистить память: /forget</i>",
        "memory_empty": "<i>Пока фактов не накоплено. Расскажите мне о себе!</i>",
        "memory_cleared": "🧹 <b>Память полностью очищена!</b>",
        "chat_reset_msg": "✨ <b>Контекст текущего диалога сброшен!</b>",
        "media_fallback": "Пожалуйста, изложите суть вашего вопроса текстом в одном сообщении.",
        "vision_not_supported": "⚠️ Текущая модель <b>{model}</b> не поддерживает анализ фото.\n\n👉 Выберите модель с меткой <b>(Мультимод.)</b> в <b>⚙️ Настройки</b>."
    },
    "en": {
        "start": "👋 <b>Hello! I am your AI assistant with persistent memory.</b>\n\n• Use keyboard buttons for quick controls.\n• Click <b>⚙️ Settings</b> to configure.",
        "panel_title": "⚙️ <b>Control Panel</b> ({role})",
        "role_owner": "👑 Creator",
        "role_user": "👤 User",
        "cur_models": "📌 <b>Models:</b>\n• <b>DM:</b> {dm_model}\n• <b>Business:</b> {biz_model}",
        "cur_preset": "🎭 <b>Mode:</b> {preset}",
        "btn_model": "🤖 Select Models",
        "btn_presets": "🎭 Prompt Presets",
        "btn_advanced": "⚙️ Advanced",
        "btn_lang": "🌐 Language (RU/EN)",
        "btn_about": "📖 About",
        "btn_stats": "📊 Stats",
        "btn_users": "👥 Users",
        "btn_broadcast": "📢 Broadcast",
        "btn_memory": "🧠 Memory",
        "btn_back": "« Back",
        "btn_cancel": "❌ Cancel",
        "choose_target_title": "🎯 <b>Select which model to configure:</b>",
        "btn_target_dm": "💬 For Direct Messages",
        "btn_target_biz": "💼 For Business Chats",
        "choose_lang_title": "🌐 <b>Choose language:</b>",
        "lang_set": "✅ Language set to English",
        "model_choose_title": "Select a model for <b>{target}</b>:",
        "target_dm_name": "Direct Messages",
        "target_biz_name": "Business Chats",
        "model_set": "✅ Model for {target} changed to {model}",
        "custom_model_prompt": "✍️ <b>Enter exact Model ID for {target}:</b>",
        "custom_model_set": "✅ Model for {target} set to: {model}",
        "presets_menu_title": "🎭 <b>Select a preset:</b>",
        "preset_set": "✅ Mode activated: {name}",
        "preset_strict": "💼 Strict Secretary",
        "preset_blogger": "📢 Ads Manager",
        "preset_support": "🛠 Support",
        "preset_vacation": "🏖 Vacation",
        "preset_consultant": "🤝 Consultant",
        "preset_custom": "✏️ Custom Prompt",
        "prompt_edit_title": "✏️ <b>Send new System Prompt:</b>",
        "prompt_set": "✅ System Prompt updated!",
        "stats_title": "📊 <b>Stats:</b>\n\n📨 <b>Processed:</b> {msg_count}\n🪙 <b>Tokens:</b> {tokens}\n🚫 <b>Spam:</b> {spam_count}\n\n📋 <b>Leads:</b>\n{recent_leads}",
        "stats_no_leads": "<i>No inquiries yet.</i>",
        "users_title": "👥 <b>Users ({count}):</b>\n\n",
        "users_empty": "👥 No users found.",
        "broadcast_title": "📢 <b>Broadcast Message</b>",
        "broadcast_empty": "❌ No users found.",
        "broadcast_started": "⏳ <i>Starting...</i>",
        "broadcast_done": "✅ <b>Finished!</b>",
        "about_text": f"📖 <b>Bot Guide [{BOT_VERSION}]</b>",
        "btn_summary": "📋 Summary",
        "summary_loading": "⏳ <i>Compiling brief...</i>",
        "summary_empty": "❌ No messages found.",
        "summary_header": "📋 <b>Deal Summary Card</b>\n\n",
        "btn_mute": "🛑 Mute 24h",
        "btn_unmute": "▶️ Unmute",
        "notify_title": "📩 <b>New message</b>\n👤 <b>From:</b> {user_info} ({username})\n\n💬 <b>Text:</b>\n<blockquote>{user_text}</blockquote>\n🤖 <b>Reply:</b>\n<blockquote expandable>{ai_reply}</blockquote>",
        "ai_error": "Hello! A temporary error occurred, please try again later.",
        "memory_view": "🧠 <b>Profile facts:</b>\n\n{memory}\n\n<i>Clear: /forget</i>",
        "memory_empty": "<i>No facts recorded yet.</i>",
        "memory_cleared": "🧹 <b>Memory cleared!</b>",
        "chat_reset_msg": "✨ <b>Topic context reset!</b>",
        "media_fallback": "Please state the core of your inquiry in one text message.",
        "vision_not_supported": "⚠️ Model <b>{model}</b> is text-only.\n\n👉 Pick a <b>(Мультимод.)</b> model in <b>⚙️ Settings</b>."
    }
}

def t(key: str, lang: str = "ru", **kwargs) -> str:
    lang_dict = TEXTS.get(lang, TEXTS["ru"])
    msg = lang_dict.get(key, key)
    return msg.format(**kwargs) if kwargs else msg

def format_to_tg_html(text: str) -> str:
    text = re.sub(r'(?m)^#+\s*', '', text)
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'(?<!\*)\*(?!\*)(.*?)(?<!\*)\*(?!\*)', r'<i>\1</i>', text)
    text = re.sub(r'(?m)^\s*[-*]\s*', '• ', text)
    text = re.sub(r'\[(.*?)\]\((.*?)\)', r'<a href="\2">\1</a>', text)
    return text.strip()

def strip_markdown(text: str) -> str:
    return re.sub(r'[*#_`]', '', text)

def dlp_sanitize(text: str) -> str:
    text = re.sub(r'sk-or-v1-[a-zA-Z0-9]{32,}', '[API_KEY_PROTECTED]', text)
    text = re.sub(r'sk-[a-zA-Z0-9]{20,}', '[API_KEY_PROTECTED]', text)
    text = re.sub(r'\d{8,11}:[A-Za-z0-9_-]{30,}', '[BOT_TOKEN_PROTECTED]', text)
    text = re.sub(r'\b(?:\d[ -]*?){16}\b', '[CARD_PROTECTED]', text)
    return text

def init_db():
    conn = sqlite3.connect("bot_config.db")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS user_configs 
                 (user_id INTEGER PRIMARY KEY, model TEXT, system_prompt TEXT, lang TEXT, preset TEXT, biz_model TEXT, show_disclaimer INTEGER DEFAULT 1)''')
    c.execute('''CREATE TABLE IF NOT EXISTS blacklist (owner_id INTEGER, blocked_id INTEGER, PRIMARY KEY (owner_id, blocked_id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS registered_users (user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS business_conns (conn_id TEXT PRIMARY KEY, user_id INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS stats (user_id INTEGER PRIMARY KEY, msg_count INTEGER DEFAULT 0, tokens INTEGER DEFAULT 0, spam_count INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS leads (id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER, chat_id INTEGER, user_info TEXT, username TEXT, snippet TEXT, timestamp INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_memories (user_id INTEGER PRIMARY KEY, memory_text TEXT, updated_at INTEGER)''')
    
    for col in ["username", "full_name"]:
        try:
            c.execute(f"ALTER TABLE registered_users ADD COLUMN {col} TEXT")
        except Exception:
            pass
    for col in ["lang", "preset", "biz_model"]:
        try:
            c.execute(f"ALTER TABLE user_configs ADD COLUMN {col} TEXT")
        except Exception:
            pass
    try:
        c.execute("ALTER TABLE user_configs ADD COLUMN show_disclaimer INTEGER DEFAULT 1")
    except Exception:
        pass
    conn.commit()
    conn.close()

init_db()

def get_user_memory(user_id: int) -> str:
    conn = sqlite3.connect("bot_config.db")
    c = conn.cursor()
    c.execute("SELECT memory_text FROM user_memories WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row and row[0] else ""

def save_user_memory(user_id: int, memory_text: str):
    conn = sqlite3.connect("bot_config.db")
    c = conn.cursor()
    c.execute('''INSERT INTO user_memories (user_id, memory_text, updated_at) VALUES (?, ?, ?)
                 ON CONFLICT(user_id) DO UPDATE SET memory_text = excluded.memory_text, updated_at = excluded.updated_at''',
              (user_id, memory_text.strip(), int(time.time())))
    conn.commit()
    conn.close()

def clear_user_memory(user_id: int):
    conn = sqlite3.connect("bot_config.db")
    c = conn.cursor()
    c.execute("DELETE FROM user_memories WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

async def update_memory_in_background(user_id: int, user_text: str, current_memory: str):
    if len(user_text.strip()) < 12 or user_text.startswith("/") or user_text.startswith("["):
        return
    extraction_prompt = (
        "Ты — модуль долговременной памяти персонального ИИ-ассистента.\n"
        "Твоя задача — извлекать долговечные факты о пользователе (имя, проекты, увлечения, стек, стиль).\n"
        f"Текущая карточка:\n{current_memory or 'Пока пусто'}\n\n"
        f"Новое сообщение пользователя:\n«{user_text}»\n\n"
        "ИНСТРУКЦИЯ:\n• Если есть новая информация — дополни список через '• '.\n• Если ничего важного нет — верни текущую память БЕЗ изменений.\n• Верни ТОЛЬКО итоговый список."
    )
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": "deepseek/deepseek-chat", "messages": [{"role": "system", "content": extraction_prompt}], "max_tokens": 300}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    new_memory = data["choices"][0]["message"]["content"].strip()
                    if new_memory and "пока пусто" not in new_memory.lower() and new_memory != current_memory:
                        save_user_memory(user_id, strip_markdown(new_memory))
    except Exception as e:
        logging.error(f"Background memory error: {e}")

def log_stat(owner_id: int, tokens: int = 0, is_spam: bool = False):
    conn = sqlite3.connect("bot_config.db")
    c = conn.cursor()
    spam_inc = 1 if is_spam else 0
    c.execute('''INSERT INTO stats (user_id, msg_count, tokens, spam_count) VALUES (?, 1, ?, ?)
                 ON CONFLICT(user_id) DO UPDATE SET msg_count = msg_count + 1, tokens = tokens + excluded.tokens, spam_count = spam_count + excluded.spam_count''',
              (owner_id, tokens, spam_inc))
    conn.commit()
    conn.close()

def log_lead(owner_id: int, chat_id: int, user_info: str, username: str, snippet: str):
    conn = sqlite3.connect("bot_config.db")
    c = conn.cursor()
    c.execute('''INSERT INTO leads (owner_id, chat_id, user_info, username, snippet, timestamp) VALUES (?, ?, ?, ?, ?, ?)''',
              (owner_id, chat_id, user_info, username, snippet[:120], int(time.time())))
    conn.commit()
    conn.close()

def get_stats_data(owner_id: int):
    conn = sqlite3.connect("bot_config.db")
    c = conn.cursor()
    c.execute("SELECT msg_count, tokens, spam_count FROM stats WHERE user_id = ?", (owner_id,))
    st = c.fetchone() or (0, 0, 0)
    c.execute("SELECT user_info, username, snippet FROM leads WHERE owner_id = ? ORDER BY id DESC LIMIT 5", (owner_id,))
    leads = c.fetchall()
    conn.close()
    return st[0], st[1], st[2], leads

def save_conn_owner(conn_id: str, user_id: int):
    conn = sqlite3.connect("bot_config.db")
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO business_conns (conn_id, user_id) VALUES (?, ?)", (conn_id, user_id))
    conn.commit()
    conn.close()

def get_conn_owner(conn_id: str):
    conn = sqlite3.connect("bot_config.db")
    c = conn.cursor()
    c.execute("SELECT user_id FROM business_conns WHERE conn_id = ?", (conn_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else OWNER_ID

def remove_conn(conn_id: str):
    conn = sqlite3.connect("bot_config.db")
    c = conn.cursor()
    c.execute("DELETE FROM business_conns WHERE conn_id = ?", (conn_id,))
    conn.commit()
    conn.close()

def register_user(user: types.User):
    if not user:
        return
    conn = sqlite3.connect("bot_config.db")
    c = conn.cursor()
    c.execute('''INSERT INTO registered_users (user_id, username, full_name) VALUES (?, ?, ?) 
                 ON CONFLICT(user_id) DO UPDATE SET username = excluded.username, full_name = excluded.full_name''', 
              (user.id, user.username or "", user.full_name or "Пользователь"))
    conn.commit()
    conn.close()

def get_all_users():
    conn = sqlite3.connect("bot_config.db")
    c = conn.cursor()
    c.execute("SELECT user_id FROM registered_users")
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]

def get_detailed_users():
    conn = sqlite3.connect("bot_config.db")
    c = conn.cursor()
    c.execute("SELECT user_id, username, full_name FROM registered_users")
    rows = c.fetchall()
    conn.close()
    return rows

PUBLIC_DM_MODELS = [
    ("🔹 DeepSeek V3 🐳", "deepseek/deepseek-chat"),
    ("🔹 Gemini 2.0 Flash ⚡️ (Мультимод.)", "google/gemini-2.0-flash-001"),
    ("🔹 Gemini 1.5 Pro 💫 (Мультимод.)", "google/gemini-pro-1.5"),
    ("🔹 Llama 3.3 70B 🦙", "meta-llama/llama-3.3-70b-instruct")
]

PUBLIC_BIZ_MODELS = [
    ("🔹 DeepSeek V3 🐳", "deepseek/deepseek-chat"),
    ("🔹 Gemini 2.0 Flash ⚡️ (Мультимод.)", "google/gemini-2.0-flash-001"),
    ("🔹 Llama 3.3 70B 🦙", "meta-llama/llama-3.3-70b-instruct")
]

OWNER_EXTRA_MODELS = [
    ("👑 Claude 3.5 Sonnet 🎭 (Мультимод.)", "anthropic/claude-3.5-sonnet"),
    ("👑 DeepSeek R1 🧠", "deepseek/deepseek-r1")
]

def get_user_config(user_id: int):
    conn = sqlite3.connect("bot_config.db")
    c = conn.cursor()
    c.execute("SELECT model, system_prompt, lang, preset, biz_model, show_disclaimer FROM user_configs WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        dm_model = row[0] or "deepseek/deepseek-chat"
        lang = row[2] or "ru"
        preset = row[3] or "strict"
        biz_model = row[4] or dm_model
        show_disclaimer = row[5] if row[5] is not None else 1
        presets_dict = PRESETS_EN if lang == "en" else PRESETS_RU
        prompt = presets_dict[preset] if preset in presets_dict else (row[1] or presets_dict["strict"])
        return dm_model, prompt, lang, preset, biz_model, show_disclaimer
    return "deepseek/deepseek-chat", PRESETS_RU["strict"], "ru", "strict", "deepseek/deepseek-chat", 1

def toggle_user_disclaimer(user_id: int) -> int:
    dm_model, prompt, lang, preset, biz_model, show_disclaimer = get_user_config(user_id)
    new_val = 0 if show_disclaimer == 1 else 1
    conn = sqlite3.connect("bot_config.db")
    c = conn.cursor()
    c.execute('''INSERT INTO user_configs (user_id, model, system_prompt, lang, preset, biz_model, show_disclaimer) 
                 VALUES (?, ?, ?, ?, ?, ?, ?) 
                 ON CONFLICT(user_id) DO UPDATE SET show_disclaimer = excluded.show_disclaimer''', 
              (user_id, dm_model, prompt, lang, preset, biz_model, new_val))
    conn.commit()
    conn.close()
    return new_val

def update_user_dm_model(user_id: int, new_model: str):
    _, prompt, lang, preset, biz_model, show_disclaimer = get_user_config(user_id)
    conn = sqlite3.connect("bot_config.db")
    c = conn.cursor()
    c.execute('''INSERT INTO user_configs (user_id, model, system_prompt, lang, preset, biz_model, show_disclaimer) 
                 VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET model = excluded.model''', 
              (user_id, new_model, prompt, lang, preset, biz_model, show_disclaimer))
    conn.commit()
    conn.close()

def update_user_biz_model(user_id: int, new_model: str):
    dm_model, prompt, lang, preset, _, show_disclaimer = get_user_config(user_id)
    conn = sqlite3.connect("bot_config.db")
    c = conn.cursor()
    c.execute('''INSERT INTO user_configs (user_id, model, system_prompt, lang, preset, biz_model, show_disclaimer) 
                 VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET biz_model = excluded.biz_model''', 
              (user_id, dm_model, prompt, lang, preset, new_model, show_disclaimer))
    conn.commit()
    conn.close()

def update_user_prompt(user_id: int, new_prompt: str):
    dm_model, _, lang, _, biz_model, show_disclaimer = get_user_config(user_id)
    conn = sqlite3.connect("bot_config.db")
    c = conn.cursor()
    c.execute('''INSERT INTO user_configs (user_id, model, system_prompt, lang, preset, biz_model, show_disclaimer) 
                 VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET system_prompt = excluded.system_prompt, preset = excluded.preset''', 
              (user_id, dm_model, new_prompt, lang, "custom", biz_model, show_disclaimer))
    conn.commit()
    conn.close()

def update_user_preset(user_id: int, preset_key: str):
    dm_model, _, lang, _, biz_model, show_disclaimer = get_user_config(user_id)
    presets_dict = PRESETS_EN if lang == "en" else PRESETS_RU
    new_prompt = presets_dict.get(preset_key, presets_dict["strict"])
    conn = sqlite3.connect("bot_config.db")
    c = conn.cursor()
    c.execute('''INSERT INTO user_configs (user_id, model, system_prompt, lang, preset, biz_model, show_disclaimer) 
                 VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET system_prompt = excluded.system_prompt, preset = excluded.preset''', 
              (user_id, dm_model, new_prompt, lang, preset_key, biz_model, show_disclaimer))
    conn.commit()
    conn.close()

def update_user_lang(user_id: int, new_lang: str):
    dm_model, prompt, _, preset, biz_model, show_disclaimer = get_user_config(user_id)
    if preset in ("strict", "blogger", "support", "vacation", "consultant"):
        presets_dict = PRESETS_EN if new_lang == "en" else PRESETS_RU
        prompt = presets_dict[preset]
    conn = sqlite3.connect("bot_config.db")
    c = conn.cursor()
    c.execute('''INSERT INTO user_configs (user_id, model, system_prompt, lang, preset, biz_model, show_disclaimer) 
                 VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET lang = excluded.lang, system_prompt = excluded.system_prompt''', 
              (user_id, dm_model, prompt, new_lang, preset, biz_model, show_disclaimer))
    conn.commit()
    conn.close()

def is_blacklisted(owner_id: int, user_id: int) -> bool:
    conn = sqlite3.connect("bot_config.db")
    c = conn.cursor()
    c.execute("SELECT 1 FROM blacklist WHERE owner_id = ? AND blocked_id = ?", (owner_id, user_id))
    row = c.fetchone()
    conn.close()
    return bool(row)

def get_main_reply_keyboard(lang: str = "ru") -> ReplyKeyboardMarkup:
    if lang == "en":
        kb = [
            [KeyboardButton(text="⚙️ Settings"), KeyboardButton(text="✨ New Topic")],
            [KeyboardButton(text="🧠 Memory"), KeyboardButton(text="📊 Stats")]
        ]
    else:
        kb = [
            [KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text="✨ Новый диалог")],
            [KeyboardButton(text="🧠 Память"), KeyboardButton(text="📊 Статистика")]
        ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def get_menu_keyboard(is_owner: bool, lang: str):
    kb = [
        [InlineKeyboardButton(text=t("btn_model", lang), callback_data="choose_model_target")],
        [InlineKeyboardButton(text=t("btn_presets", lang), callback_data="open_presets")],
        [InlineKeyboardButton(text=t("btn_memory", lang), callback_data="view_memory")],
        [InlineKeyboardButton(text=t("btn_stats", lang), callback_data="view_stats")],
        [InlineKeyboardButton(text=t("btn_advanced", lang), callback_data="open_advanced")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_model_target_keyboard(lang: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t("btn_target_dm", lang), callback_data="select_target:dm")],
        [InlineKeyboardButton(text=t("btn_target_biz", lang), callback_data="select_target:biz")],
        [InlineKeyboardButton(text=t("btn_back", lang), callback_data="back_menu")]
    ])

def get_advanced_keyboard(is_owner: bool, lang: str, show_disclaimer: int = 1):
    disc_status = "Вкл ✅" if show_disclaimer == 1 else "Выкл ❌"
    disc_btn_text = f"🏷 Подпись ИИ: {disc_status}" if lang == "ru" else f"🏷 AI Watermark: {disc_status}"
    kb = [
        [InlineKeyboardButton(text=disc_btn_text, callback_data="toggle_disclaimer")],
        [InlineKeyboardButton(text=t("btn_about", lang), callback_data="view_about")],
        [InlineKeyboardButton(text=t("btn_lang", lang), callback_data="choose_lang")]
    ]
    if is_owner:
        kb.append([InlineKeyboardButton(text=t("btn_users", lang), callback_data="list_users")])
        kb.append([InlineKeyboardButton(text=t("btn_broadcast", lang), callback_data="start_broadcast")])
    kb.append([InlineKeyboardButton(text=t("btn_back", lang), callback_data="back_menu")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_presets_keyboard(lang: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t("preset_strict", lang), callback_data="set_preset:strict"),
         InlineKeyboardButton(text=t("preset_blogger", lang), callback_data="set_preset:blogger")],
        [InlineKeyboardButton(text=t("preset_support", lang), callback_data="set_preset:support"),
         InlineKeyboardButton(text=t("preset_consultant", lang), callback_data="set_preset:consultant")],
        [InlineKeyboardButton(text=t("preset_vacation", lang), callback_data="set_preset:vacation"),
         InlineKeyboardButton(text=t("preset_custom", lang), callback_data="edit_prompt")],
        [InlineKeyboardButton(text=t("btn_back", lang), callback_data="back_menu")]
    ])

def get_lang_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇷🇺 Русский", callback_data="set_lang:ru"),
         InlineKeyboardButton(text="🇬🇧 English", callback_data="set_lang:en")],
        [InlineKeyboardButton(text="« Back / Назад", callback_data="open_advanced")]
    ])

def get_models_keyboard(target: str, is_owner: bool, lang: str):
    kb = []
    models_list = PUBLIC_DM_MODELS if target == "dm" else PUBLIC_BIZ_MODELS
    for title, m_id in models_list:
        kb.append([InlineKeyboardButton(text=title, callback_data=f"set_model:{target}:{m_id}")])
    if is_owner:
        for title, m_id in OWNER_EXTRA_MODELS:
            kb.append([InlineKeyboardButton(text=title, callback_data=f"set_model:{target}:{m_id}")])
        kb.append([InlineKeyboardButton(text="✍️ Custom Model ID", callback_data=f"custom_model:{target}")])
    kb.append([InlineKeyboardButton(text=t("btn_back", lang), callback_data="choose_model_target")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_notify_keyboard(chat_id: int, is_paused: bool = False, lang: str = "ru"):
    pause_text = t("btn_unmute", lang) if is_paused else t("btn_mute", lang)
    pause_cb = f"unmute:{chat_id}" if is_paused else f"mute:{chat_id}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t("btn_summary", lang), callback_data=f"summary:{chat_id}"),
         InlineKeyboardButton(text=pause_text, callback_data=pause_cb)]
    ])

async def show_panel(message: types.Message, user_id: int):
    is_owner = (user_id == OWNER_ID)
    dm_model, _, lang, preset, biz_model, _ = get_user_config(user_id)
    role_badge = t("role_owner", lang) if is_owner else t("role_user", lang)
    preset_name = t(f"preset_{preset}", lang) if f"preset_{preset}" in TEXTS[lang] else t("preset_custom", lang)
    text = (
        f"{t('panel_title', lang, role=role_badge)}\n\n"
        f"{t('cur_models', lang, dm_model=get_pretty_model_name(dm_model), biz_model=get_pretty_model_name(biz_model))}\n"
        f"{t('cur_preset', lang, preset=preset_name)}"
    )
    try:
        await message.edit_text(text, reply_markup=get_menu_keyboard(is_owner, lang), parse_mode="HTML")
    except Exception:
        await message.answer(text, reply_markup=get_menu_keyboard(is_owner, lang), parse_mode="HTML")

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    register_user(message.from_user)
    _, _, lang, _, _, _ = get_user_config(message.from_user.id)
    await message.answer(t("start", lang), reply_markup=get_main_reply_keyboard(lang), parse_mode="HTML")

@dp.message(Command("new", "reset"))
async def cmd_reset_context(message: types.Message):
    chat_history[message.chat.id].clear()
    _, _, lang, _, _, _ = get_user_config(message.from_user.id)
    await message.answer(t("chat_reset_msg", lang), reply_markup=get_main_reply_keyboard(lang), parse_mode="HTML")

@dp.message(Command("memory"))
async def cmd_memory(message: types.Message):
    mem = get_user_memory(message.from_user.id)
    _, _, lang, _, _, _ = get_user_config(message.from_user.id)
    await message.answer(t("memory_view", lang, memory=mem if mem else t("memory_empty", lang)), parse_mode="HTML")

@dp.message(Command("forget"))
async def cmd_forget(message: types.Message):
    clear_user_memory(message.from_user.id)
    _, _, lang, _, _, _ = get_user_config(message.from_user.id)
    await message.answer(t("memory_cleared", lang), parse_mode="HTML")

@dp.message(Command("settings", "admin"))
async def cmd_settings(message: types.Message, state: FSMContext = None):
    register_user(message.from_user)
    if state:
        await state.clear()
    await show_panel(message, message.from_user.id)

@dp.callback_query()
async def handle_callbacks(call: types.CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    is_owner = (uid == OWNER_ID)
    dm_model, prompt, lang, preset, biz_model, show_disclaimer = get_user_config(uid)
    data = call.data
    
    if data == "choose_model_target":
        await call.answer()
        await call.message.edit_text(t("choose_target_title", lang), reply_markup=get_model_target_keyboard(lang), parse_mode="HTML")
    elif data.startswith("select_target:"):
        await call.answer()
        target = data.split(":")[1]
        t_name = t("target_dm_name", lang) if target == "dm" else t("target_biz_name", lang)
        await call.message.edit_text(t("model_choose_title", lang, target=t_name), reply_markup=get_models_keyboard(target, is_owner, lang), parse_mode="HTML")
    elif data.startswith("set_model:"):
        _, target, new_model = data.split(":")
        if target == "dm":
            update_user_dm_model(uid, new_model)
            t_name = t("target_dm_name", lang)
        else:
            update_user_biz_model(uid, new_model)
            t_name = t("target_biz_name", lang)
        await call.answer(t("model_set", lang, target=t_name, model=get_pretty_model_name(new_model)), show_alert=False)
        await show_panel(call.message, uid)
    elif data == "open_presets":
        await call.answer()
        await call.message.edit_text(t("presets_menu_title", lang), reply_markup=get_presets_keyboard(lang), parse_mode="HTML")
    elif data.startswith("set_preset:"):
        p_key = data.split("set_preset:")[1]
        update_user_preset(uid, p_key)
        await call.answer(t("preset_set", lang, name=t(f"preset_{p_key}", lang)), show_alert=False)
        await show_panel(call.message, uid)
    elif data == "view_memory":
        await call.answer()
        mem = get_user_memory(uid)
        back_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t("btn_back", lang), callback_data="back_menu")]])
        await call.message.edit_text(t("memory_view", lang, memory=mem if mem else t("memory_empty", lang)), reply_markup=back_kb, parse_mode="HTML")
    elif data == "open_advanced":
        await call.answer()
        await call.message.edit_text(t("panel_title", lang, role=t("role_owner", lang) if is_owner else t("role_user", lang)), 
                                     reply_markup=get_advanced_keyboard(is_owner, lang, show_disclaimer), parse_mode="HTML")
    elif data == "toggle_disclaimer":
        new_val = toggle_user_disclaimer(uid)
        toast = "Подпись ИИ включена" if new_val == 1 else "Подпись ИИ выключена"
        await call.answer(toast, show_alert=False)
        await call.message.edit_reply_markup(reply_markup=get_advanced_keyboard(is_owner, lang, new_val))
    elif data == "view_about":
        await call.answer()
        back_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t("btn_back", lang), callback_data="open_advanced")]])
        await call.message.edit_text(t("about_text", lang), reply_markup=back_kb, parse_mode="HTML")
    elif data == "view_stats":
        await call.answer()
        msg_count, tokens, spam_count, leads = get_stats_data(uid)
        leads_str = t("stats_no_leads", lang) if not leads else "\n".join([f"• <b>{l[0]}</b>: <i>{l[2]}</i>" for l in leads])
        back_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t("btn_back", lang), callback_data="back_menu")]])
        await call.message.edit_text(t("stats_title", lang, msg_count=msg_count, tokens=tokens, spam_count=spam_count, recent_leads=leads_str), reply_markup=back_kb, parse_mode="HTML")
    elif data == "choose_lang":
        await call.answer()
        await call.message.edit_text(t("choose_lang_title", lang), reply_markup=get_lang_keyboard(), parse_mode="HTML")
    elif data.startswith("set_lang:"):
        new_lang = data.split("set_lang:")[1]
        update_user_lang(uid, new_lang)
        await call.answer(t("lang_set", new_lang), show_alert=False)
        await show_panel(call.message, uid)
    elif data.startswith("custom_model:") and is_owner:
        await call.answer()
        target = data.split(":")[1]
        await state.update_data(target=target)
        await state.set_state(FormStates.waiting_for_custom_model)
        t_name = t("target_dm_name", lang) if target == "dm" else t("target_biz_name", lang)
        cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t("btn_cancel", lang), callback_data="cancel_action")]])
        await call.message.edit_text(t("custom_model_prompt", lang, target=t_name), reply_markup=cancel_kb, parse_mode="HTML")
    elif data == "edit_prompt":
        await call.answer()
        await state.set_state(FormStates.waiting_for_prompt)
        cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t("btn_cancel", lang), callback_data="cancel_action")]])
        await call.message.edit_text(t("prompt_edit_title", lang), reply_markup=cancel_kb, parse_mode="HTML")
    elif data == "start_broadcast" and is_owner:
        await call.answer()
        await state.set_state(FormStates.waiting_for_broadcast)
        cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t("btn_cancel", lang), callback_data="cancel_action")]])
        await call.message.edit_text(t("broadcast_title", lang), reply_markup=cancel_kb, parse_mode="HTML")
    elif data == "list_users" and is_owner:
        await call.answer()
        users = get_detailed_users()
        text = t("users_empty", lang) if not users else t("users_title", lang, count=len(users)) + "\n".join([f"{i}. <b>{u[2]}</b> (@{u[1]}) — <code>{u[0]}</code>" for i, u in enumerate(users, 1)])
        back_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t("btn_back", lang), callback_data="open_advanced")]])
        await call.message.edit_text(text, reply_markup=back_kb, parse_mode="HTML")
    elif data in ("cancel_action", "back_menu"):
        await call.answer()
        await state.clear()
        await show_panel(call.message, uid)
    elif data.startswith("summary:"):
        target_chat = int(data.split(":")[1])
        history = list(chat_history[target_chat])
        if not history:
            await call.answer(t("summary_empty", lang), show_alert=True)
            return
        await call.answer()
        status_msg = await call.message.reply(t("summary_loading", lang), parse_mode="HTML")
        dialog_text = "\n".join([f"{'Клиент' if m['role']=='user' else 'Бот'}: {m['content']}" for m in history])
        summary_prompt = "Ты — ИИ-аналитик. Составь карточку сделки в Telegram HTML:\n🎯 <b>Услуга:</b> [суть]\n💰 <b>Бюджет:</b> [сумма]\n⏳ <b>Сроки:</b> [дедлайн]\n📌 <b>Детали:</b> [описание]"
        lead_summary, _ = await ask_openrouter(call.message.chat.id, f"Диалог:\n{dialog_text}", custom_system_prompt=summary_prompt, model=dm_model, lang=lang)
        await status_msg.edit_text(f"{t('summary_header', lang)}{dlp_sanitize(format_to_tg_html(lead_summary))}", parse_mode="HTML")
    elif data.startswith("mute:"):
        target_chat = int(data.split(":")[1])
        paused_chats[target_chat] = time.time() + 86400
        await call.answer("🛑 Бот заглушен на 24 часа", show_alert=False)
        await call.message.edit_reply_markup(reply_markup=get_notify_keyboard(target_chat, is_paused=True, lang=lang))
    elif data.startswith("unmute:"):
        target_chat = int(data.split(":")[1])
        paused_chats.pop(target_chat, None)
        await call.answer("▶️ Бот снова активен", show_alert=False)
        await call.message.edit_reply_markup(reply_markup=get_notify_keyboard(target_chat, is_paused=False, lang=lang))

@dp.message(FormStates.waiting_for_prompt)
async def process_new_prompt(message: types.Message, state: FSMContext):
    _, _, lang, _, _, _ = get_user_config(message.from_user.id)
    update_user_prompt(message.from_user.id, message.text)
    await state.clear()
    await message.answer(t("prompt_set", lang), reply_markup=get_main_reply_keyboard(lang), parse_mode="HTML")
    await show_panel(message, message.from_user.id)

@dp.message(FormStates.waiting_for_custom_model)
async def process_custom_model(message: types.Message, state: FSMContext):
    data = await state.get_data()
    target = data.get("target", "dm")
    _, _, lang, _, _, _ = get_user_config(message.from_user.id)
    new_model = message.text.strip()
    if message.from_user.id == OWNER_ID:
        if target == "dm":
            update_user_dm_model(message.from_user.id, new_model)
            t_name = t("target_dm_name", lang)
        else:
            update_user_biz_model(message.from_user.id, new_model)
            t_name = t("target_biz_name", lang)
        await message.answer(t("custom_model_set", lang, target=t_name, model=new_model), reply_markup=get_main_reply_keyboard(lang), parse_mode="HTML")
    await state.clear()
    await show_panel(message, message.from_user.id)

@dp.message(FormStates.waiting_for_broadcast)
async def process_broadcast(message: types.Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return
    _, _, lang, _, _, _ = get_user_config(message.from_user.id)
    await state.clear()
    users = get_all_users()
    if not users:
        await message.answer(t("broadcast_empty", lang))
        await show_panel(message, message.from_user.id)
        return
    status_msg = await message.answer(t("broadcast_started", lang, count=len(users)), parse_mode="HTML")
    success, failed = 0, 0
    for uid in users:
        try:
            if message.photo:
                await bot.send_photo(uid, photo=message.photo[-1].file_id, caption=message.caption or "", parse_mode="HTML")
            elif message.text:
                await bot.send_message(uid, text=message.text, parse_mode="HTML")
            success += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1
    await status_msg.edit_text(t("broadcast_done", lang, success=success, failed=failed), parse_mode="HTML")
    await show_panel(message, message.from_user.id)

async def ask_openrouter(chat_id: int, user_text: str, custom_system_prompt: str = None, model: str = None, lang: str = "ru"):
    active_model = model or "deepseek/deepseek-chat"
    active_prompt = custom_system_prompt or PRESETS_RU["strict"]
    clean_text = user_text[:800].strip()
    chat_history[chat_id].append({"role": "user", "content": clean_text})
    messages = [{"role": "system", "content": active_prompt}] + list(chat_history[chat_id])
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": active_model, "messages": messages, "max_tokens": 600}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload) as resp:
                data = await resp.json()
                reply = dlp_sanitize(data["choices"][0]["message"]["content"])
                usage = data.get("usage", {}).get("total_tokens", 100)
                chat_history[chat_id].append({"role": "assistant", "content": reply})
                return reply, usage
        except Exception as e:
            logging.error(f"OpenRouter error: {e}")
            return t("ai_error", lang), 0

async def ask_openrouter_stream(target_message: types.Message, user_text: str, custom_system_prompt: str = None, model: str = None, lang: str = "ru", image_b64: str = None):
    active_model = model or "deepseek/deepseek-chat"
    active_prompt = custom_system_prompt or DIRECT_PROMPTS["ru"]
    clean_text = user_text[:800].strip()
    chat_id = target_message.chat.id
    
    if image_b64:
        user_msg_content = [
            {"type": "text", "text": clean_text or "Что на этом фото?"},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}}
        ]
        history_snapshot = list(chat_history[chat_id])
        messages = [{"role": "system", "content": active_prompt}] + history_snapshot + [{"role": "user", "content": user_msg_content}]
        chat_history[chat_id].append({"role": "user", "content": f"[Фото] {clean_text}".strip()})
    else:
        chat_history[chat_id].append({"role": "user", "content": clean_text})
        messages = [{"role": "system", "content": active_prompt}] + list(chat_history[chat_id])
    
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": active_model, "messages": messages, "max_tokens": 800, "stream": True}
    sent_msg = await target_message.answer("...")
    full_text, last_rendered, last_edit = "", "", time.time()
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload) as resp:
                if resp.status != 200:
                    await sent_msg.edit_text(t("ai_error", lang))
                    return t("ai_error", lang), 0
                async for line in resp.content:
                    line_str = line.decode("utf-8").strip()
                    if not line_str.startswith("data:"): continue
                    data_part = line_str[5:].strip()
                    if data_part == "[DONE]": break
                    try:
                        delta = json.loads(data_part).get("choices", [{}])[0].get("delta", {}).get("content", "")
                        if delta:
                            full_text += delta
                            now = time.time()
                            if now - last_edit >= 0.35 and len(full_text) - len(last_rendered) >= 8:
                                await sent_msg.edit_text(strip_markdown(full_text))
                                last_rendered, last_edit = full_text, now
                    except Exception:
                        continue
        except Exception as e:
            logging.error(f"Stream error: {e}")
            
    if not full_text:
        await sent_msg.edit_text(t("ai_error", lang))
        return t("ai_error", lang), 0
    
    formatted = dlp_sanitize(format_to_tg_html(full_text))
    chat_history[chat_id].append({"role": "assistant", "content": formatted})
    try:
        await sent_msg.edit_text(formatted, parse_mode="HTML")
    except Exception:
        await sent_msg.edit_text(strip_markdown(full_text))
    return formatted, len(full_text) // 3

@dp.business_connection()
async def handle_business_connection(conn: types.BusinessConnection):
    register_user(conn.user)
    if conn.is_enabled:
        save_conn_owner(conn.id, conn.user.id)
    else:
        remove_conn(conn.id)

@dp.business_message()
async def handle_business(message: types.Message):
    if message.message_id in processed_msg_ids:
        return
    processed_msg_ids.append(message.message_id)
    chat_id = message.chat.id
    conn_id = message.business_connection_id
    current_owner = get_conn_owner(conn_id)
    now = time.time()
    
    if message.from_user.id != chat_id:
        save_conn_owner(conn_id, message.from_user.id)
        paused_chats[chat_id] = now + 10800
        return
    
    if is_blacklisted(current_owner, message.from_user.id): return
    if chat_id in paused_chats and now < paused_chats[chat_id]: return
    
    recent_times = [ts for ts in chat_rate_limits[chat_id] if now - ts < 240]
    if len(recent_times) >= 5: return
    chat_rate_limits[chat_id].append(now)

    bot_replies_count = sum(1 for m in chat_history[chat_id] if m["role"] == "assistant")
    if bot_replies_count >= 8:
        paused_chats[chat_id] = now + 86400
        return

    if chat_id in active_chat_locks: return
    active_chat_locks.add(chat_id)
    
    try:
        _, system_prompt, lang, _, biz_model, show_disclaimer = get_user_config(current_owner)
        disclaimer_suffix = ("\n\nЭтот ответ сгенерирован AI ассистентом 🤖" if lang == "ru" else "\n\nThis reply was generated by AI assistant 🤖") if show_disclaimer else ""
        last_bot_reply = next((m["content"].lower() for m in reversed(chat_history[chat_id]) if m["role"] == "assistant"), "")
        was_dismissed = "удалите этот чат" in last_bot_reply or "delete this chat" in last_bot_reply

        user_text = message.text or message.caption
        is_media_without_text = False

        if not user_text:
            if message.sticker or message.animation or message.photo or message.voice or message.video_note or message.document:
                is_media_without_text = True
                user_text = "[Медиа]"
            else:
                return

        if is_media_without_text:
            if was_dismissed:
                spam_base = "Пожалуйста, удалите этот чат и не тратьте наше время" if lang == "ru" else "Please delete this chat and do not waste our time"
                await message.answer(spam_base + disclaimer_suffix)
            else:
                await message.answer(t("media_fallback", lang) + disclaimer_suffix)
            return

        user_text_lower = user_text.lower().strip()
        has_business_intent = any(w in user_text_lower for w in BUSINESS_INTENT_WORDS)
        is_hard_spam = any(re.search(pat, user_text_lower) for pat in SPAM_PHRASE_PATTERNS)

        if is_hard_spam and not has_business_intent:
            spam_base = "Пожалуйста, удалите этот чат и не тратьте наше время" if lang == "ru" else "Please delete this chat and do not waste our time"
            chat_history[chat_id].append({"role": "assistant", "content": spam_base})
            await message.answer(spam_base + disclaimer_suffix)
            log_stat(current_owner, tokens=0, is_spam=True)
            return

        ai_reply, tokens_used = await ask_openrouter(chat_id, user_text, custom_system_prompt=system_prompt, model=biz_model, lang=lang)
        if not show_disclaimer:
            ai_reply = re.sub(r'(?i)\n*Этот ответ сгенерирован AI ассистентом.*', '', ai_reply).strip()
        else:
            if "сгенерирован ai" not in ai_reply.lower():
                ai_reply = ai_reply.strip() + disclaimer_suffix

        formatted_reply = dlp_sanitize(format_to_tg_html(ai_reply))
        try:
            await message.answer(formatted_reply, parse_mode="HTML")
        except Exception:
            await message.answer(strip_markdown(ai_reply))
        
        is_spam = ("удалите этот чат" in ai_reply.lower() or "не тратьте наше время" in ai_reply.lower())
        log_stat(current_owner, tokens=tokens_used, is_spam=is_spam)
        if is_spam: return

        clean_text_check = re.sub(r'[^\w\s]', '', user_text_lower).strip()
        if clean_text_check not in GREETING_WORDS and len(clean_text_check) > 3 or has_business_intent:
            user_info = message.from_user.full_name or "Пользователь"
            username = message.from_user.username or ""
            log_lead(current_owner, chat_id, user_info, username, user_text)
            username_tag = f"@{username}" if username else f"ID {message.from_user.id}"
            notify_text = t("notify_title", lang, user_info=user_info, username=username_tag, user_text=user_text, ai_reply=formatted_reply)
            try:
                await bot.send_message(current_owner, notify_text, reply_markup=get_notify_keyboard(chat_id, is_paused=False, lang=lang), parse_mode="HTML")
            except Exception as e:
                logging.error(f"Notify error: {e}")
    finally:
        active_chat_locks.discard(chat_id)

@dp.message(F.sticker)
async def handle_direct_sticker(message: types.Message):
    register_user(message.from_user)
    uid = message.from_user.id
    dm_model, _, lang, _, _, _ = get_user_config(uid)
    user_prompt = f"[Пользователь отправил стикер: {message.sticker.emoji or '✨'}]"
    user_mem = get_user_memory(uid)
    base_prompt = DIRECT_PROMPTS.get(lang, DIRECT_PROMPTS["ru"]).format(model_name=get_pretty_model_name(dm_model))
    if user_mem: base_prompt += f"\n\nПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ:\n{user_mem}"
    _, tokens = await ask_openrouter_stream(message, user_prompt, custom_system_prompt=base_prompt, model=dm_model, lang=lang)
    log_stat(uid, tokens=tokens, is_spam=False)

@dp.message(F.photo)
async def handle_direct_photo(message: types.Message):
    register_user(message.from_user)
    uid = message.from_user.id
    dm_model, _, lang, _, _, _ = get_user_config(uid)
    if not is_multimodal_model(dm_model):
        await message.answer(t("vision_not_supported", lang, model=get_pretty_model_name(dm_model)), parse_mode="HTML")
        return
    status = await message.answer("⏳ <i>Анализирую фото...</i>", parse_mode="HTML")
    try:
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        photo_bytes = await bot.download_file(file_info.file_path)
        img_b64 = base64.b64encode(photo_bytes.read()).decode("utf-8")
        user_mem = get_user_memory(uid)
        base_prompt = DIRECT_PROMPTS.get(lang, DIRECT_PROMPTS["ru"]).format(model_name=get_pretty_model_name(dm_model))
        if user_mem: base_prompt += f"\n\nПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ:\n{user_mem}"
        await status.delete()
        caption = message.caption or "Что изображено на фото?"
        _, tokens = await ask_openrouter_stream(message, caption, custom_system_prompt=base_prompt, model=dm_model, lang=lang, image_b64=img_b64)
        log_stat(uid, tokens=tokens, is_spam=False)
    except Exception as e:
        logging.error(f"Vision error: {e}")
        try: await status.edit_text(t("ai_error", lang))
        except Exception: pass

@dp.message(F.text & ~F.text.startswith("/"))
async def handle_direct_text(message: types.Message, state: FSMContext):
    if await state.get_state() is not None: return
    text = message.text.strip()
    uid = message.from_user.id
    register_user(message.from_user)
    dm_model, _, lang, _, _, _ = get_user_config(uid)

    if text in ("⚙️ Настройки", "⚙️ Settings"):
        await cmd_settings(message, state)
        return
    elif text in ("✨ Новый диалог", "✨ New Topic"):
        await cmd_reset_context(message)
        return
    elif text in ("🧠 Память", "🧠 Memory"):
        await cmd_memory(message)
        return
    elif text in ("📊 Статистика", "📊 Stats"):
        msg_count, tokens, spam_count, leads = get_stats_data(uid)
        leads_str = t("stats_no_leads", lang) if not leads else "\n".join([f"• <b>{l[0]}</b>: <i>{l[2]}</i>" for l in leads])
        await message.answer(t("stats_title", lang, msg_count=msg_count, tokens=tokens, spam_count=spam_count, recent_leads=leads_str), parse_mode="HTML")
        return

    if message.message_id in processed_msg_ids: return
    processed_msg_ids.append(message.message_id)

    user_mem = get_user_memory(uid)
    base_prompt = DIRECT_PROMPTS.get(lang, DIRECT_PROMPTS["ru"]).format(model_name=get_pretty_model_name(dm_model))
    if user_mem: base_prompt += f"\n\nПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ:\n{user_mem}"
    _, tokens = await ask_openrouter_stream(message, message.text, custom_system_prompt=base_prompt, model=dm_model, lang=lang)
    log_stat(uid, tokens=tokens, is_spam=False)
    asyncio.create_task(update_memory_in_background(uid, message.text, user_mem))

async def setup_bot_commands():
    commands = [
        BotCommand(command="start", description="Перезапустить бота"),
        BotCommand(command="new", description="Новый диалог"),
        BotCommand(command="settings", description="Панель настроек"),
        BotCommand(command="memory", description="Память обо мне"),
        BotCommand(command="forget", description="Очистить память")
    ]
    try:
        await bot.set_my_commands(commands)
    except Exception as e:
        logging.error(f"Commands error: {e}")

async def main():
    await setup_bot_commands()
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
