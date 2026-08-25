import asyncio
import logging
import sqlite3
import time
import re
import os
import json
import aiohttp
from collections import defaultdict, deque
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OWNER_ID = int(os.getenv("OWNER_ID", "5240174256"))
BOT_VERSION = "v2.3.0"

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

chat_history = defaultdict(lambda: deque(maxlen=10))
paused_chats = {}
processed_msg_ids = deque(maxlen=300)
active_chat_locks = set()
chat_rate_limits = defaultdict(lambda: deque(maxlen=20))

# 🛡 Белый список коммерческих намерений (приоритет над спамом)
BUSINESS_INTENT_WORDS = [
    "реклам", "купит", "заказ", "сотруднич", "прайс", 
    "стоимост", "бюджет", "оплат", "интеграц", "тариф", "размещен"
]

# 🛡 Шаблоны спам-приглашений, флуда и сомнительных схем
SPAM_PHRASE_PATTERNS = [
    r"(?:вступ|заход|переход|присоединяй|подпиш|го\s+в|иди\s+в).*(?:флуд|чат|бесед|конф|канал|групп|паблик)",
    r"(?:взаимн(?:ая|ый|о)).*(?:подписк|пиар|фолловинг)",
    r"(?:казино|1win|ставки|схемка|темка|ворк|легкие\s+деньги|сигналы\s+крипт)"
]

class FormStates(StatesGroup):
    waiting_for_prompt = State()
    waiting_for_custom_model = State()
    waiting_for_broadcast = State()

# 🧠 Ролевой промпт самосознания для личных сообщений с ботом
DIRECT_PROMPTS = {
    "ru": (
        "Ты — умный персональный Telegram-бот «Ai Assistant».\n"
        "Ты работаешь прямо внутри Telegram на базе современных нейросетей.\n\n"
        "ТВОЯ РОЛЬ И ВОЗМОЖНОСТИ:\n"
        "1. В этом личном чате: ты универсальный помощник. Отвечай на любые вопросы, пиши тексты, код, помогай с учебой и проектами.\n"
        "2. В бизнес-чатах: ты работаешь как умный авто-секретарь для Telegram Business (отвечаешь клиентам, фильтруешь спам, собираешь лиды).\n\n"
        "ЕСЛИ СПРАШИВАЮТ, КАК ПОДКЛЮЧИТЬ ТЕБЯ КАК СЕКРЕТАРЯ / В БИЗНЕС:\n"
        "Объясни пошагово:\n"
        "• Открыть Настройки Telegram ➔ «Telegram для бизнеса» ➔ «Чат-боты»\n"
        "• Ввести юзернейм этого бота и выдать разрешение отвечать на сообщения\n"
        "• В блоке «Чаты» выбрать «Все, кроме контактов»\n"
        "• Для настройки пресетов или выбора модели использовать команду /settings в этом чате.\n\n"
        "ПРАВИЛА ТЕКСТА: Пиши чистым текстом без Markdown-решёток. Списки оформляй аккуратно через маркер '• '."
    ),
    "en": (
        "You are 'Ai Assistant', a smart Telegram AI bot operating directly inside Telegram.\n\n"
        "YOUR ROLE & CAPABILITIES:\n"
        "1. In direct messages: Intelligent personal AI assistant ready to help with any task, code, or ideas.\n"
        "2. In business chats: Automated AI secretary for Telegram Business.\n\n"
        "IF ASKED HOW TO CONNECT AS A SECRETARY:\n"
        "Provide exact steps:\n"
        "• Telegram Settings ➔ 'Telegram for Business' ➔ 'Chatbots'\n"
        "• Add this bot's username and grant permissions\n"
        "• Under 'Chats', choose 'Exclude Contacts'\n"
        "• Use /settings in this chat to switch models or prompt presets.\n\n"
        "FORMATTING: Clean plain text with neat '• ' bullet points."
    )
}

PRESETS_RU = {
    "strict": (
        "Ты — строгий и бескомпромиссный ИИ-секретарь владельца этого аккаунта.\n\n"
        "КРИТИЧЕСКИЕ ПРАВИЛА (ПО ПРИОРИТЕТУ):\n"
        "1. СПАМ, ФЛУД, ПЕСНИ, МЕМЫ И ЧАТЫ (ВЫСШИЙ ПРИОРИТЕТ):\n"
        "   На любые предложения скинуть песни, видео, вступить в группы, беседы, игры или флуд отвечай ИСКЛЮЧИТЕЛЬНО:\n"
        "   «Пожалуйста, удалите этот чат и не тратьте наше время»\n\n"
        "2. ДЕЛОВЫЕ ЗАПРОСЫ:\n"
        "   • Если описана суть конкретной рабочей задачи или рекламы — подтверди приём: «Ваш запрос принят и передан владельцу аккаунта. Он свяжется с вами при необходимости.»\n"
        "   • Если написали только приветствие («Привет», «Шо ты», «Как дела») без темы — попроси изложить суть делового вопроса в одном сообщении.\n\n"
        "3. ПРАВИЛА ТЕКСТА: Чистый текст БЕЗ Markdown. Кратко (1-2 предложения).\n"
        f"4. ВСЕГДА в конце на новой строке добавляй:\nЭтот ответ сгенерирован AI ассистентом 🤖 Ai Assistant [{BOT_VERSION}]"
    ),
    "blogger": (
        "Ты — официальный ИИ-менеджер по рекламе блогера.\n\n"
        "1. На спам, мемы и флуд отвечай: «Пожалуйста, удалите этот чат и не тратьте наше время».\n"
        "2. РЕКЛАМНЫЕ ЗАПРОСЫ:\n"
        "   • Если нет деталей — запроси ссылку, формат, дату и бюджет списком через '• '.\n"
        "   • Если данные есть — подтверди получение заявки.\n"
        "3. Чистый текст БЕЗ Markdown.\n"
        f"4. ВСЕГДА в конце на новой строке добавляй:\nЭтот ответ сгенерирован AI ассистентом 🤖 Ai Assistant [{BOT_VERSION}]"
    ),
    "support": (
        "Ты — профессиональный ИИ-ассистент службы технической поддержки.\n\n"
        "1. СБОР ДИАГНОСТИКИ: Запрашивай суть проблемы, модель устройства и текст ошибки списком через '• '.\n"
        "2. Чистый текст БЕЗ Markdown.\n"
        f"3. ВСЕГДА в конце на новой строке добавляй:\nЭтот ответ сгенерирован AI ассистентом 🤖 Ai Assistant [{BOT_VERSION}]"
    ),
    "vacation": (
        "Ты — вежливый ИИ-секретарь. Владелец аккаунта сейчас в отпуске / офлайн.\n\n"
        "1. Сообщи, что передашь контакты владельцу по возвращении.\n"
        "2. Чистый текст БЕЗ Markdown.\n"
        f"3. ВСЕГДА в конце на новой строке добавляй:\nЭтот ответ сгенерирован AI ассистентом 🤖 Ai Assistant [{BOT_VERSION}]"
    ),
    "consultant": (
        "Ты — деловой ИИ-консультант владельца аккаунта.\n\n"
        "1. Отвечай по делу на вопросы о проектах.\n"
        "2. Чистый текст БЕЗ Markdown.\n"
        f"3. ВСЕГДА в конце на новой строке добавляй:\nЭтот ответ сгенерирован AI ассистентом 🤖 Ai Assistant [{BOT_VERSION}]"
    )
}

PRESETS_EN = {
    "strict": (
        "You are a strict AI secretary for the owner of this account.\n\n"
        "1. SPAM/OFFTOPIC: If spam, songs, memes, chats, reply ONLY: 'Please delete this chat and do not waste our time.'\n"
        "2. BUSINESS: Acknowledge details or ask to state in one message.\n"
        "3. FORMATTING: Plain text without markdown.\n"
        f"4. ALWAYS append: This reply was generated by AI assistant 🤖 Ai Assistant [{BOT_VERSION}]"
    ),
    "blogger": (
        "You are an AI Ads Manager.\n"
        "1. Spam/memes: Reply 'Please delete this chat and do not waste our time.'\n"
        "2. Ask for ad specs with '• ' bullets.\n"
        f"3. ALWAYS append: This reply was generated by AI assistant 🤖 Ai Assistant [{BOT_VERSION}]"
    ),
    "support": (
        "You are a Support AI. Collect issue details with bullets.\n"
        f"ALWAYS append: This reply was generated by AI assistant 🤖 Ai Assistant [{BOT_VERSION}]"
    ),
    "vacation": (
        "You are a polite AI secretary. The owner is offline.\n"
        f"ALWAYS append: This reply was generated by AI assistant 🤖 Ai Assistant [{BOT_VERSION}]"
    ),
    "consultant": (
        "You are a professional AI consultant.\n"
        f"ALWAYS append: This reply was generated by AI assistant 🤖 Ai Assistant [{BOT_VERSION}]"
    )
}

TEXTS = {
    "ru": {
        "start": "👋 <b>Привет! Я твой персональный ИИ-ассистент.</b>\n\n• В этом чате можно задавать мне любые вопросы.\n• Подключи меня в <b>Настройки → Telegram для бизнеса → Чат-боты</b>, чтобы я стал твоим секретарём!\n\n⚙️ Для настройки нажми /settings",
        "panel_title": "⚙️ <b>Панель управления ИИ-секретарём</b> ({role})",
        "role_owner": "👑 Создатель",
        "role_user": "👤 Пользователь",
        "cur_model": "📌 <b>Модель:</b> <code>{model}</code>",
        "cur_preset": "🎭 <b>Режим:</b> {preset}",
        "btn_model": "🤖 Выбрать модель",
        "btn_presets": "🎭 Пресеты промпта",
        "btn_advanced": "⚙️ Доп. настройки",
        "btn_lang": "🌐 Язык (RU/EN)",
        "btn_about": "📖 О боте и справка",
        "btn_stats": "📊 Дайджест и статистика",
        "btn_users": "👥 Пользователи",
        "btn_broadcast": "📢 Рассылка",
        "btn_back": "« Назад",
        "btn_cancel": "❌ Отмена",
        "choose_lang_title": "🌐 <b>Выберите язык интерфейса:</b>",
        "lang_set": "✅ Язык переключён на <b>Русский</b>!",
        "model_choose_title": "Выберите модель для работы:",
        "model_set": "✅ Модель: <code>{model}</code>",
        "custom_model_prompt": "✍️ <b>Введите точный ID модели с OpenRouter:</b>",
        "custom_model_set": "✅ <b>Модель изменена на:</b> <code>{model}</code>",
        "presets_menu_title": "🎭 <b>Выберите готовый режим или настройте свой:</b>",
        "preset_set": "✅ Активирован режим: <b>{name}</b>",
        "preset_strict": "💼 Строгий секретарь",
        "preset_blogger": "📢 Менеджер рекламы",
        "preset_support": "🛠 Техподдержка",
        "preset_vacation": "🏖 В отпуске / Сплю",
        "preset_consultant": "🤝 Консультант",
        "preset_custom": "✏️ Свой промпт",
        "prompt_edit_title": "✏️ <b>Отправьте новый System Prompt сообщением:</b>",
        "prompt_set": "✅ <b>System Prompt успешно обновлён!</b>",
        "stats_title": "📊 <b>Дайджест и статистика секретаря:</b>\n\n📨 <b>Обработано сообщений:</b> {msg_count}\n🪙 <b>Потрачено токенов (прим.):</b> {tokens}\n🚫 <b>Отсеяно спама:</b> {spam_count}\n\n📋 <b>Последние контакты:</b>\n{recent_leads}",
        "stats_no_leads": "<i>Обращений пока не зафиксировано.</i>",
        "users_title": "👥 <b>Список зарегистрированных пользователей ({count}):</b>\n\n",
        "users_empty": "👥 Пользователей пока нет.",
        "broadcast_title": "📢 <b>Рассылка сообщений</b>\n\nОтправьте сообщение (текст или фото с подписью), которое увидят все пользователи бота:",
        "broadcast_empty": "❌ Нет зарегистрированных пользователей для рассылки.",
        "broadcast_started": "⏳ <i>Начинаю рассылку для {count} пользователей...</i>",
        "broadcast_done": "✅ <b>Рассылка завершена!</b>\n\nУспешно: <b>{success}</b>\nОшибок: <b>{failed}</b>",
        "about_text": (
            f"📖 <b>Справка по боту [{BOT_VERSION}]</b>\n\n"
            "🤖 <b>Возможности:</b>\n"
            "• Потоковая печать ответов (Live Stream) в личных сообщениях.\n"
            "• Автоматический умный секретарь для Telegram Business.\n"
            "• <b>Human Takeover:</b> при ответе владельца бот засыпает в чате на 3 часа.\n"
            "• <b>Умный антиспам:</b> мгновенный отсев нецелевого флуда без отправки в ЛС."
        ),
        "btn_summary": "📋 Выжимка",
        "btn_menu_new": "⚙️ Меню",
        "summary_loading": "⏳ <i>Анализирую диалог и составляю бриф...</i>",
        "summary_empty": "❌ В памяти пока нет сообщений из этого диалога для анализа.",
        "summary_header": "📋 <b>Карточка сделки</b>\n\n",
        "btn_mute": "🛑 Мут 24ч",
        "btn_unmute": "▶️ Снять мут",
        "btn_clear": "🧹 Сброс",
        "btn_block": "⛔️ В ЧС",
        "muted_msg": "🛑 Бот заглушен в этом чате на <b>24 часа</b>.",
        "unmuted_msg": "▶️ Мут снят! Бот снова отвечает в чате.",
        "cleared_msg": "🧹 Память диалога очищена!",
        "blocked_msg": "⛔️ Пользователь добавлен в <b>Чёрный список</b>.",
        "notify_title": "📩 <b>Новое обращение</b>\n👤 <b>От:</b> {user_info} ({username})\n\n💬 <b>Запрос:</b>\n<blockquote>{user_text}</blockquote>\n🤖 <b>Ответ ИИ:</b>\n<blockquote expandable>{ai_reply}</blockquote>",
        "ai_error": "Здравствуйте! Произошла временная ошибка, попробуйте чуть позже."
    },
    "en": {
        "start": "👋 <b>Hello! I am your personal AI assistant.</b>\n\n• Chat directly with me here.\n• Connect me in <b>Settings → Telegram for Business → Chatbots</b>!\n\n⚙️ To configure, type /settings",
        "panel_title": "⚙️ <b>AI Secretary Control Panel</b> ({role})",
        "role_owner": "👑 Creator",
        "role_user": "👤 User",
        "cur_model": "📌 <b>Model:</b> <code>{model}</code>",
        "cur_preset": "🎭 <b>Mode:</b> {preset}",
        "btn_model": "🤖 Select Model",
        "btn_presets": "🎭 Prompt Presets",
        "btn_advanced": "⚙️ Advanced",
        "btn_lang": "🌐 Language (RU/EN)",
        "btn_about": "📖 About & Guide",
        "btn_stats": "📊 Digest & Stats",
        "btn_users": "👥 User List",
        "btn_broadcast": "📢 Broadcast",
        "btn_back": "« Back",
        "btn_cancel": "❌ Cancel",
        "choose_lang_title": "🌐 <b>Choose interface language:</b>",
        "lang_set": "✅ Language set to <b>English</b>!",
        "model_choose_title": "Select a model:",
        "model_set": "✅ Model: <code>{model}</code>",
        "custom_model_prompt": "✍️ <b>Enter exact OpenRouter Model ID:</b>",
        "custom_model_set": "✅ <b>Model set to:</b> <code>{model}</code>",
        "presets_menu_title": "🎭 <b>Select a preset or edit custom prompt:</b>",
        "preset_set": "✅ Mode activated: <b>{name}</b>",
        "preset_strict": "💼 Strict Secretary",
        "preset_blogger": "📢 Ads & PR Manager",
        "preset_support": "🛠 Tech Support",
        "preset_vacation": "🏖 Vacation / Away",
        "preset_consultant": "🤝 Consultant",
        "preset_custom": "✏️ Custom Prompt",
        "prompt_edit_title": "✏️ <b>Send your new System Prompt:</b>",
        "prompt_set": "✅ <b>System Prompt updated!</b>",
        "stats_title": "📊 <b>Secretary Digest & Stats:</b>\n\n📨 <b>Processed messages:</b> {msg_count}\n🪙 <b>Approx. tokens:</b> {tokens}\n🚫 <b>Spam filtered:</b> {spam_count}\n\n📋 <b>Recent inquiries:</b>\n{recent_leads}",
        "stats_no_leads": "<i>No inquiries recorded yet.</i>",
        "users_title": "👥 <b>Registered Users ({count}):</b>\n\n",
        "users_empty": "👥 No users registered yet.",
        "broadcast_title": "📢 <b>Broadcast Message</b>\n\nSend a message to broadcast to all bot users:",
        "broadcast_empty": "❌ No registered users to broadcast to.",
        "broadcast_started": "⏳ <i>Starting broadcast for {count} users...</i>",
        "broadcast_done": "✅ <b>Broadcast finished!</b>\n\nSuccess: <b>{success}</b>\nFailed: <b>{failed}</b>",
        "about_text": (
            f"📖 <b>Bot Guide [{BOT_VERSION}]</b>\n\n"
            "🤖 <b>Features:</b>\n"
            "• Live Streaming AI replies in direct messages.\n"
            "• Automated AI secretary for Telegram Business.\n"
            "• <b>Human Takeover:</b> Mutes bot for 3h when owner replies.\n"
            "• <b>Smart Anti-Spam:</b> Discards spam without flooding owner DM."
        ),
        "btn_summary": "📋 Summary",
        "btn_menu_new": "⚙️ Menu",
        "summary_loading": "⏳ <i>Analyzing dialogue and compiling brief...</i>",
        "summary_empty": "❌ No messages found in memory for this dialog.",
        "summary_header": "📋 <b>Deal Summary Card</b>\n\n",
        "btn_mute": "🛑 Mute 24h",
        "btn_unmute": "▶️ Unmute",
        "btn_clear": "🧹 Clear",
        "btn_block": "⛔️ Block",
        "muted_msg": "🛑 Bot is muted in this chat for <b>24 hours</b>.",
        "unmuted_msg": "▶️ Unmuted! Bot is active again in this chat.",
        "cleared_msg": "🧹 Memory cleared!",
        "blocked_msg": "⛔️ User added to <b>Blacklist</b>.",
        "notify_title": "📩 <b>New message</b>\n👤 <b>From:</b> {user_info} ({username})\n\n💬 <b>Text:</b>\n<blockquote>{user_text}</blockquote>\n🤖 <b>AI Reply:</b>\n<blockquote expandable>{ai_reply}</blockquote>",
        "ai_error": "Hello! A temporary error occurred, please try again later."
    }
}

def t(key: str, lang: str = "ru", **kwargs) -> str:
    lang_dict = TEXTS.get(lang, TEXTS["ru"])
    msg = lang_dict.get(key, key)
    if kwargs:
        return msg.format(**kwargs)
    return msg

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
                 (user_id INTEGER PRIMARY KEY, model TEXT, system_prompt TEXT, lang TEXT, preset TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS blacklist 
                 (owner_id INTEGER, blocked_id INTEGER, PRIMARY KEY (owner_id, blocked_id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS registered_users 
                 (user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS business_conns 
                 (conn_id TEXT PRIMARY KEY, user_id INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS stats 
                 (user_id INTEGER PRIMARY KEY, msg_count INTEGER DEFAULT 0, tokens INTEGER DEFAULT 0, spam_count INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS leads 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER, chat_id INTEGER, user_info TEXT, username TEXT, snippet TEXT, timestamp INTEGER)''')
    
    for col in ["username", "full_name"]:
        try:
            c.execute(f"ALTER TABLE registered_users ADD COLUMN {col} TEXT")
        except Exception:
            pass
    for col in ["lang", "preset"]:
        try:
            c.execute(f"ALTER TABLE user_configs ADD COLUMN {col} TEXT")
        except Exception:
            pass
            
    conn.commit()
    conn.close()

init_db()

def log_stat(owner_id: int, tokens: int = 0, is_spam: bool = False):
    conn = sqlite3.connect("bot_config.db")
    c = conn.cursor()
    spam_inc = 1 if is_spam else 0
    c.execute('''INSERT INTO stats (user_id, msg_count, tokens, spam_count) VALUES (?, 1, ?, ?)
                 ON CONFLICT(user_id) DO UPDATE SET 
                 msg_count = msg_count + 1,
                 tokens = tokens + excluded.tokens,
                 spam_count = spam_count + excluded.spam_count''', (owner_id, tokens, spam_inc))
    conn.commit()
    conn.close()

def log_lead(owner_id: int, chat_id: int, user_info: str, username: str, snippet: str):
    conn = sqlite3.connect("bot_config.db")
    c = conn.cursor()
    c.execute('''INSERT INTO leads (owner_id, chat_id, user_info, username, snippet, timestamp)
                 VALUES (?, ?, ?, ?, ?, ?)''', (owner_id, chat_id, user_info, username, snippet[:120], int(time.time())))
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
    c.execute('''INSERT INTO registered_users (user_id, username, full_name) 
                 VALUES (?, ?, ?) 
                 ON CONFLICT(user_id) DO UPDATE SET 
                 username = excluded.username, 
                 full_name = excluded.full_name''', 
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

PUBLIC_MODELS = [
    ("🔹 DeepSeek V3", "deepseek/deepseek-chat"),
    ("🔹 Gemini 2.0 Flash", "google/gemini-2.0-flash-001"),
    ("🔹 Llama 3.3 70B (Meta)", "meta-llama/llama-3.3-70b-instruct"),
    ("🔹 Llama 3.1 8B (Meta)", "meta-llama/llama-3.1-8b-instruct"),
]

OWNER_EXTRA_MODELS = [
    ("👑 Claude 3.5 Sonnet", "anthropic/claude-3.5-sonnet"),
    ("👑 DeepSeek R1", "deepseek/deepseek-r1"),
]

def get_user_config(user_id: int):
    conn = sqlite3.connect("bot_config.db")
    c = conn.cursor()
    c.execute("SELECT model, system_prompt, lang, preset FROM user_configs WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        model = row[0] or "deepseek/deepseek-chat"
        lang = row[2] or "ru"
        preset = row[3] or "strict"
        presets_dict = PRESETS_EN if lang == "en" else PRESETS_RU
        if preset in presets_dict:
            prompt = presets_dict[preset]
        else:
            prompt = row[1] or presets_dict["strict"]
        return model, prompt, lang, preset
    return "deepseek/deepseek-chat", PRESETS_RU["strict"], "ru", "strict"

def update_user_model(user_id: int, new_model: str):
    _, prompt, lang, preset = get_user_config(user_id)
    conn = sqlite3.connect("bot_config.db")
    c = conn.cursor()
    c.execute('''INSERT INTO user_configs (user_id, model, system_prompt, lang, preset) 
                 VALUES (?, ?, ?, ?, ?) 
                 ON CONFLICT(user_id) DO UPDATE SET model = excluded.model''', 
              (user_id, new_model, prompt, lang, preset))
    conn.commit()
    conn.close()

def update_user_prompt(user_id: int, new_prompt: str):
    model, _, lang, _ = get_user_config(user_id)
    conn = sqlite3.connect("bot_config.db")
    c = conn.cursor()
    c.execute('''INSERT INTO user_configs (user_id, model, system_prompt, lang, preset) 
                 VALUES (?, ?, ?, ?, ?) 
                 ON CONFLICT(user_id) DO UPDATE SET system_prompt = excluded.system_prompt, preset = excluded.preset''', 
              (user_id, model, new_prompt, lang, "custom"))
    conn.commit()
    conn.close()

def update_user_preset(user_id: int, preset_key: str):
    model, _, lang, _ = get_user_config(user_id)
    presets_dict = PRESETS_EN if lang == "en" else PRESETS_RU
    new_prompt = presets_dict.get(preset_key, presets_dict["strict"])
    conn = sqlite3.connect("bot_config.db")
    c = conn.cursor()
    c.execute('''INSERT INTO user_configs (user_id, model, system_prompt, lang, preset) 
                 VALUES (?, ?, ?, ?, ?) 
                 ON CONFLICT(user_id) DO UPDATE SET system_prompt = excluded.system_prompt, preset = excluded.preset''', 
              (user_id, model, new_prompt, lang, preset_key))
    conn.commit()
    conn.close()

def update_user_lang(user_id: int, new_lang: str):
    model, prompt, _, preset = get_user_config(user_id)
    if preset in ("strict", "blogger", "support", "vacation", "consultant"):
        presets_dict = PRESETS_EN if new_lang == "en" else PRESETS_RU
        prompt = presets_dict[preset]
    conn = sqlite3.connect("bot_config.db")
    c = conn.cursor()
    c.execute('''INSERT INTO user_configs (user_id, model, system_prompt, lang, preset) 
                 VALUES (?, ?, ?, ?, ?) 
                 ON CONFLICT(user_id) DO UPDATE SET lang = excluded.lang, system_prompt = excluded.system_prompt''', 
              (user_id, model, prompt, new_lang, preset))
    conn.commit()
    conn.close()

def is_blacklisted(owner_id: int, user_id: int) -> bool:
    conn = sqlite3.connect("bot_config.db")
    c = conn.cursor()
    c.execute("SELECT 1 FROM blacklist WHERE owner_id = ? AND blocked_id = ?", (owner_id, user_id))
    row = c.fetchone()
    conn.close()
    return bool(row)

def add_blacklist(owner_id: int, user_id: int):
    conn = sqlite3.connect("bot_config.db")
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO blacklist (owner_id, blocked_id) VALUES (?, ?)", (owner_id, user_id))
    conn.commit()
    conn.close()

def get_menu_keyboard(is_owner: bool, lang: str):
    kb = [
        [InlineKeyboardButton(text=t("btn_model", lang), callback_data="choose_model")],
        [InlineKeyboardButton(text=t("btn_presets", lang), callback_data="open_presets")],
        [InlineKeyboardButton(text=t("btn_stats", lang), callback_data="view_stats")],
        [InlineKeyboardButton(text=t("btn_advanced", lang), callback_data="open_advanced")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_advanced_keyboard(is_owner: bool, lang: str):
    kb = [
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
        [
            InlineKeyboardButton(text=t("preset_strict", lang), callback_data="set_preset:strict"),
            InlineKeyboardButton(text=t("preset_blogger", lang), callback_data="set_preset:blogger")
        ],
        [
            InlineKeyboardButton(text=t("preset_support", lang), callback_data="set_preset:support"),
            InlineKeyboardButton(text=t("preset_consultant", lang), callback_data="set_preset:consultant")
        ],
        [
            InlineKeyboardButton(text=t("preset_vacation", lang), callback_data="set_preset:vacation"),
            InlineKeyboardButton(text=t("preset_custom", lang), callback_data="edit_prompt")
        ],
        [InlineKeyboardButton(text=t("btn_back", lang), callback_data="back_menu")]
    ])

def get_lang_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🇷🇺 Русский", callback_data="set_lang:ru"),
            InlineKeyboardButton(text="🇬🇧 English", callback_data="set_lang:en")
        ],
        [InlineKeyboardButton(text="« Back / Назад", callback_data="open_advanced")]
    ])

def get_models_keyboard(is_owner: bool, lang: str):
    kb = []
    for title, m_id in PUBLIC_MODELS:
        kb.append([InlineKeyboardButton(text=title, callback_data=f"set_model:{m_id}")])
    if is_owner:
        for title, m_id in OWNER_EXTRA_MODELS:
            kb.append([InlineKeyboardButton(text=title, callback_data=f"set_model:{m_id}")])
        kb.append([InlineKeyboardButton(text="✍️ Custom Model ID", callback_data="custom_model")])
    kb.append([InlineKeyboardButton(text=t("btn_back", lang), callback_data="back_menu")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_notify_keyboard(chat_id: int, sender_id: int, is_paused: bool = False, lang: str = "ru"):
    pause_btn = InlineKeyboardButton(text=t("btn_unmute", lang), callback_data=f"unmute:{chat_id}") if is_paused else InlineKeyboardButton(text=t("btn_mute", lang), callback_data=f"mute:{chat_id}")
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            pause_btn,
            InlineKeyboardButton(text=t("btn_clear", lang), callback_data=f"clear:{chat_id}")
        ],
        [
            InlineKeyboardButton(text=t("btn_summary", lang), callback_data=f"summary:{chat_id}"),
            InlineKeyboardButton(text=t("btn_block", lang), callback_data=f"block:{sender_id}:{chat_id}")
        ],
        [
            InlineKeyboardButton(text=t("btn_menu_new", lang), callback_data="send_new_menu")
        ]
    ])

async def show_panel(message: types.Message, user_id: int):
    is_owner = (user_id == OWNER_ID)
    model, _, lang, preset = get_user_config(user_id)
    role_badge = t("role_owner", lang) if is_owner else t("role_user", lang)
    preset_name = t(f"preset_{preset}", lang) if f"preset_{preset}" in TEXTS[lang] else t("preset_custom", lang)
    
    text = (
        f"{t('panel_title', lang, role=role_badge)}\n\n"
        f"{t('cur_model', lang, model=model)}\n"
        f"{t('cur_preset', lang, preset=preset_name)}"
    )
    try:
        await message.edit_text(text, reply_markup=get_menu_keyboard(is_owner, lang), parse_mode="HTML")
    except Exception:
        await message.answer(text, reply_markup=get_menu_keyboard(is_owner, lang), parse_mode="HTML")

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    register_user(message.from_user)
    _, _, lang, _ = get_user_config(message.from_user.id)
    await message.answer(t("start", lang), parse_mode="HTML")

@dp.message(Command("settings", "admin"))
async def cmd_settings(message: types.Message, state: FSMContext = None):
    register_user(message.from_user)
    if state:
        await state.clear()
    await show_panel(message, message.from_user.id)

@dp.callback_query()
async def handle_callbacks(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    uid = call.from_user.id
    is_owner = (uid == OWNER_ID)
    model, prompt, lang, preset = get_user_config(uid)
    data = call.data
    
    if data == "choose_model":
        await call.message.edit_text(t("model_choose_title", lang), reply_markup=get_models_keyboard(is_owner, lang))
    elif data.startswith("set_model:"):
        new_model = data.split("set_model:")[1]
        update_user_model(uid, new_model)
        await call.message.answer(t("model_set", lang, model=new_model), parse_mode="HTML")
        await show_panel(call.message, uid)
    elif data == "open_presets":
        await call.message.edit_text(t("presets_menu_title", lang), reply_markup=get_presets_keyboard(lang), parse_mode="HTML")
    elif data.startswith("set_preset:"):
        p_key = data.split("set_preset:")[1]
        update_user_preset(uid, p_key)
        p_name = t(f"preset_{p_key}", lang)
        await call.message.answer(t("preset_set", lang, name=p_name), parse_mode="HTML")
        await show_panel(call.message, uid)
    elif data == "open_advanced":
        await call.message.edit_text(t("panel_title", lang, role=t("role_owner", lang) if is_owner else t("role_user", lang)), reply_markup=get_advanced_keyboard(is_owner, lang), parse_mode="HTML")
    elif data == "view_about":
        back_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t("btn_back", lang), callback_data="open_advanced")]])
        await call.message.edit_text(t("about_text", lang), reply_markup=back_kb, parse_mode="HTML")
    elif data == "view_stats":
        msg_count, tokens, spam_count, leads = get_stats_data(uid)
        if not leads:
            leads_str = t("stats_no_leads", lang)
        else:
            leads_str = "\n".join([f"• <b>{l[0]}</b> ({'@'+l[1] if l[1] else 'no tag'}): <i>{l[2]}</i>" for l in leads])
        text = t("stats_title", lang, msg_count=msg_count, tokens=tokens, spam_count=spam_count, recent_leads=leads_str)
        back_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t("btn_back", lang), callback_data="back_menu")]])
        await call.message.edit_text(text, reply_markup=back_kb, parse_mode="HTML")
    elif data == "choose_lang":
        await call.message.edit_text(t("choose_lang_title", lang), reply_markup=get_lang_keyboard(), parse_mode="HTML")
    elif data.startswith("set_lang:"):
        new_lang = data.split("set_lang:")[1]
        update_user_lang(uid, new_lang)
        await call.message.answer(t("lang_set", new_lang), parse_mode="HTML")
        await show_panel(call.message, uid)
    elif data == "custom_model" and is_owner:
        await state.set_state(FormStates.waiting_for_custom_model)
        cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t("btn_cancel", lang), callback_data="cancel_action")]])
        await call.message.edit_text(t("custom_model_prompt", lang), reply_markup=cancel_kb, parse_mode="HTML")
    elif data == "edit_prompt":
        await state.set_state(FormStates.waiting_for_prompt)
        cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t("btn_cancel", lang), callback_data="cancel_action")]])
        await call.message.edit_text(t("prompt_edit_title", lang), reply_markup=cancel_kb, parse_mode="HTML")
    elif data == "start_broadcast" and is_owner:
        await state.set_state(FormStates.waiting_for_broadcast)
        cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t("btn_cancel", lang), callback_data="cancel_action")]])
        await call.message.edit_text(t("broadcast_title", lang), reply_markup=cancel_kb, parse_mode="HTML")
    elif data == "list_users" and is_owner:
        users = get_detailed_users()
        if not users:
            text = t("users_empty", lang)
        else:
            text = t("users_title", lang, count=len(users))
            for i, (u_id, u_name, f_name) in enumerate(users, 1):
                user_tag = f"@{u_name}" if u_name else "no username"
                text += f"{i}. <b>{f_name}</b> ({user_tag}) — <code>{u_id}</code>\n"
        back_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t("btn_back", lang), callback_data="open_advanced")]])
        await call.message.edit_text(text, reply_markup=back_kb, parse_mode="HTML")
    elif data in ("cancel_action", "back_menu"):
        await state.clear()
        await show_panel(call.message, uid)
    
    elif data == "send_new_menu":
        role_badge = t("role_owner", lang) if is_owner else t("role_user", lang)
        preset_name = t(f"preset_{preset}", lang) if f"preset_{preset}" in TEXTS[lang] else t("preset_custom", lang)
        text = (
            f"{t('panel_title', lang, role=role_badge)}\n\n"
            f"{t('cur_model', lang, model=model)}\n"
            f"{t('cur_preset', lang, preset=preset_name)}"
        )
        await bot.send_message(uid, text, reply_markup=get_menu_keyboard(is_owner, lang), parse_mode="HTML")

    elif data.startswith("summary:"):
        target_chat = int(data.split(":")[1])
        history = list(chat_history[target_chat])
        if not history:
            await call.message.reply(t("summary_empty", lang))
            return
        
        status_msg = await call.message.reply(t("summary_loading", lang), parse_mode="HTML")
        dialog_text = "\n".join([f"{'Клиент' if m['role']=='user' else 'Бот'}: {m['content']}" for m in history])
        if lang == "en":
            summary_prompt = (
                "You are an AI Lead Analyst. Analyze this dialogue and create a clean deal card.\n"
                "DO NOT use Markdown. Use ONLY HTML tags (<b>bold</b>).\n\n"
                "Format:\n"
                "🎯 <b>Goal / Request:</b> [details]\n"
                "💰 <b>Budget:</b> [details]\n"
                "⏳ <b>Deadline:</b> [details]\n"
                "📌 <b>Key Details:</b> [details]"
            )
        else:
            summary_prompt = (
                "Ты — ИИ-аналитик лидов. Проанализируй диалог и сформируй красивую карточку сделки.\n"
                "КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать Markdown разметку.\n"
                "Используй ТОЛЬКО HTML теги <b>жирный текст</b> для заголовков.\n\n"
                "Формат вывода строго такой:\n"
                "🎯 <b>Услуга / Задача:</b> [суть]\n"
                "💰 <b>Бюджет:</b> [сумма]\n"
                "⏳ <b>Сроки / Дедлайн:</b> [сроки]\n"
                "📌 <b>Ключевые детали:</b> [описание и ссылки обычным текстом]"
            )
        
        lead_summary, _ = await ask_openrouter(call.message.chat.id, f"Диалог:\n{dialog_text}", custom_system_prompt=summary_prompt, model=model, lang=lang)
        cleaned_summary = format_to_tg_html(lead_summary)
        cleaned_summary = dlp_sanitize(cleaned_summary)
        await status_msg.edit_text(f"{t('summary_header', lang)}{cleaned_summary}", parse_mode="HTML")

    elif data.startswith("mute:"):
        target_chat = int(data.split(":")[1])
        paused_chats[target_chat] = time.time() + 86400
        await call.message.edit_reply_markup(reply_markup=get_notify_keyboard(target_chat, uid, is_paused=True, lang=lang))
        await call.message.reply(t("muted_msg", lang), parse_mode="HTML")
    elif data.startswith("unmute:"):
        target_chat = int(data.split(":")[1])
        paused_chats.pop(target_chat, None)
        await call.message.edit_reply_markup(reply_markup=get_notify_keyboard(target_chat, uid, is_paused=False, lang=lang))
        await call.message.reply(t("unmuted_msg", lang), parse_mode="HTML")
    elif data.startswith("clear:"):
        target_chat = int(data.split(":")[1])
        chat_history[target_chat].clear()
        chat_rate_limits[target_chat].clear()
        await call.message.reply(t("cleared_msg", lang), parse_mode="HTML")
    elif data.startswith("block:"):
        _, target_user, target_chat = data.split(":")
        add_blacklist(uid, int(target_user))
        paused_chats[int(target_chat)] = time.time() + 315360000
        await call.message.reply(t("blocked_msg", lang), parse_mode="HTML")

@dp.message(FormStates.waiting_for_prompt)
async def process_new_prompt(message: types.Message, state: FSMContext):
    _, _, lang, _ = get_user_config(message.from_user.id)
    update_user_prompt(message.from_user.id, message.text)
    await state.clear()
    await message.answer(t("prompt_set", lang), parse_mode="HTML")
    await show_panel(message, message.from_user.id)

@dp.message(FormStates.waiting_for_custom_model)
async def process_custom_model(message: types.Message, state: FSMContext):
    _, _, lang, _ = get_user_config(message.from_user.id)
    if message.from_user.id == OWNER_ID:
        update_user_model(message.from_user.id, message.text.strip())
        await message.answer(t("custom_model_set", lang, model=message.text.strip()), parse_mode="HTML")
    await state.clear()
    await show_panel(message, message.from_user.id)

@dp.message(FormStates.waiting_for_broadcast)
async def process_broadcast(message: types.Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return
    _, _, lang, _ = get_user_config(message.from_user.id)
    await state.clear()
    users = get_all_users()
    if not users:
        await message.answer(t("broadcast_empty", lang))
        await show_panel(message, message.from_user.id)
        return
    
    status_msg = await message.answer(t("broadcast_started", lang, count=len(users)), parse_mode="HTML")
    success = 0
    failed = 0
    
    for uid in users:
        try:
            if message.photo:
                photo_id = message.photo[-1].file_id
                await bot.send_photo(uid, photo=photo_id, caption=message.caption or "", parse_mode="HTML")
            elif message.text:
                await bot.send_message(uid, text=message.text, parse_mode="HTML")
            success += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1
            
    await status_msg.edit_text(t("broadcast_done", lang, success=success, failed=failed), parse_mode="HTML")
    await show_panel(message, message.from_user.id)

# 🌐 Нестриминговый вызов для бизнес-чатов и выжимок
async def ask_openrouter(chat_id: int, user_text: str, custom_system_prompt: str = None, model: str = None, lang: str = "ru"):
    active_model = model or "deepseek/deepseek-chat"
    active_prompt = custom_system_prompt or PRESETS_RU["strict"]
    clean_text = user_text[:800].strip()
    
    chat_history[chat_id].append({"role": "user", "content": clean_text})
    messages = [{"role": "system", "content": active_prompt}] + list(chat_history[chat_id])
    
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": active_model,
        "messages": messages,
        "max_tokens": 600
    }
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload) as resp:
                data = await resp.json()
                reply = data["choices"][0]["message"]["content"]
                usage = data.get("usage", {}).get("total_tokens", 100)
                reply = dlp_sanitize(reply)
                chat_history[chat_id].append({"role": "assistant", "content": reply})
                return reply, usage
        except Exception as e:
            logging.error(f"OpenRouter error: {e}")
            return t("ai_error", lang), 0

# ⚡️ ПЛАВНЫЙ СТРИМИНГ (Typewriter Effect) ДЛЯ ЛИЧНЫХ СООБЩЕНИЙ
async def ask_openrouter_stream(target_message: types.Message, user_text: str, custom_system_prompt: str = None, model: str = None, lang: str = "ru"):
    active_model = model or "deepseek/deepseek-chat"
    active_prompt = custom_system_prompt or PRESETS_RU["strict"]
    clean_text = user_text[:800].strip()
    chat_id = target_message.chat.id
    
    chat_history[chat_id].append({"role": "user", "content": clean_text})
    messages = [{"role": "system", "content": active_prompt}] + list(chat_history[chat_id])
    
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": active_model,
        "messages": messages,
        "max_tokens": 800,
        "stream": True
    }
    
    sent_msg = await target_message.answer("...")
    full_text = ""
    last_rendered_text = ""
    last_edit_time = time.time()
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload) as resp:
                if resp.status != 200:
                    await sent_msg.edit_text(t("ai_error", lang))
                    return t("ai_error", lang), 0
                
                async for line in resp.content:
                    line_str = line.decode("utf-8").strip()
                    if not line_str.startswith("data:"):
                        continue
                    data_part = line_str[5:].strip()
                    if data_part == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_part)
                        delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                        if delta:
                            full_text += delta
                            now = time.time()
                            # Плавная допечатка каждые 0.35с без курсоров, чтобы Telegram анимировал появление букв
                            if now - last_edit_time >= 0.35 and len(full_text) - len(last_rendered_text) >= 8:
                                clean_chunk = strip_markdown(full_text)
                                try:
                                    await sent_msg.edit_text(clean_chunk)
                                    last_rendered_text = full_text
                                    last_edit_time = now
                                except Exception:
                                    pass
                    except Exception:
                        continue
        except Exception as e:
            logging.error(f"Stream error: {e}")
            
    if not full_text:
        await sent_msg.edit_text(t("ai_error", lang))
        return t("ai_error", lang), 0
    
    # Финальное применение HTML разметки и DLP-фильтра
    formatted = format_to_tg_html(full_text)
    formatted = dlp_sanitize(formatted)
    chat_history[chat_id].append({"role": "assistant", "content": formatted})
    
    try:
        await sent_msg.edit_text(formatted, parse_mode="HTML")
    except Exception:
        await sent_msg.edit_text(strip_markdown(full_text))
        
    approx_tokens = len(full_text) // 3
    return formatted, approx_tokens

@dp.business_connection()
async def handle_business_connection(conn: types.BusinessConnection):
    register_user(conn.user)
    if conn.is_enabled:
        save_conn_owner(conn.id, conn.user.id)
    else:
        remove_conn(conn.id)

@dp.business_message()
async def handle_business(message: types.Message):
    if not message.text:
        return
    
    if message.message_id in processed_msg_ids:
        return
    processed_msg_ids.append(message.message_id)

    chat_id = message.chat.id
    conn_id = message.business_connection_id
    current_owner = get_conn_owner(conn_id)
    now = time.time()
    
    # 🛑 HUMAN TAKEOVER: если пишет владелец — глушим бота в этом чате на 3 часа
    if message.from_user.id != chat_id:
        save_conn_owner(conn_id, message.from_user.id)
        paused_chats[chat_id] = now + 10800
        return
    
    if is_blacklisted(current_owner, message.from_user.id):
        return
    if chat_id in paused_chats and now < paused_chats[chat_id]:
        return
    
    # 🛡 Антифлуд: 5 сообщений за 4 минуты
    recent_times = [ts for ts in chat_rate_limits[chat_id] if now - ts < 240]
    if len(recent_times) >= 5:
        return
    chat_rate_limits[chat_id].append(now)

    # 🛡 Защита от зацикливания ботов: 8 автоответов подряд
    bot_replies_count = sum(1 for m in chat_history[chat_id] if m["role"] == "assistant")
    if bot_replies_count >= 8:
        paused_chats[chat_id] = now + 86400
        return

    if chat_id in active_chat_locks:
        return
    active_chat_locks.add(chat_id)
    
    try:
        user_text = message.text
        user_text_lower = user_text.lower()
        model, system_prompt, lang, _ = get_user_config(current_owner)

        # 🛑 УМНЫЙ АНТИСПАМ-ФИЛЬТР (отсекает спам без вызова OpenRouter, пропуская коммерцию)
        has_business_intent = any(w in user_text_lower for w in BUSINESS_INTENT_WORDS)
        is_hard_spam = any(re.search(pat, user_text_lower) for pat in SPAM_PHRASE_PATTERNS)

        if is_hard_spam and not has_business_intent:
            spam_reply = (
                f"Пожалуйста, удалите этот чат и не тратьте наше время\n\nЭтот ответ сгенерирован AI ассистентом 🤖 Ai Assistant [{BOT_VERSION}]"
                if lang == "ru" else
                f"Please delete this chat and do not waste our time\n\nThis reply was generated by AI assistant 🤖 Ai Assistant [{BOT_VERSION}]"
            )
            await message.answer(spam_reply)
            log_stat(current_owner, tokens=0, is_spam=True)
            return

        ai_reply, tokens_used = await ask_openrouter(chat_id, user_text, custom_system_prompt=system_prompt, model=model, lang=lang)
        
        formatted_reply = format_to_tg_html(ai_reply)
        formatted_reply = dlp_sanitize(formatted_reply)
        
        try:
            await message.answer(formatted_reply, parse_mode="HTML")
        except Exception:
            await message.answer(strip_markdown(ai_reply))
        
        is_spam = ("удалите этот чат" in ai_reply.lower() or 
                   "delete this chat" in ai_reply.lower() or 
                   "не тратьте наше время" in ai_reply.lower())
        
        log_stat(current_owner, tokens=tokens_used, is_spam=is_spam)
        
        if is_spam:
            return
            
        user_info = message.from_user.full_name or "Пользователь"
        username = message.from_user.username or ""
        log_lead(current_owner, chat_id, user_info, username, user_text)
        
        username_tag = f"@{username}" if username else f"ID {message.from_user.id}"
        notify_text = t("notify_title", lang, user_info=user_info, username=username_tag, user_text=user_text, ai_reply=formatted_reply)
        try:
            kb = get_notify_keyboard(chat_id, message.from_user.id, is_paused=False, lang=lang)
            await bot.send_message(current_owner, notify_text, reply_markup=kb, parse_mode="HTML")
        except Exception as e:
            logging.error(f"Notify error: {e}")
    finally:
        active_chat_locks.discard(chat_id)

@dp.message(F.text & ~F.text.startswith("/"))
async def handle_direct_text(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is not None:
        return

    if message.message_id in processed_msg_ids:
        return
    processed_msg_ids.append(message.message_id)

    register_user(message.from_user)
    model, _, lang, _ = get_user_config(message.from_user.id)
    assistant_prompt = DIRECT_PROMPTS.get(lang, DIRECT_PROMPTS["ru"])
    
    # ⚡️ Запуск потоковой анимации печати в личных сообщениях
    _, tokens_used = await ask_openrouter_stream(message, message.text, custom_system_prompt=assistant_prompt, model=model, lang=lang)
    log_stat(message.from_user.id, tokens=tokens_used, is_spam=False)

async def main():
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
