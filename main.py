# -*- coding: utf-8 -*-
# ALNUSIRY BOT { VIP } - Version 2.0
#
# Changelog:
# - Switched to Polygon.io for reliable market data.
# - Integrated TA-Lib for advanced candlestick pattern recognition (15 patterns).
# - Added a weighted scoring system for candlestick patterns.
# - Implemented a configurable multi-timeframe trend filter (None, M15, H1, M15+H1).
# - Added Fibonacci retracement levels as a signal strength factor.
# - Implemented a data collection system for future machine learning (trades_data.csv).
# - Added a /stats command to display performance metrics.
# - Implemented strategy profiles management (load settings from .json files).
# - Enhanced cancellation messages with precise reasons.
# - Added a Flask web server to comply with Render's "Web Service" requirements.

import logging
import json
import os
import asyncio
from datetime import datetime, timedelta, timezone
import pandas as pd
import requests
import ta
import talib
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ContextTypes, ConversationHandler, CallbackQueryHandler,
    PicklePersistence
)
from flask import Flask
import threading

# --- Constants and Global Variables ---
TOKEN = os.environ.get('TELEGRAM_TOKEN')
CHAT_ID_STR = os.environ.get('TELEGRAM_CHAT_ID')
CHAT_ID = int(CHAT_ID_STR) if CHAT_ID_STR else None
POLYGON_API_KEY = os.environ.get('POLYGON_API_KEY')

STRATEGIES_DIR = 'strategies'
TRADES_FILE = 'trades_data.csv'

# --- Logging Setup ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Flask Web Server for Render ---
app = Flask(__name__)
@app.route('/')
def index():
    return "Bot is running!"

# --- Default Settings ---
DEFAULT_SETTINGS = {
    'running': False, 
    'selected_pairs': [],
    'profile_name': "الافتراضي (متوازن)",
    'initial_confidence': 2,
    'final_confidence': 3,
    'macd_strategy': 'dynamic',
    'trend_filter_mode': 'M15',
    'indicator_params': {
        'rsi_period': 14, 'macd_fast': 12, 'macd_slow': 26, 'macd_signal': 9,
        'bollinger_period': 20, 'stochastic_period': 14, 'atr_period': 14, 'adx_period': 14,
        'm15_ema_period': 20, 'h1_ema_period': 50
    }
}

# --- Bot State and Signal Memory ---
bot_state = {}
pending_signals = {}
trade_follow_ups = {}

# --- Helper & Utility Functions ---
async def send_error_to_telegram(context: ContextTypes.DEFAULT_TYPE, error_message: str):
    logger.error(error_message)
    if CHAT_ID:
        try:
            await context.bot.send_message(chat_id=CHAT_ID, text=f"🤖⚠️ **حدث خطأ في البوت** ⚠️🤖\n\n**التفاصيل:**\n`{error_message}`", parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Could not send error message to Telegram: {e}")

def save_bot_settings():
    with open('bot_state.json', 'w') as f:
        json.dump(bot_state, f, indent=4)
    logger.info("Bot state saved.")

def load_bot_settings():
    global bot_state
    try:
        with open('bot_state.json', 'r') as f:
            bot_state = json.load(f)
        logger.info("Bot state loaded from file.")
    except (FileNotFoundError, json.JSONDecodeError):
        logger.warning("Bot state file not found or invalid. Loading default profile.")
        load_strategy_profile('default.json')

def load_strategy_profile(profile_filename: str) -> bool:
    global bot_state
    filepath = os.path.join(STRATEGIES_DIR, profile_filename)
    try:
        with open(filepath, 'r') as f:
            profile_settings = json.load(f)
        
        running_status = bot_state.get('running', False)
        selected_pairs = bot_state.get('selected_pairs', [])
        
        bot_state = profile_settings.copy()
        bot_state['running'] = running_status
        bot_state['selected_pairs'] = selected_pairs
        
        save_bot_settings()
        logger.info(f"Successfully loaded strategy profile: {profile_filename}")
        return True
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.error(f"Failed to load strategy profile {profile_filename}: {e}")
        return False

def get_strategy_files():
    if not os.path.exists(STRATEGIES_DIR):
        os.makedirs(STRATEGIES_DIR)
    return [f for f in os.listdir(STRATEGIES_DIR) if f.endswith('.json')]

# --- Conversation States ---
(SELECTING_ACTION, SELECTING_PAIR, SETTINGS_MENU, SETTING_CONFIDENCE, 
 SETTING_INDICATOR, AWAITING_VALUE, SETTING_MACD_STRATEGY, 
 SELECTING_STRATEGY, SELECTING_TREND_FILTER) = range(9)

# --- UI Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_name = update.effective_user.first_name
    message = (f"أهلاً بك يا {user_name} في ALNUSIRY BOT {{ VIP }} - v2.0 👋\n\n"
               "مساعدك الذكي لإشارات التداول.\n\n"
               "استخدم الأزرار أدناه للتحكم.")
    await update.message.reply_text(message)
    return await send_main_menu(update, context)

async def send_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, message_text: str = 'القائمة الرئيسية:') -> int:
    status = "يعمل ✅" if bot_state.get('running', False) else "متوقف ❌"
    main_menu_keyboard = [
        [KeyboardButton(f"حالة البوت: {status}")],
        [KeyboardButton("اختيار الأزواج"), KeyboardButton("الإعدادات ⚙️")],
        [KeyboardButton("🔍 اكتشاف الأزواج النشطة"), KeyboardButton("📊 عرض الإحصائيات")]
    ]
    reply_markup = ReplyKeyboardMarkup(main_menu_keyboard, resize_keyboard=True)
    target_message = update.callback_query.message if update.callback_query else update.message
    await target_message.reply_text(message_text, reply_markup=reply_markup)
    return SELECTING_ACTION

async def toggle_bot_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    was_running = bot_state.get('running', False)
    bot_state['running'] = not was_running
    save_bot_settings()
    if bot_state['running']:
        message = "✅ تم تشغيل البوت.\n\nسيبدأ الآن في تحليل السوق وإرسال الإشارات الأولية وتأكيداتها."
        if not context.job_queue.get_jobs_by_name('signal_check'):
            context.job_queue.run_repeating(check_for_signals, interval=60, first=1, name='signal_check')
        if not context.job_queue.get_jobs_by_name('confirmation_check'):
            context.job_queue.run_repeating(confirm_pending_signals, interval=15, first=1, name='confirmation_check')
    else:
        message = "❌ تم إيقاف البوت."
        for job in context.job_queue.get_jobs_by_name('signal_check'): job.schedule_removal()
        for job in context.job_queue.get_jobs_by_name('confirmation_check'): job.schedule_removal()
    await update.message.reply_text(message)
    return await send_main_menu(update, context, "")

async def select_pairs_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    selected = bot_state.get('selected_pairs', [])
    message = "اختر زوجًا لإضافته أو إزالته. الأزواج المختارة حاليًا:\n" + (", ".join(selected) or "لا يوجد")
    pairs_keyboard = [[KeyboardButton(f"{pair} {'✅' if pair in selected else '❌'}")] for pair in USER_DEFINED_PAIRS]
    pairs_keyboard.append([KeyboardButton("العودة إلى القائمة الرئيسية")])
    reply_markup = ReplyKeyboardMarkup(pairs_keyboard, resize_keyboard=True)
    await update.message.reply_text(message, reply_markup=reply_markup)
    return SELECTING_PAIR

async def toggle_pair(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    pair = update.message.text.split(" ")[0]
    if 'selected_pairs' not in bot_state:
        bot_state['selected_pairs'] = []
    if pair in bot_state['selected_pairs']:
        bot_state['selected_pairs'].remove(pair)
    else:
        bot_state['selected_pairs'].append(pair)
    save_bot_settings()
    return await select_pairs_menu(update, context)

async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    settings_keyboard = [
        [KeyboardButton("📁 ملفات تعريف الاستراتيجية")],
        [KeyboardButton("🚦 فلاتر الاتجاه")],
        [KeyboardButton("تحديد عتبة الإشارة الأولية"), KeyboardButton("تحديد عتبة التأكيد النهائي")],
        [KeyboardButton("تعديل قيم المؤشرات"), KeyboardButton("📊 استراتيجية الماكد")],
        [KeyboardButton("🔬 فحص اتصال API"), KeyboardButton("العودة إلى القائمة الرئيسية")]
    ]
    reply_markup = ReplyKeyboardMarkup(settings_keyboard, resize_keyboard=True)
    await update.message.reply_text("اختر الإعداد الذي تريد تعديله:", reply_markup=reply_markup)
    return SETTINGS_MENU

async def trend_filter_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    current_mode = bot_state.get('trend_filter_mode', 'M15')
    modes = {
        'NONE': '⚫️ إيقاف الفلترة (مغامر)',
        'M15': '🟢 M15 فقط (متوازن)',
        'H1': '🟡 H1 فقط (نظرة أوسع)',
        'M15_H1': '🔴 M15 + H1 (متحفظ جدًا)'
    }
    keyboard = []
    for mode, text in modes.items():
        keyboard.append([KeyboardButton(f"{text} {'✅' if current_mode == mode else ''}")])
    keyboard.append([KeyboardButton("العودة إلى الإعدادات")])
    
    await update.message.reply_text(
        f"اختر وضع فلتر الاتجاه (الحالي: {modes.get(current_mode, 'غير معروف')}):",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )
    return SELECTING_TREND_FILTER

async def set_trend_filter_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    choice = update.message.text
    new_mode = 'NONE'
    if 'M15 فقط' in choice: new_mode = 'M15'
    elif 'H1 فقط' in choice: new_mode = 'H1'
    elif 'M15 + H1' in choice: new_mode = 'M15_H1'
    
    bot_state['trend_filter_mode'] = new_mode
    save_bot_settings()
    await update.message.reply_text(f"تم تحديث وضع فلتر الاتجاه إلى: {new_mode}")
    return await settings_menu(update, context)

async def strategy_profile_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    profiles = get_strategy_files()
    if not profiles:
        await update.message.reply_text("لم يتم العثور على ملفات تعريف استراتيجية في مجلد `strategies`.")
        return await settings_menu(update, context)

    keyboard = [[KeyboardButton(f"تحميل: {profile}")] for profile in profiles]
    keyboard.append([KeyboardButton("♻️ إعادة للوضع الافتراضي")])
    keyboard.append([KeyboardButton("العودة إلى الإعدادات")])
    
    current_profile = bot_state.get('profile_name', 'غير معروف')
    await update.message.reply_text(
        f"اختر ملف تعريف لتحميله. (الحالي: {current_profile})",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )
    return SELECTING_STRATEGY

async def set_strategy_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    profile_filename = update.message.text.replace("تحميل: ", "")
    if load_strategy_profile(profile_filename):
        await update.message.reply_text(f"✅ تم تحميل ملف التعريف '{bot_state.get('profile_name')}' بنجاح.")
    else:
        await update.message.reply_text(f"❌ فشل تحميل ملف التعريف '{profile_filename}'.")
    return await settings_menu(update, context)

async def reset_to_default_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if load_strategy_profile('default.json'):
        await update.message.reply_text("✅ تم استعادة الإعدادات الافتراضية بنجاح.")
    else:
        await update.message.reply_text("❌ فشل استعادة الإعدادات الافتراضية. تأكد من وجود ملف `default.json`.")
    return await settings_menu(update, context)

async def set_confidence_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['setting_type'] = 'initial' if 'الأولية' in update.message.text else 'final'
    setting_key = 'initial_confidence' if context.user_data['setting_type'] == 'initial' else 'final_confidence'
    current = bot_state.get(setting_key, 2)
    title = "عتبة الإشارة الأولية" if context.user_data['setting_type'] == 'initial' else "عتبة التأكيد النهائي"
    message = f"اختر الحد الأدنى من المؤشرات المتوافقة لـ **{title}**.\nالحالي: {current}"
    keyboard = [
        [KeyboardButton(f"مؤشرين (مغامر) {'✅' if current == 2 else ''}")],
        [KeyboardButton(f"3 مؤشرات (متوازن) {'✅' if current == 3 else ''}")],
        [KeyboardButton(f"4 مؤشرات (متحفظ) {'✅' if current == 4 else ''}")],
        [KeyboardButton("العودة إلى الإعدادات")]
    ]
    await update.message.reply_text(message, reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True), parse_mode='Markdown')
    return SETTING_CONFIDENCE

async def set_confidence_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    setting_key = 'initial_confidence' if context.user_data.get('setting_type') == 'initial' else 'final_confidence'
    choice = update.message.text
    if "مؤشرين" in choice: bot_state[setting_key] = 2
    elif "3 مؤشرات" in choice: bot_state[setting_key] = 3
    elif "4 مؤشرات" in choice: bot_state[setting_key] = 4
    save_bot_settings()
    title = "الإشارة الأولية" if context.user_data.get('setting_type') == 'initial' else "التأكيد النهائي"
    await update.message.reply_text(f"تم تحديث عتبة {title} إلى: {bot_state.get(setting_key)}")
    update.message.text = f"تحديد عتبة {title}"
    return await set_confidence_menu(update, context)

async def set_indicator_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    params = bot_state.get('indicator_params', DEFAULT_SETTINGS['indicator_params'])
    keyboard = [[KeyboardButton(f"{key.replace('_', ' ').title()} ({value})")] for key, value in params.items()]
    keyboard.append([KeyboardButton("العودة إلى الإعدادات")])
    await update.message.reply_text("اختر المؤشر الذي تريد تعديل قيمته:", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
    return SETTING_INDICATOR

async def select_indicator_to_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    param_key_str = update.message.text.split(" (")[0].lower().replace(' ', '_')
    if param_key_str in bot_state.get('indicator_params', {}):
        context.user_data['param_to_set'] = param_key_str
        await update.message.reply_text(f"أرسل القيمة الرقمية الجديدة لـ {param_key_str}:")
        return AWAITING_VALUE
    await update.message.reply_text("خيار غير صالح.")
    return SETTING_INDICATOR

async def receive_new_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        new_value = int(update.message.text)
        param_key = context.user_data.get('param_to_set')
        if param_key:
            bot_state['indicator_params'][param_key] = new_value
            save_bot_settings()
            await update.message.reply_text("تم حفظ القيمة بنجاح!")
            del context.user_data['param_to_set']
            return await set_indicator_menu(update, context)
    except (ValueError, TypeError):
        await update.message.reply_text("قيمة غير صالحة. يرجى إرسال رقم صحيح فقط.")
        return AWAITING_VALUE
    return await settings_menu(update, context)

async def set_macd_strategy_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    current_strategy = bot_state.get('macd_strategy', 'dynamic')
    message = "اختر استراتيجية الماكد التي تفضلها:"
    keyboard = [
        [KeyboardButton(f"🟢 ديناميكي (جودة عالية) {'✅' if current_strategy == 'dynamic' else ''}")],
        [KeyboardButton(f"🟡 بسيط (كمية أكبر) {'✅' if current_strategy == 'simple' else ''}")],
        [KeyboardButton("العودة إلى الإعدادات")]
    ]
    await update.message.reply_text(message, reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
    return SETTING_MACD_STRATEGY

async def set_macd_strategy_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    choice = update.message.text
    if "ديناميكي" in choice:
        bot_state['macd_strategy'] = 'dynamic'
    elif "بسيط" in choice:
        bot_state['macd_strategy'] = 'simple'
    save_bot_settings()
    await update.message.reply_text(f"تم تحديث استراتيجية الماكد إلى: {bot_state['macd_strategy']}")
    return await settings_menu(update, context)

async def check_api_connection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    api_key = bot_state.get('polygon_api_key')
    if not api_key:
        await update.message.reply_text("❌ خطأ: مفتاح API الخاص بـ Polygon غير موجود.")
        return SETTINGS_MENU
    
    url = f"https://api.polygon.io/v3/reference/tickers/AAPL?apiKey={api_key}"
    await update.message.reply_text("🔬 جاري فحص الاتصال مع Polygon.io...")
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            message = "✅ **الاتصال ناجح!**\n\nأنت متصل بخوادم Polygon.io."
            await update.message.reply_text(message, parse_mode='Markdown')
        else:
            data = response.json()
            message = f"❌ **فشل الاتصال!**\n\n**الرمز:** {response.status_code}\n**الرسالة:** {data.get('message', 'خطأ غير معروف')}"
            await update.message.reply_text(message, parse_mode='Markdown')
    except requests.RequestException as e:
        await update.message.reply_text(f"❌ **خطأ في الشبكة!**\n\nلا يمكن الوصول إلى خوادم Polygon.io. التفاصيل: {e}")
    return SETTINGS_MENU

# --- Data Fetching & Analysis ---
async def fetch_historical_data(pair: str, interval: int, timeframe: str, limit: int) -> pd.DataFrame:
    api_key = bot_state.get("polygon_api_key")
    if not api_key:
        logger.error("Polygon API key is missing.")
        return pd.DataFrame()

    polygon_ticker = f"C:{pair.replace('/', '')}"
    end_date = datetime.now(timezone.utc)
    
    if timeframe == 'minute':
        delta_days = (limit * interval) / (24 * 60) + 5
    else: # hour
        delta_days = (limit * interval) / 24 + 10
        
    start_date = end_date - timedelta(days=delta_days)

    url = (f"https://api.polygon.io/v2/aggs/ticker/{polygon_ticker}/range/{interval}/{timeframe}/"
           f"{start_date.strftime('%Y-%m-%d')}/{end_date.strftime('%Y-%m-%d')}?adjusted=true&sort=desc&limit={limit}&apiKey={api_key}")

    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        data = response.json()

        if "results" in data and data['results']:
            df = pd.DataFrame(data["results"])
            df["datetime"] = pd.to_datetime(df["t"], unit='ms', utc=True)
            df = df.set_index("datetime").astype(float)
            df.rename(columns={"o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"}, inplace=True)
            return df.sort_index()
        else:
            logger.warning(f"No data returned from Polygon for {pair}. Response: {data}")
            return pd.DataFrame()
            
    except requests.exceptions.RequestException as e:
        logger.error(f"Network error fetching data for {pair} from Polygon: {e}")
        return pd.DataFrame()
    except Exception as e:
        logger.error(f"An unexpected error occurred in fetch_historical_data for {pair}: {e}")
        return pd.DataFrame()

def analyze_candlestick_patterns(data: pd.DataFrame) -> dict:
    buy_score = 0
    sell_score = 0
    
    strong_bullish = ['CDLMORNINGSTAR', 'CDL3WHITESOLDIERS']
    strong_bearish = ['CDLEVENINGSTAR', 'CDL3BLACKCROWS']
    
    normal_bullish = ['CDLENGULFING', 'CDLHAMMER', 'CDLINVERTEDHAMMER', 'CDLPIERCING', 'CDL3INSIDE']
    normal_bearish = ['CDLENGULFING', 'CDLHANGINGMAN', 'CDLSHOOTINGSTAR', 'CDL3OUTSIDE', 'CDLHARAMI']

    for pattern in strong_bullish:
        result = getattr(talib, pattern)(data['Open'], data['High'], data['Low'], data['Close'])
        if not result.empty and result.iloc[-1] > 0: buy_score += 2
    for pattern in strong_bearish:
        result = getattr(talib, pattern)(data['Open'], data['High'], data['Low'], data['Close'])
        if not result.empty and result.iloc[-1] < 0: sell_score += 2
            
    for pattern in normal_bullish:
        result = getattr(talib, pattern)(data['Open'], data['High'], data['Low'], data['Close'])
        if not result.empty and result.iloc[-1] > 0: buy_score += 1
    for pattern in normal_bearish:
        result = getattr(talib, pattern)(data['Open'], data['High'], data['Low'], data['Close'])
        if not result.empty:
            if pattern == 'CDLENGULFING' and result.iloc[-1] < 0:
                sell_score += 1
            elif result.iloc[-1] < 0 and pattern != 'CDLENGULFING':
                sell_score += 1
                
    return {'buy': buy_score, 'sell': sell_score}

def get_fibonacci_retracement(data: pd.DataFrame) -> dict:
    if len(data) < 20: return {'buy_proximity': 0, 'sell_proximity': 0}
    
    high_point = data['High'].rolling(window=20).max().iloc[-1]
    low_point = data['Low'].rolling(window=20).min().iloc[-1]
    current_price = data['Close'].iloc[-1]

    if high_point == low_point: return {'buy_proximity': 0, 'sell_proximity': 0}

    levels = [0.382, 0.5, 0.618]
    buy_proximity_score = 0
    sell_proximity_score = 0

    for level in levels:
        fib_level_up = high_point - (high_point - low_point) * level
        fib_level_down = low_point + (high_point - low_point) * level
        
        if abs(current_price - fib_level_up) / current_price < 0.0005:
            buy_proximity_score += 1
        if abs(current_price - fib_level_down) / current_price < 0.0005:
            sell_proximity_score += 1

    return {'buy_proximity': buy_proximity_score, 'sell_proximity': sell_proximity_score}

async def analyze_signal_strength(data: pd.DataFrame, context: ContextTypes.DEFAULT_TYPE) -> dict:
    params = bot_state.get('indicator_params', DEFAULT_SETTINGS['indicator_params'])
    macd_strategy = bot_state.get('macd_strategy', 'dynamic')
    
    required_length = max(params.values())
    if data is None or data.empty or len(data) < required_length:
        logger.warning(f"Not enough data for signal analysis. Got {len(data) if not data.empty else 0}, need {required_length}.")
        return {}

    data["rsi"] = ta.momentum.RSIIndicator(data["Close"], window=params.get('rsi_period', 14)).rsi()
    macd = ta.trend.MACD(data["Close"], window_fast=params.get('macd_fast', 12), window_slow=params.get('macd_slow', 26), window_sign=params.get('macd_signal', 9))
    data["macd"], data["macd_signal"] = macd.macd(), macd.macd_signal()
    bollinger = ta.volatility.BollingerBands(data["Close"], window=params.get('bollinger_period', 20))
    data["bb_h"], data["bb_l"] = bollinger.bollinger_hband(), bollinger.bollinger_lband()
    stoch = ta.momentum.StochasticOscillator(data["High"], data["Low"], data["Close"], window=params.get('stochastic_period', 14))
    data["stoch_k"], data["stoch_d"] = stoch.stoch(), stoch.stoch_signal()
    data.dropna(inplace=True)
    
    if data.empty or len(data) < 2: return {}
    last, prev = data.iloc[-1], data.iloc[-2]
    
    buy_signals, sell_signals = 0, 0
    
    if last["rsi"] < 35: buy_signals += 1
    if last["rsi"] > 30 and prev["rsi"] <= 30: buy_signals += 1
    if last["rsi"] > 65: sell_signals += 1
    if last["rsi"] < 70 and prev["rsi"] >= 70: sell_signals += 1
    
    is_cross_up = last["macd"] > last["macd_signal"] and prev["macd"] <= prev["macd_signal"]
    is_cross_down = last["macd"] < last["macd_signal"] and prev["macd"] >= prev["macd_signal"]
    if macd_strategy == 'dynamic':
        if is_cross_up and last["macd"] < 0: buy_signals += 1
        if is_cross_down and last["macd"] > 0: sell_signals += 1
    else:
        if is_cross_up: buy_signals += 1
        if is_cross_down: sell_signals += 1
        
    if last["Close"] < last["bb_l"]: buy_signals += 1
    if last["Close"] > last["bb_h"]: sell_signals += 1
    
    if last["stoch_k"] > last["stoch_d"] and last["stoch_k"] < 30: buy_signals += 1
    if last["stoch_k"] < last["stoch_d"] and last["stoch_k"] > 70: sell_signals += 1
    
    candle_patterns = analyze_candlestick_patterns(data)
    buy_signals += candle_patterns['buy']
    sell_signals += candle_patterns['sell']
    
    fib_scores = get_fibonacci_retracement(data)
    buy_signals += fib_scores['buy_proximity']
    sell_signals += fib_scores['sell_proximity']

    analysis_results = {
        'buy': buy_signals, 'sell': sell_signals,
        'rsi_value': last["rsi"], 'macd_value': last["macd"], 'stoch_k': last["stoch_k"],
        'candle_buy_score': candle_patterns['buy'], 'candle_sell_score': candle_patterns['sell'],
        'fib_buy_score': fib_scores['buy_proximity'], 'fib_sell_score': fib_scores['sell_proximity']
    }
    return analysis_results

# --- Core Bot Logic ---
async def check_for_signals(context: ContextTypes.DEFAULT_TYPE):
    if not bot_state.get("running") or not bot_state.get('selected_pairs'): return
    
    now = datetime.now(timezone.utc)
    if now.minute % 5 != 0: return
    
    logger.info("Checking for potential signals on M5...")
    
    pairs_to_check = bot_state.get('selected_pairs', [])
    for i in range(0, len(pairs_to_check), 4):
        batch = pairs_to_check[i:i+4]
        tasks = [process_single_pair_signal(pair, context, now) for pair in batch if pair not in pending_signals]
        await asyncio.gather(*tasks)
        
        if i + 4 < len(pairs_to_check):
            logger.info("Waiting for 60 seconds before next batch of signal checks...")
            await asyncio.sleep(60)

async def process_single_pair_signal(pair: str, context: ContextTypes.DEFAULT_TYPE, now: datetime):
    try:
        data = await fetch_historical_data(pair, 5, "minute", 150)
        if data.empty: return

        analysis = await analyze_signal_strength(data, context)
        if not analysis: return

        buy_strength, sell_strength = analysis.get('buy', 0), analysis.get('sell', 0)
        
        direction = None
        if buy_strength >= bot_state.get('initial_confidence',
 2) and sell_strength == 0:
            direction = "صعود"
        elif sell_strength >= bot_state.get('initial_confidence', 2) and buy_strength == 0:
            direction = "هبوط"
            
        if direction:
            entry_time = (now + timedelta(minutes=5)).strftime("%H:%M:%S")
            direction_emoji = "🟢" if direction == "صعود" else "🔴"
            direction_arrow = "⬆️" if direction == "صعود" else "⬇️"
            signal_text = (f"   🔔   {direction_emoji} {{  اشارة   {direction}  }} {direction_emoji}   🔔       \n"
                           f"           📊 الزوج :  {pair} \n"
                           f"           🕛  الفريم :  M5\n"
                           f"           📉  الاتجاه:  {direction} {direction_arrow}\n"
                           f"           ⏳ وقت الدخول : {entry_time}\n\n"
                           f"               🔍 {{  انتظر   التاكيد   }}")
            sent_message = await context.bot.send_message(chat_id=CHAT_ID, text=signal_text)
            
            pending_signals[pair] = {
                'direction': direction, 
                'message_id': sent_message.message_id, 
                'timestamp': now,
                'initial_analysis': analysis
            }
            logger.info(f"Potential signal found for {pair}. Awaiting confirmation.")
            
    except Exception as e:
        await send_error_to_telegram(context, f"Error in process_single_pair_signal for {pair}: {e}")


async def confirm_pending_signals(context: ContextTypes.DEFAULT_TYPE):
    if not bot_state.get("running") or not pending_signals:
        return
        
    now = datetime.now(timezone.utc)
    if now.minute % 5 != 4 or now.second < 45:
        return

    logger.info("Final confirmation window is open. Checking pending signals...")
    
    pairs_to_confirm = list(pending_signals.items())
    for pair, signal_info in pairs_to_confirm:
        try:
            time_since_signal = (now - signal_info['timestamp']).total_seconds()
            if time_since_signal < 60:
                continue

            trend_filter_mode = bot_state.get('trend_filter_mode', 'M15')
            m15_trend_ok, h1_trend_ok = True, True
            cancellation_reason = ""

            if trend_filter_mode in ['M15', 'M15_H1']:
                data_m15 = await fetch_historical_data(pair, 15, "minute", 100)
                if data_m15.empty:
                    logger.warning(f"Could not fetch M15 data for {pair}, skipping M15 trend filter.")
                else:
                    m15_ema = ta.trend.EMAIndicator(data_m15['Close'], window=bot_state['indicator_params']['m15_ema_period']).ema_indicator().iloc[-1]
                    if (signal_info['direction'] == 'صعود' and data_m15['Close'].iloc[-1] < m15_ema) or \
                       (signal_info['direction'] == 'هبوط' and data_m15['Close'].iloc[-1] > m15_ema):
                        m15_trend_ok = False
                        cancellation_reason = "الإشارة معاكسة لاتجاه M15"

            if m15_trend_ok and trend_filter_mode in ['H1', 'M15_H1']:
                data_h1 = await fetch_historical_data(pair, 1, "hour", 100)
                if data_h1.empty:
                    logger.warning(f"Could not fetch H1 data for {pair}, skipping H1 trend filter.")
                else:
                    h1_ema = ta.trend.EMAIndicator(data_h1['Close'], window=bot_state['indicator_params']['h1_ema_period']).ema_indicator().iloc[-1]
                    if (signal_info['direction'] == 'صعود' and data_h1['Close'].iloc[-1] < h1_ema) or \
                       (signal_info['direction'] == 'هبوط' and data_h1['Close'].iloc[-1] > h1_ema):
                        h1_trend_ok = False
                        cancellation_reason = "الإشارة معاكسة لاتجاه H1"

            confirmed = False
            final_analysis = {}
            if m15_trend_ok and h1_trend_ok:
                data_m5 = await fetch_historical_data(pair, 5, "minute", 150)
                if data_m5.empty:
                    raise Exception("Failed to fetch M5 data for final confirmation.")

                final_analysis = await analyze_signal_strength(data_m5, context)
                if not final_analysis:
                    raise Exception("Final M5 analysis returned empty.")

                buy_strength, sell_strength = final_analysis.get('buy', 0), final_analysis.get('sell', 0)
                final_confidence = bot_state.get('final_confidence', 3)

                if (signal_info['direction'] == 'صعود' and buy_strength >= final_confidence and sell_strength == 0) or \
                   (signal_info['direction'] == 'هبوط' and sell_strength >= final_confidence and buy_strength == 0):
                    confirmed = True
                else:
                    cancellation_reason = "ضعف تأكيد شروط الدخول على فريم M5"
            
            await context.bot.delete_message(chat_id=CHAT_ID, message_id=signal_info['message_id'])
            
            unique_trade_id = f"{pair.replace('/', '')}-{now.strftime('%Y%m%d%H%M%S')}"

            if confirmed:
                confirmation_text = (f"✅✅✅   تــأكــيــد الــدخــول   ✅✅✅\n\n"
                                     f"الزوج: {pair}\n"
                                     f"الاتجاه: {signal_info['direction']} {'⬆️' if signal_info['direction'] == 'صعود' else '⬇️'}\n\n"
                                     f"          🔥 ادخــــــــل الآن 🔥")
                
                follow_up_keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("ربح ✅", callback_data=f"result_win_{unique_trade_id}"),
                     InlineKeyboardButton("خسارة ❌", callback_data=f"result_loss_{unique_trade_id}")]
                ])
                
                sent_follow_up = await context.bot.send_message(chat_id=CHAT_ID, text=confirmation_text, reply_markup=follow_up_keyboard)
                
                trade_follow_ups[unique_trade_id] = {
                    'pair': pair,
                    'direction': signal_info['direction'],
                    'timestamp': now.isoformat(),
                    'message_id': sent_follow_up.message_id,
                    'initial_analysis': signal_info['initial_analysis'],
                    'final_analysis': final_analysis
                }
                logger.info(f"Signal CONFIRMED for {pair}")

            else:
                cancellation_text = (f"❌❌❌   إلــغــاء الــصــفــقــة   ❌❌❌\n\n"
                                     f"الزوج: {pair}\n\n"
                                     f"**السبب: {cancellation_reason}.**\n\n"
                                     "الشروط لم تعد مثالية، لا تقم بالدخول.")
                await context.bot.send_message(chat_id=CHAT_ID, text=cancellation_text, parse_mode='Markdown')
                logger.info(f"Signal CANCELED for {pair} due to: {cancellation_reason}")
            
            del pending_signals[pair]

        except Exception as e:
            await send_error_to_telegram(context, f"Error in confirm_pending_signals for {pair}: {e}")
            if pair in pending_signals:
                try:
                    await context.bot.delete_message(chat_id=CHAT_ID, message_id=pending_signals[pair]['message_id'])
                except Exception: pass
                del pending_signals[pair]

async def trade_result_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    _, result, trade_id = query.data.split('_')
    
    if trade_id in trade_follow_ups:
        trade_data = trade_follow_ups[trade_id]
        
        file_exists = os.path.isfile(TRADES_FILE)
        with open(TRADES_FILE, 'a', newline='') as f:
            flat_data = {
                'trade_id': trade_id, 'timestamp': trade_data['timestamp'], 'pair': trade_data['pair'],
                'direction': trade_data['direction'], 'result': result,
                **{f"initial_{k}": v for k, v in trade_data['initial_analysis'].items()},
                **{f"final_{k}": v for k, v in trade_data['final_analysis'].items()}
            }
            
            writer = pd.DataFrame([flat_data])
            writer.to_csv(f, header=not file_exists, index=False)

        result_text = "ربح" if result == 'win' else "خسارة"
        await query.edit_message_text(text=f"{query.message.text}\n\n---\n**تم تسجيل النتيجة:** {result_text}")
        
        del trade_follow_ups[trade_id]
        logger.info(f"Result '{result}' recorded for trade {trade_id}")
    else:
        await query.edit_message_text(text=f"{query.message.text}\n\n---\n**خطأ:** لم يتم العثور على بيانات هذه الصفقة.")

async def show_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not os.path.exists(TRADES_FILE):
        await update.message.reply_text("لا يوجد سجل تداول حتى الآن لبناء الإحصائيات.")
        return SELECTING_ACTION

    df = pd.read_csv(TRADES_FILE)
    if df.empty:
        await update.message.reply_text("سجل التداول فارغ.")
        return SELECTING_ACTION

    total_trades = len(df)
    wins = len(df[df['result'] == 'win'])
    losses = len(df[df['result'] == 'loss'])
    win_rate = (wins / total_trades) * 100 if total_trades > 0 else 0

    stats_text = (
        f"📊 **إحصائيات أداء البوت** 📊\n\n"
        f"**إجمالي الصفقات:** {total_trades}\n"
        f"**صفقات رابحة:** {wins} ✅\n"
        f"**صفقات خاسرة:** {losses} ❌\n"
        f"**نسبة النجاح:** {win_rate:.2f}%\n\n"
        f"--- *تحليل مستمر لتحسين الأداء* ---"
    )
    await update.message.reply_text(stats_text, parse_mode='Markdown')
    return SELECTING_ACTION

async def find_active_pairs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("🔍 جاري تحليل نشاط السوق... قد تستغرق العملية عدة دقائق لاحترام حدود الـ API.", reply_markup=ReplyKeyboardMarkup([[]], resize_keyboard=True))
    all_results = []
    
    pairs_to_check = USER_DEFINED_PAIRS.copy()
    
    for i in range(0, len(pairs_to_check), 4):
        batch = pairs_to_check[i:i+4]
        tasks = [analyze_pair_activity(pair, context) for pair in batch]
        results = await asyncio.gather(*tasks)
        for res in results:
            if res: all_results.append(res)
        
        if i + 4 < len(pairs_to_check):
            logger.info("Waiting for 60 seconds to respect API rate limit...")
            await asyncio.sleep(60)

    if not all_results:
        await send_main_menu(update, context, "لم يتم العثور على أزواج نشطة حاليًا (قد يكون السوق مغلقًا).")
        return SELECTING_ACTION

    all_results.sort(key=lambda x: x.get('adx', 0) + (x.get('atr_percent', 0) * 20), reverse=True)
    top_pairs = all_results[:4]
    message = "📈 **أفضل الأزواج النشطة للتداول الآن:**\n\n"
    keyboard = []
    for res in top_pairs:
        reason = "اتجاه قوي" if res.get('adx', 0) > 25 else "تقلب جيد" if res.get('atr_percent', 0) > 0.04 else "نشاط معتدل"
        message += f"• **{res['pair']}** ({reason})\n"
        keyboard.append([InlineKeyboardButton(f"✅ تفعيل مراقبة {res['pair']}", callback_data=f"addpair_{res['pair']}")])
    keyboard.append([InlineKeyboardButton("➕ تفعيل مراقبة الكل", callback_data="addpairall_" + ",".join([p['pair'] for p in top_pairs]))])
    await update.message.reply_text(message, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return await send_main_menu(update, context, message_text="اختر إجراءً آخر من القائمة الرئيسية:")

async def analyze_pair_activity(pair: str, context: ContextTypes.DEFAULT_TYPE) -> dict or None:
    try:
        data = await fetch_historical_data(pair, 5, "minute", 100)
        params = bot_state.get('indicator_params', DEFAULT_SETTINGS['indicator_params'])
        
        required_length = max(params.get('adx_period', 14), params.get('atr_period', 14))
        if data is None or data.empty or len(data) < required_length:
            logger.warning(f"Not enough data for {pair} to analyze activity. Got {len(data) if not data.empty else 0}, need {required_length}.")
            return None

        adx_value = ta.trend.ADXIndicator(data['High'], data['Low'], data['Close'], window=params.get('adx_period', 14)).adx().iloc[-1]
        atr_value = ta.volatility.AverageTrueRange(data['High'], data['Low'], data['Close'], window=params.get('atr_period', 14)).average_true_range().iloc[-1]
        atr_percent = (atr_value / data['Close'].iloc[-1]) * 100
        return {'pair': pair, 'adx': adx_value, 'atr_percent': atr_percent}
    except Exception as e:
        await send_error_to_telegram(context, f"Error in analyze_pair_activity for {pair}: {e}")
        return None

async def add_pair_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    action, payload = query.data.split('_', 1)
    pairs_to_add = payload.split(',') if action == 'addpairall' else [payload]
    if 'selected_pairs' not in bot_state:
        bot_state['selected_pairs'] = []
    added_now = [pair for pair in pairs_to_add if pair not in bot_state['selected_pairs']]
    if added_now:
        bot_state['selected_pairs'].extend(added_now)
        save_bot_settings()
        await query.edit_message_text(text=f"تم تفعيل المراقبة للأزواج:\n{', '.join(added_now)}")
    else:
        await query.edit_message_text(text="الأزواج المحددة مفعلة بالفعل.")

# --- Main Application Setup ---
def main_bot():
    if not all([TOKEN, CHAT_ID, POLYGON_API_KEY]):
        logger.critical("One or more environment variables are missing.")
        return
        
    load_bot_settings()
    
    persistence = PicklePersistence(filepath="bot_persistence")
    application = Application.builder().token(TOKEN).persistence(persistence).build()
    
    application.add_handler(CallbackQueryHandler(add_pair_callback, pattern=r'^addpair'))
    application.add_handler(CallbackQueryHandler(trade_result_callback, pattern=r'^result_'))

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            SELECTING_ACTION: [
                MessageHandler(filters.Regex(r'^(حالة البوت:)'), toggle_bot_status),
                MessageHandler(filters.Regex(r'^اختيار الأزواج$'), select_pairs_menu),
                MessageHandler(filters.Regex(r'^الإعدادات ⚙️$'), settings_menu),
                MessageHandler(filters.Regex(r'^🔍 اكتشاف الأزواج النشطة$'), find_active_pairs_command),
                MessageHandler(filters.Regex(r'^📊 عرض الإحصائيات$'), show_stats_command),
            ],
            SELECTING_PAIR: [
                MessageHandler(filters.Regex(r'العودة إلى القائمة الرئيسية'), start), 
                MessageHandler(filters.TEXT & ~filters.COMMAND, toggle_pair)
            ],
            SETTINGS_MENU: [
                MessageHandler(filters.Regex(r'^📁 ملفات تعريف الاستراتيجية$'), strategy_profile_menu),
                MessageHandler(filters.Regex(r'^🚦 فلاتر الاتجاه$'), trend_filter_menu),
                MessageHandler(filters.Regex(r'^تحديد عتبة الإشارة الأولية$'), set_confidence_menu),
                MessageHandler(filters.Regex(r'^تحديد عتبة التأكيد النهائي$'), set_confidence_value),
                MessageHandler(filters.Regex(r'^تعديل قيم المؤشرات$'), set_indicator_menu),
                MessageHandler(filters.Regex(r'^📊 استراتيجية الماكد$'), set_macd_strategy_menu),
                MessageHandler(filters.Regex(r'^🔬 فحص اتصال API$'), check_api_connection),
                MessageHandler(filters.Regex(r'العودة إلى القائمة الرئيسية'), start),
            ],
            SELECTING_STRATEGY: [
                MessageHandler(filters.Regex(r'العودة إلى الإعدادات'), settings_menu),
                MessageHandler(filters.Regex(r'^♻️ إعادة للوضع الافتراضي$'), reset_to_default_profile),
                MessageHandler(filters.Regex(r'^تحميل: '), set_strategy_profile),
            ],
            SELECTING_TREND_FILTER: [
                MessageHandler(filters.Regex(r'العودة إلى الإعدادات'), settings_menu),
                MessageHandler(filters.TEXT & ~filters.COMMAND, set_trend_filter_mode),
            ],
            SETTING_CONFIDENCE: [
                MessageHandler(filters.Regex(r'العودة إلى الإعدادات'), settings_menu),
                MessageHandler(filters.TEXT & ~filters.COMMAND, set_confidence_value)
            ],
            SETTING_INDICATOR: [
                MessageHandler(filters.Regex(r'العودة إلى الإعدادات'), settings_menu),
                MessageHandler(filters.TEXT & ~filters.COMMAND, select_indicator_to_set)
            ],
            AWAITING_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_new_value)
            ],
            SETTING_MACD_STRATEGY: [
                MessageHandler(filters.Regex(r'العودة إلى الإعدادات'), settings_menu),
                MessageHandler(filters.TEXT & ~filters.COMMAND, set_macd_strategy_value)
            ]
        },
        fallbacks=[CommandHandler('start', start)],
        persistent=True, name="bot_conversation"
    )
    application.add_handler(conv_handler)
    
    if bot_state.get('running'):
        application.job_queue.run_repeating(check_for_signals, interval=60, first=1, name='signal_check')
        application.job_queue.run_repeating(confirm_pending_signals, interval=15, first=1, name='confirmation_check')
        
    logger.info("Bot v2.0 is starting with Polygon.io data provider...")
    application.run_polling()

if __name__ == '__main__':
    flask_thread = threading.Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000))))
    flask_thread.daemon = True
    flask_thread.start()
    
    main_bot()
