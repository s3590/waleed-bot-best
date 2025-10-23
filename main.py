# -*- coding: utf-8 -*-
# ALNUSIRY BOT { VIP } - Version 3.0 (Full Feature & Stability Fix)
# Changelog:
# - Restored the full-featured ConversationHandler UI as requested.
# - Corrected all NameError issues by reordering function definitions.
# - Fixed all IndentationError and logical errors.
# - Ensured the ConversationHandler flow is stable and fallbacks work correctly.
# - All original buttons and detailed settings menus are now functional.

import logging
import json
import os
import asyncio
from datetime import datetime, timedelta, timezone
from threading import Thread

import pandas as pd
import requests
import ta
# import talib # Optional, complex installation

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ContextTypes, ConversationHandler, CallbackQueryHandler, JobQueue
)


from flask import Flask

# --- الإعدادات الأساسية والمتغيرات العامة ---
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
POLYGON_API_KEY = os.environ.get('POLYGON_API_KEY')

STATE_FILE = 'bot_state.json'
STRATEGIES_DIR = 'strategies'

# --- قائمة الأزواج المعتمدة ---
USER_DEFINED_PAIRS = [
    "EUR/USD", "USD/JPY", "USD/CHF", "AUD/USD", "USD/CAD",
    "EUR/JPY", "AUD/JPY", "CHF/JPY", "EUR/CHF", "AUD/CHF", "CAD/CHF",
    "EUR/AUD", "EUR/CAD", "AUD/CAD", "CAD/JPY"
]

# --- إعداد تسجيل الأنشطة (Logging) ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- خادم ويب Flask (للتوافق مع Render) ---
flask_app = Flask(__name__)
@flask_app.route('/')
def health_check():
    return "ALNUSIRY BOT (Full Feature) is alive!", 200

def run_flask_app():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host='0.0.0.0', port=port)

# --- حالة البوت والبيانات ---
bot_state = {}
signals_statistics = {}
pending_signals = [] # يجب أن تكون قائمة

# --- دوال إدارة الحالة والاستراتيجيات ---
def save_bot_state():
    """حفظ حالة البوت والإحصائيات في ملف JSON."""
    try:
        state_to_save = {
            'bot_state': bot_state,
            'signals_statistics': signals_statistics
        }
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(state_to_save, f, indent=4, ensure_ascii=False)
        logger.info("تم حفظ حالة البوت بنجاح.")
    except Exception as e:
        logger.error(f"فشل في حفظ حالة البوت: {e}")

def load_strategy_profile(profile_filename: str) -> bool:
    """تحميل ملف تعريف استراتيجية معين."""
    global bot_state
    filepath = os.path.join(STRATEGIES_DIR, profile_filename)
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            profile_settings = json.load(f)
        
        # الاحتفاظ بالحالة الحالية للتشغيل والأزواج المختارة
        is_running = bot_state.get('is_running', False)
        selected_pairs = bot_state.get('selected_pairs', [])
        
        bot_state = profile_settings
        bot_state['is_running'] = is_running
        bot_state['selected_pairs'] = selected_pairs
        
        save_bot_state()
        logger.info(f"تم تحميل ملف التعريف بنجاح: {profile_filename}")
        return True
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.error(f"فشل تحميل ملف التعريف {profile_filename}: {e}")
        return False

def load_bot_state():
    """تحميل حالة البوت عند بدء التشغيل."""
    global bot_state, signals_statistics
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            loaded_data = json.load(f)
            bot_state = loaded_data.get('bot_state', {})
            signals_statistics = loaded_data.get('signals_statistics', {})
        logger.info("تم تحميل حالة البوت من الملف.")
    except (FileNotFoundError, json.JSONDecodeError):
        logger.warning("ملف حالة البوت غير موجود. سيتم تحميل الإعدادات الافتراضية من 'default.json'.")
        if not os.path.exists(STRATEGIES_DIR):
            os.makedirs(STRATEGIES_DIR)
        if not load_strategy_profile('default.json'):
            logger.error("فشل تحميل 'default.json'. سيتم استخدام إعدادات الطوارئ.")
            bot_state = {
                'is_running': False, 'selected_pairs': [], 'profile_name': 'الطوارئ',
                'initial_confidence': 3, 'confirmation_confidence': 4,
                'scan_interval_seconds': 300, 'confirmation_minutes': 5,
                'macd_strategy': 'dynamic',
                'indicator_params': {
                    'rsi_period': 14, 'macd_fast': 12, 'macd_slow': 26, 'macd_signal': 9,
                    'bollinger_period': 20, 'stochastic_period': 14, 'adx_period': 14, 'atr_period': 14
                }
            }
        signals_statistics = {}
        save_bot_state()

def get_strategy_files():
    """الحصول على قائمة بملفات الاستراتيجيات المتاحة."""
    if not os.path.exists(STRATEGIES_DIR):
        os.makedirs(STRATEGIES_DIR)
    return [f for f in os.listdir(STRATEGIES_DIR) if f.endswith('.json')]

# --- دوال التحليل الفني (معرّفة قبل استخدامها) ---
# (نفس دوال التحليل من الإصدار السابق المستقر)
async def get_forex_data(pair: str, timeframe: str, limit: int) -> pd.DataFrame:
    if not POLYGON_API_KEY:
        logger.error("مفتاح Polygon API غير موجود!")
        return pd.DataFrame()
    polygon_ticker = f"C:{pair.replace('/', '')}"
    interval_map = {"M5": "5", "M15": "15", "H1": "1"}
    timespan_map = {"M5": "minute", "M15": "minute", "H1": "hour"}
    if timeframe not in interval_map: return pd.DataFrame()
    interval, timespan = interval_map[timeframe], timespan_map[timeframe]
    end_date = datetime.now(timezone.utc)
    if timespan == 'minute': start_date = end_date - timedelta(days=(int(interval) * limit) / (24 * 60) + 2)
    else: start_date = end_date - timedelta(days=(int(interval) * limit) / 24 + 2)
    url = (f"https://api.polygon.io/v2/aggs/ticker/{polygon_ticker}/range/{interval}/{timespan}/"
           f"{start_date.strftime('%Y-%m-%d')}/{end_date.strftime('%Y-%m-%d')}?adjusted=true&sort=asc&limit={limit}")
    headers = {"Authorization": f"Bearer {POLYGON_API_KEY}"}
    try:
        async with asyncio.get_event_loop().run_in_executor(None, lambda: requests.get(url, headers=headers, timeout=20)) as response:
            response.raise_for_status()
            data = response.json()
            if "results" in data and data['results']:
                df = pd.DataFrame(data['results'])
                df['datetime'] = pd.to_datetime(df['t'], unit='ms', utc=True)
                df = df.set_index('datetime')[['o', 'h', 'l', 'c', 'v']].astype(float)
                df.columns = ['Open', 'High', 'Low', 'Close', 'Volume']
                return df
            return pd.DataFrame()
    except Exception as e:
        logger.error(f"خطأ أثناء جلب بيانات {pair}: {e}")
        return pd.DataFrame()

def analyze_signal_strength(df: pd.DataFrame) -> (int, int):
    buy, sell = 0, 0
    params = bot_state.get('indicator_params', {})
    required_len = max(params.values()) if params else 26
    if df is None or df.empty or len(df) < required_len: return 0, 0
    df['rsi'] = ta.momentum.RSIIndicator(df['Close'], window=params.get('rsi_period', 14)).rsi()
    macd_indicator = ta.trend.MACD(df['Close'], window_fast=params.get('macd_fast', 12), window_slow=params.get('macd_slow', 26), window_sign=params.get('macd_signal', 9))
    df['macd'], df['macd_signal'] = macd_indicator.macd(), macd_indicator.macd_signal()
    bollinger = ta.volatility.BollingerBands(df['Close'], window=params.get('bollinger_period', 20))
    df['bb_h'], df['bb_l'] = bollinger.bollinger_hband(), bollinger.bollinger_lband()
    stoch = ta.momentum.StochasticOscillator(df['High'], df['Low'], df['Close'], window=params.get('stochastic_period', 14))
    df['stoch_k'], df['stoch_d'] = stoch.stoch(), stoch.stoch_signal()
    adx_indicator = ta.trend.ADXIndicator(df['High'], df['Low'], df['Close'], window=params.get('adx_period', 14))
    df['adx'], df['dmp'], df['dmn'] = adx_indicator.adx(), adx_indicator.adx_pos(), adx_indicator.adx_neg()
    df.dropna(inplace=True)
    if df.empty: return 0, 0
    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else last
    if last['rsi'] < 30: buy += 1
    if last['rsi'] > 70: sell += 1
    if bot_state.get('macd_strategy', 'dynamic') == 'dynamic':
        if last['macd'] > last['macd_signal'] and prev['macd'] <= prev['macd_signal'] and last['macd'] < 0: buy += 1
        if last['macd'] < last['macd_signal'] and prev['macd'] >= prev['macd_signal'] and last['macd'] > 0: sell += 1
    else: # simple
        if last['macd'] > last['macd_signal'] and prev['macd'] <= prev['macd_signal']: buy += 1
        if last['macd'] < last['macd_signal'] and prev['macd'] >= prev['macd_signal']: sell += 1
    if last['Close'] < last['bb_l']: buy += 1
    if last['Close'] > last['bb_h']: sell += 1
    if last['stoch_k'] > last['stoch_d'] and last['stoch_k'] < 30: buy += 1
    if last['stoch_k'] < last['stoch_d'] and last['stoch_k'] > 70: sell += 1
    if last['adx'] > 25 and last['dmp'] > last['dmn']: buy += 1
    if last['adx'] > 25 and last['dmn'] > last['dmp']: sell += 1
    return buy, sell

# --- دوال البوت الأساسية والمهام المجدولة ---
# (نفس دوال البوت من الإصدار السابق المستقر)
async def process_single_pair_signal(pair: str, context: ContextTypes.DEFAULT_TYPE):
    global pending_signals, signals_statistics
    if any(s['pair'] == pair for s in pending_signals): return False
    df = await get_forex_data(pair, "M5", 200)
    if df is None or df.empty: return False
    buy_strength, sell_strength = analyze_signal_strength(df)
    signal_type, confidence = (None, 0)
    if buy_strength > sell_strength and buy_strength >= bot_state.get('initial_confidence', 3):
        signal_type, confidence = 'BUY', buy_strength
    elif sell_strength > buy_strength and sell_strength >= bot_state.get('initial_confidence', 3):
        signal_type, confidence = 'SELL', sell_strength
    if signal_type:
        new_signal = {'pair': pair, 'type': signal_type, 'confidence': confidence, 'timestamp': datetime.now(timezone.utc)}
        pending_signals.append(new_signal)
        if pair not in signals_statistics: signals_statistics[pair] = {'initial': 0, 'confirmed': 0, 'failed_confirmation': 0}
        signals_statistics[pair]['initial'] += 1
        save_bot_state()
        strength_meter = '⬆️' * buy_strength if signal_type == 'BUY' else '⬇️' * sell_strength
        message = (f"🔔 إشارة أولية محتملة 🔔\n\nالزوج: {pair}\nالنوع: {signal_type}\nالقوة: {strength_meter} ({confidence})\n"
                   f"سيتم التأكيد بعد {bot_state.get('confirmation_minutes', 5)} دقيقة.")
        try:
            await context.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)
            return True
        except Exception as e:
            logger.error(f"فشل إرسال رسالة الإشارة الأولية: {e}")
            pending_signals.remove(new_signal)
            signals_statistics[pair]['initial'] -= 1
            return False
    return False

async def check_for_signals(context: ContextTypes.DEFAULT_TYPE):
    if not bot_state.get('is_running', False): return
    pairs = bot_state.get('selected_pairs', [])
    if not pairs: return
    logger.info(f"بدء جولة التحليل للأزواج: {', '.join(pairs)}")
    tasks = [process_single_pair_signal(pair, context) for pair in pairs]
    await asyncio.gather(*tasks)

async def confirm_pending_signals(context: ContextTypes.DEFAULT_TYPE):
    global pending_signals, signals_statistics
    if not bot_state.get('is_running', False): return
    current_time = datetime.now(timezone.utc)
    confirmation_minutes = bot_state.get('confirmation_minutes', 5)
    signals_to_process = [s for s in pending_signals if (current_time - s['timestamp']).total_seconds() / 60 >= confirmation_minutes]
    for signal in signals_to_process:
        pending_signals.remove(signal)
        pair, initial_type = signal['pair'], signal['type']
        df_confirm = await get_forex_data(pair, "M5", 200)
        if df_confirm is None or df_confirm.empty:
            signals_statistics[pair]['failed_confirmation'] += 1
            continue
        buy_strength, sell_strength = analyze_signal_strength(df_confirm)
        confirmed = False
        if initial_type == 'BUY' and buy_strength > sell_strength and buy_strength >= bot_state.get('confirmation_confidence', 4): confirmed = True
        elif initial_type == 'SELL' and sell_strength > buy_strength and sell_strength >= bot_state.get('confirmation_confidence', 4): confirmed = True
        if confirmed:
            strength_meter = '⬆️' * buy_strength if initial_type == 'BUY' else '⬇️' * sell_strength
            message = (f"✅ إشارة مؤكدة ✅\n\nالزوج: {pair}\nالنوع: {initial_type}\nقوة التأكيد: {strength_meter}")
            try:
                await context.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)
                signals_statistics[pair]['confirmed'] += 1
            except Exception as e: logger.error(f"فشل إرسال رسالة التأكيد: {e}")
        else:
            signals_statistics[pair]['failed_confirmation'] += 1
        save_bot_state()

# --- تعريف حالات المحادثة ---
(SELECTING_ACTION, SELECTING_PAIR, SETTINGS_MENU, SETTING_CONFIDENCE, 
 SETTING_INDICATOR, AWAITING_VALUE, SETTING_MACD_STRATEGY, 
 SELECTING_STRATEGY, SHOW_STATS) = range(9)

# --- دوال واجهة المستخدم والقوائم (ConversationHandler) ---

# --- القائمة الرئيسية ودوال البدء ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """إرسال رسالة ترحيبية وعرض القائمة الرئيسية."""
    user_name = update.effective_user.first_name
    message = (f"أهلاً بك يا {user_name} في ALNUSIRY BOT {{ VIP }} - v3.0 👋\n\n"
               "مساعدك الذكي لإشارات التداول. (إصدار الميزات الكاملة)")
    await update.message.reply_text(message)
    return await send_main_menu(update, context)

async def send_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, message_text: str = 'القائمة الرئيسية:') -> int:
    """إرسال القائمة الرئيسية مع الأزرار."""
    status_text = "يعمل ✅" if bot_state.get('is_running', False) else "متوقف ❌"
    main_menu_keyboard = [
        [KeyboardButton(f"حالة البوت: {status_text}")],
        [KeyboardButton("اختيار الأزواج"), KeyboardButton("الإعدادات ⚙️")],
        [KeyboardButton("📊 عرض الإحصائيات")]
    ]
    reply_markup = ReplyKeyboardMarkup(main_menu_keyboard, resize_keyboard=True)
    
    # تحديد كيفية إرسال الرسالة بناءً على مصدر الاستدعاء
    if update.callback_query:
        await update.callback_query.message.edit_text(message_text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(message_text, reply_markup=reply_markup)
        
    return SELECTING_ACTION

async def toggle_bot_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """تبديل حالة تشغيل/إيقاف البوت."""
    if not bot_state.get('selected_pairs') and not bot_state.get('is_running'):
        await update.message.reply_text("⚠️ خطأ: يرجى تحديد زوج عملات واحد على الأقل قبل البدء.")
        return await send_main_menu(update, context, "")

    was_running = bot_state.get('is_running', False)
    bot_state['is_running'] = not was_running
    save_bot_state()
    
    if bot_state['is_running']:
        message = "✅ تم تشغيل البوت. سيبدأ الآن في تحليل السوق."
    else:
        message = "❌ تم إيقاف البوت."
        
    await update.message.reply_text(message)
    return await send_main_menu(update, context, "")

# --- قائمة اختيار الأزواج ---
async def select_pairs_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """عرض قائمة اختيار الأزواج."""
    selected = bot_state.get('selected_pairs', [])
    message = "اختر زوجًا لإضافته أو إزالته. الأزواج المختارة حاليًا:\n" + (", ".join(selected) or "لا يوجد")
    pairs_keyboard = [[KeyboardButton(f"{pair} {'✅' if pair in selected else '❌'}")] for pair in USER_DEFINED_PAIRS]
    pairs_keyboard.append([KeyboardButton("العودة إلى القائمة الرئيسية")])
    reply_markup = ReplyKeyboardMarkup(pairs_keyboard, resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text(message, reply_markup=reply_markup)
    return SELECTING_PAIR

async def toggle_pair(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """إضافة أو إزالة زوج من القائمة."""
    pair = update.message.text.split(" ")[0]
    if 'selected_pairs' not in bot_state: bot_state['selected_pairs'] = []
    
    if pair in bot_state['selected_pairs']:
        bot_state['selected_pairs'].remove(pair)
    elif pair in USER_DEFINED_PAIRS:
        bot_state['selected_pairs'].append(pair)
    
    save_bot_state()
    return await select_pairs_menu(update, context) # إعادة عرض القائمة المحدثة

# --- قائمة الإعدادات الرئيسية ---
async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """عرض قائمة الإعدادات."""
    settings_keyboard = [
        [KeyboardButton("📁 ملفات تعريف الاستراتيجية")],
        [KeyboardButton("تحديد عتبة الإشارة الأولية"), KeyboardButton("تحديد عتبة التأكيد النهائي")],
        [KeyboardButton("تعديل قيم المؤشرات"), KeyboardButton("📊 استراتيجية الماكد")],
        [KeyboardButton("العودة إلى القائمة الرئيسية")]
    ]
    reply_markup = ReplyKeyboardMarkup(settings_keyboard, resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text("اختر الإعداد الذي تريد تعديله:", reply_markup=reply_markup)
    return SETTINGS_MENU

# --- قوائم الإعدادات الفرعية ---
async def strategy_profile_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    profiles = get_strategy_files()
    keyboard = [[KeyboardButton(f"تحميل: {profile}")] for profile in profiles]
    keyboard.append([KeyboardButton("العودة إلى الإعدادات")])
    current_profile = bot_state.get('profile_name', 'غير معروف')
    await update.message.reply_text(
        f"اختر ملف تعريف لتحميله. (الحالي: {current_profile})",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
    )
    return SELECTING_STRATEGY

async def set_strategy_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    profile_filename = update.message.text.replace("تحميل: ", "")
    if load_strategy_profile(profile_filename):
        await update.message.reply_text(f"✅ تم تحميل ملف التعريف '{bot_state.get('profile_name')}' بنجاح.")
    else:
        await update.message.reply_text(f"❌ فشل تحميل ملف التعريف '{profile_filename}'.")
    return await settings_menu(update, context)

async def set_confidence_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['setting_type'] = 'initial' if 'الأولية' in update.message.text else 'final'
    setting_key = 'initial_confidence' if context.user_data['setting_type'] == 'initial' else 'confirmation_confidence'
    current = bot_state.get(setting_key, 2)
    title = "عتبة الإشارة الأولية" if context.user_data['setting_type'] == 'initial' else "عتبة التأكيد النهائي"
    message = f"اختر الحد الأدنى من المؤشرات المتوافقة لـ **{title}**.\nالحالي: {current}"
    keyboard = [[KeyboardButton(f"{i} مؤشرات {'✅' if current == i else ''}") for i in range(2, 6)], [KeyboardButton("العودة إلى الإعدادات")]]
    await update.message.reply_text(message, reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True), parse_mode='Markdown')
    return SETTING_CONFIDENCE

async def set_confidence_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    setting_key = 'initial_confidence' if context.user_data.get('setting_type') == 'initial' else 'confirmation_confidence'
    try:
        new_value = int(update.message.text.split(" ")[0])
        bot_state[setting_key] = new_value
        save_bot_state()
        await update.message.reply_text(f"تم تحديث القيمة إلى: {new_value}")
    except (ValueError, IndexError):
        await update.message.reply_text("قيمة غير صالحة.")
    return await settings_menu(update, context)

async def set_indicator_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    params = bot_state.get('indicator_params', {})
    keyboard = [[KeyboardButton(f"{key.replace('_', ' ').title()} ({value})")] for key, value in params.items()]
    keyboard.append([KeyboardButton("العودة إلى الإعدادات")])
    await update.message.reply_text("اختر المؤشر لتعديل قيمته:", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True))
    return SETTING_INDICATOR

async def select_indicator_to_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    param_key_str = update.message.text.split(" (")[0].lower().replace(' ', '_')
    if param_key_str in bot_state.get('indicator_params', {}):
        context.user_data['param_to_set'] = param_key_str
        await update.message.reply_text(f"أرسل القيمة الرقمية الجديدة لـ {param_key_str}:", reply_markup=ReplyKeyboardMarkup([["إلغاء"]], resize_keyboard=True, one_time_keyboard=True))
        return AWAITING_VALUE
    await update.message.reply_text("خيار غير صالح.")
    return await settings_menu(update, context)

async def receive_new_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        new_value = int(update.message.text)
        param_key = context.user_data.get('param_to_set')
        if param_key:
            bot_state['indicator_params'][param_key] = new_value
            save_bot_state()
            await update.message.reply_text("✅ تم حفظ القيمة بنجاح!")
            del context.user_data['param_to_set']
    except (ValueError, TypeError):
        await update.message.reply_text("❌ قيمة غير صالحة. يرجى إرسال رقم صحيح فقط.")
    return await set_indicator_menu(update, context)

async def set_macd_strategy_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    current_strategy = bot_state.get('macd_strategy', 'dynamic')
    keyboard = [[KeyboardButton(f"ديناميكي (جودة عالية) {'✅' if current_strategy == 'dynamic' else ''}")],
                [KeyboardButton(f"بسيط (كمية أكبر) {'✅' if current_strategy == 'simple' else ''}")],
                [KeyboardButton("العودة إلى الإعدادات")]]
    await update.message.reply_text("اختر استراتيجية الماكد:", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True))
    return SETTING_MACD_STRATEGY

async def set_macd_strategy_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    choice = update.message.text
    if "ديناميكي" in choice: bot_state['macd_strategy'] = 'dynamic'
    elif "بسيط" in choice: bot_state['macd_strategy'] = 'simple'
    save_bot_state()
    await update.message.reply_text(f"تم تحديث استراتيجية الماكد إلى: {bot_state['macd_strategy']}")
    return await settings_menu(update, context)

# --- عرض الإحصائيات ---
async def show_statistics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """يعرض إحصائيات الإشارات."""
    if not signals_statistics:
        await update.message.reply_text("لا توجد إحصائيات لعرضها حتى الآن.")
        return SELECTING_ACTION

    message = "📊 **إحصائيات البوت**:\n\n"
    totals = {'initial': 0, 'confirmed': 0, 'failed': 0}
    for pair, stats in signals_statistics.items():
        initial = stats.get('initial', 0)
        confirmed = stats.get('confirmed', 0)
        failed = stats.get('failed_confirmation', 0)
        totals['initial'] += initial
        totals['confirmed'] += confirmed
        totals['failed'] += failed
        if initial > 0:
            message += f"🔹 **{pair}**: أولية: {initial}, مؤكدة: {confirmed}, فاشلة: {failed}\n"

    message += f"\n**المجموع الكلي:**\n"
    message += f"- إجمالي الإشارات الأولية: {totals['initial']}\n"
    message += f"- إجمالي الإشارات المؤكدة: {totals['confirmed']}\n"
    if totals['initial'] > 0:
        rate = (totals['confirmed'] / totals['initial']) * 100
        message += f"- نسبة نجاح التأكيد: {rate:.2f}%\n"

    await update.message.reply_text(message, parse_mode='Markdown')
    # لا نغير الحالة، ليبقى في القائمة الرئيسية
    return SELECTING_ACTION

async def done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """إنهاء المحادثة (للإلغاء)."""
    await update.message.reply_text("تم الإلغاء.")
    return await send_main_menu(update, context)

# --- نقطة انطلاق البوت ---
def main() -> None:
    """إعداد وتشغيل البوت."""
    if not all([TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, POLYGON_API_KEY]):
        logger.critical("خطأ فادح: أحد متغيرات البيئة (TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, POLYGON_API_KEY) غير موجود.")
        return

    # تحميل حالة البوت والإحصائيات من الملف عند بدء التشغيل
    load_bot_state()

    # --- بداية التعديل ---
    # 1. إنشاء JobQueue بشكل منفصل
    job_queue = JobQueue()

    # 2. إنشاء كائن التطبيق وتمرير job_queue إليه
    application = Application.builder().token(TELEGRAM_TOKEN).job_queue(job_queue).build()
    # --- نهاية التعديل ---
    
    # جدولة المهام المتكررة باستخدام job_queue مباشرة
    scan_interval = bot_state.get('scan_interval_seconds', 300)
    job_queue.run_repeating(check_for_signals, interval=scan_interval, first=10, name="SignalCheckJob")
    job_queue.run_repeating(confirm_pending_signals, interval=60, first=15, name="ConfirmationJob")

    # إعداد ConversationHandler الذي يدير جميع القوائم والأزرار
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            # الحالة: القائمة الرئيسية
            SELECTING_ACTION: [
                MessageHandler(filters.Regex(r'^حالة البوت:'), toggle_bot_status),
                MessageHandler(filters.Regex(r'^اختيار الأزواج$'), select_pairs_menu),
                MessageHandler(filters.Regex(r'^الإعدادات ⚙️$'), settings_menu),
                MessageHandler(filters.Regex(r'^📊 عرض الإحصائيات$'), show_statistics),
            ],
            # الحالة: قائمة اختيار الأزواج
            SELECTING_PAIR: [
                MessageHandler(filters.Regex(r'^(EUR|USD|AUD|CAD|CHF|JPY)\/.*(✅|❌)$'), toggle_pair),
                MessageHandler(filters.Regex(r'^العودة إلى القائمة الرئيسية$'), start),
            ],
            # الحالة: قائمة الإعدادات
            SETTINGS_MENU: [
                MessageHandler(filters.Regex(r'^📁 ملفات تعريف الاستراتيجية$'), strategy_profile_menu),
                MessageHandler(filters.Regex(r'^تحديد عتبة'), set_confidence_menu),
                MessageHandler(filters.Regex(r'^تعديل قيم المؤشرات$'), set_indicator_menu),
                MessageHandler(filters.Regex(r'^📊 استراتيجية الماكد$'), set_macd_strategy_menu),
                MessageHandler(filters.Regex(r'^العودة إلى القائمة الرئيسية$'), start),
            ],
            # الحالة: قائمة اختيار الاستراتيجية
            SELECTING_STRATEGY: [
                MessageHandler(filters.Regex(r'^تحميل:'), set_strategy_profile),
                MessageHandler(filters.Regex(r'^العودة إلى الإعدادات$'), settings_menu),
            ],
            # الحالة: قائمة تحديد عتبة الثقة
            SETTING_CONFIDENCE: [
                MessageHandler(filters.Regex(r'^\d مؤشرات'), set_confidence_value),
                MessageHandler(filters.Regex(r'^العودة إلى الإعدادات$'), settings_menu),
            ],
            # الحالة: قائمة تعديل المؤشرات
            SETTING_INDICATOR: [
                MessageHandler(filters.Regex(r'^\w.* \(\d+\)$'), select_indicator_to_set),
                MessageHandler(filters.Regex(r'^العودة إلى الإعدادات$'), settings_menu),
            ],
            # الحالة: انتظار قيمة جديدة للمؤشر
            AWAITING_VALUE: [
                MessageHandler(filters.Regex(r'^\d+$'), receive_new_value),
                MessageHandler(filters.Regex(r'^إلغاء$'), set_indicator_menu),
            ],
            # الحالة: قائمة تعديل استراتيجية الماكد
            SETTING_MACD_STRATEGY: [
                MessageHandler(filters.Regex(r'^(ديناميكي|بسيط)'), set_macd_strategy_value),
                MessageHandler(filters.Regex(r'^العودة إلى الإعدادات$'), settings_menu),
            ],
        },
        fallbacks=[
            CommandHandler('start', start),
            MessageHandler(filters.Regex(r'^العودة إلى القائمة الرئيسية$'), start),
            MessageHandler(filters.Regex(r'^إلغاء$'), done),
            MessageHandler(filters.TEXT, start) 
        ],
        allow_reentry=True
    )

    # إضافة المعالج الرئيسي إلى التطبيق
    application.add_handler(conv_handler)

    # بدء خادم Flask في خيط منفصل (للتوافق مع Render)
    flask_thread = Thread(target=run_flask_app)
    flask_thread.daemon = True
    flask_thread.start()

    # بدء تشغيل البوت (يبدأ في الاستماع للرسائل)
    logger.info("البوت (إصدار v3.1 - مع إصلاح JobQueue) جاهز للعمل...")
    application.run_polling()

# هذا السطر يتأكد من أن دالة main() تعمل فقط عند تشغيل الملف مباشرة
if __name__ == '__main__':
    main()
    
