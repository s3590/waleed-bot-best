# -*- coding: utf-8 -*-
# ALNUSIRY BOT { VIP } - Version 4.7 (The Definitive UI & Engine Fix)
# Changelog:
# - FINAL, ABSOLUTE FIX for the "Edit Indicator Values" buttons not working.
# - Replaced the faulty Regex with a flexible and robust one (`^.* \(\d+\)$`) that matches all indicator buttons.
# - This is the definitive, stable version incorporating the Governor Engine and all UI fixes. All features are now fully operational.

import logging
import json
import os
import asyncio
from datetime import datetime, timedelta, timezone
from threading import Thread
from collections import deque

import pandas as pd
import requests
import ta
import talib

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ContextTypes, ConversationHandler
)

from flask import Flask

# --- الإعدادات الأساسية والمتغيرات العامة ---
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
POLYGON_API_KEY = os.environ.get('POLYGON_API_KEY')

STATE_FILE = 'bot_state.json'
STRATEGIES_DIR = 'strategies'

# --- إعداد تسجيل الأنشطة (Logging) ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- متغيرات محرك الحاكم (Governor Engine) ---
api_request_queue = asyncio.Queue()
api_call_timestamps = deque(maxlen=4)

# --- دالة إرسال الأخطاء إلى تليجرام ---
async def send_error_to_telegram(context: ContextTypes.DEFAULT_TYPE, error_message: str):
    logger.error(error_message)
    try:
        await context.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"🤖⚠️ **حدث خطأ في البوت** ⚠️🤖\n\n**التفاصيل:**\n`{error_message}`",
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"فشل حاد: لم يتمكن البوت من إرسال رسالة الخطأ. الخطأ: {e}")

# --- خادم ويب Flask ---
flask_app = Flask(__name__)
@flask_app.route('/')
def health_check():
    return "ALNUSIRY BOT (v4.7 Final Fix) is alive!", 200

def run_flask_app():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host='0.0.0.0', port=port)

# --- حالة البوت والبيانات ---
bot_state = {}
signals_statistics = {}
pending_signals = []
USER_DEFINED_PAIRS = [
    "EUR/USD", "USD/JPY", "USD/CHF", "AUD/USD", "USD/CAD",
    "EUR/JPY", "AUD/JPY", "CHF/JPY", "EUR/CHF", "AUD/CHF", "CAD/CHF",
    "EUR/AUD", "EUR/CAD", "AUD/CAD", "CAD/JPY"
]

# --- دوال إدارة الحالة والاستراتيجيات ---
def save_bot_state():
    try:
        state_to_save = {'bot_state': bot_state, 'signals_statistics': signals_statistics}
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(state_to_save, f, indent=4, ensure_ascii=False)
    except Exception as e:
        logger.error(f"فشل في حفظ حالة البوت: {e}")

def load_strategy_profile(profile_filename: str) -> bool:
    global bot_state
    filepath = os.path.join(STRATEGIES_DIR, profile_filename)
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            profile_settings = json.load(f)
        is_running, selected_pairs = bot_state.get('is_running', False), bot_state.get('selected_pairs', [])
        bot_state = profile_settings
        bot_state.update({'is_running': is_running, 'selected_pairs': selected_pairs})
        save_bot_state()
        return True
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.error(f"فشل تحميل ملف التعريف {profile_filename}: {e}")
        return False

def load_bot_state():
    global bot_state, signals_statistics
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            loaded_data = json.load(f)
            bot_state = loaded_data.get('bot_state', {})
            signals_statistics = loaded_data.get('signals_statistics', {})
        logger.info("تم تحميل حالة البوت من الملف.")
    except (FileNotFoundError, json.JSONDecodeError):
        logger.warning("ملف حالة البوت غير موجود. سيتم تحميل 'default.json'.")
        if not os.path.exists(STRATEGIES_DIR): os.makedirs(STRATEGIES_DIR)
        if not load_strategy_profile('default.json'):
            logger.error("فشل تحميل 'default.json'. سيتم استخدام إعدادات الطوارئ.")
            bot_state = {
                'is_running': False, 'selected_pairs': [], 'profile_name': 'الطوارئ',
                'initial_confidence': 3, 'confirmation_confidence': 4,
                'scan_interval_seconds': 5, 'confirmation_minutes': 5,
                'macd_strategy': 'dynamic', 'trend_filter_mode': 'M15',
                'indicator_params': {
                    'rsi_period': 14, 'macd_fast': 12, 'macd_slow': 26, 'macd_signal': 9,
                    'bollinger_period': 20, 'stochastic_period': 14, 'adx_period': 14,
                    'm15_ema_period': 50, 'h1_ema_period': 50
                }
            }
        signals_statistics = {}
        save_bot_state()

def get_strategy_files():
    if not os.path.exists(STRATEGIES_DIR): os.makedirs(STRATEGIES_DIR)
    return [f for f in os.listdir(STRATEGIES_DIR) if f.endswith('.json')]

# --- دوال التحليل الفني ---
async def execute_get_forex_data(pair: str, timeframe: str, limit: int, context: ContextTypes.DEFAULT_TYPE) -> pd.DataFrame:
    if not POLYGON_API_KEY:
        await send_error_to_telegram(context, "متغير البيئة POLYGON_API_KEY غير موجود!")
        return pd.DataFrame()
    
    polygon_ticker = f"C:{pair.replace('/', '')}"
    interval_map = {"M5": "5", "M15": "15", "H1": "1"}
    timespan_map = {"M5": "minute", "M15": "minute", "H1": "hour"}
    if timeframe not in interval_map: return pd.DataFrame()
    
    interval, timespan = interval_map[timeframe], timespan_map[timeframe]
    end_date = datetime.now(timezone.utc)
    if timespan == 'minute': start_date = end_date - timedelta(days=(int(interval) * limit) / (24 * 60) + 5)
    else: start_date = end_date - timedelta(days=(int(interval) * limit) / 24 + 10)
    
    url = (f"https://api.polygon.io/v2/aggs/ticker/{polygon_ticker}/range/{interval}/{timespan}/"
           f"{start_date.strftime('%Y-%m-%d')}/{end_date.strftime('%Y-%m-%d')}?adjusted=true&sort=asc&limit={limit}")
    headers = {"Authorization": f"Bearer {POLYGON_API_KEY}"}
    
    try:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, lambda: requests.get(url, headers=headers, timeout=20))
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
        await send_error_to_telegram(context, f"فشل الاتصال بـ Polygon API لجلب بيانات {pair} ({timeframe}): {e}")
        return pd.DataFrame()

def analyze_candlestick_patterns(data: pd.DataFrame) -> (int, int):
    buy_score, sell_score = 0, 0
    bullish_patterns = ['CDLHAMMER', 'CDLMORNINGSTAR', 'CDL3WHITESOLDIERS']
    bearish_patterns = ['CDLHANGINGMAN', 'CDLEVENINGSTAR', 'CDL3BLACKCROWS']
    for pattern in bullish_patterns:
        result = getattr(talib, pattern)(data['Open'], data['High'], data['Low'], data['Close'])
        if not result.empty and result.iloc[-1] > 0: buy_score += 1
    for pattern in bearish_patterns:
        result = getattr(talib, pattern)(data['Open'], data['High'], data['Low'], data['Close'])
        if not result.empty and result.iloc[-1] < 0: sell_score += 1
    return buy_score, sell_score

def analyze_signal_strength(df: pd.DataFrame, trend_m15: str, trend_h1: str) -> (int, int):
    buy, sell = 0, 0
    params = bot_state.get('indicator_params', {})
    trend_mode = bot_state.get('trend_filter_mode', 'M15')
    
    if trend_mode == 'M15' and trend_m15 == 'DOWN': buy = -99
    if trend_mode == 'M15' and trend_m15 == 'UP': sell = -99
    if trend_mode == 'H1' and trend_h1 == 'DOWN': buy = -99
    if trend_mode == 'H1' and trend_h1 == 'UP': sell = -99
    if trend_mode == 'M15_H1' and (trend_m15 == 'DOWN' or trend_h1 == 'DOWN'): buy = -99
    if trend_mode == 'M15_H1' and (trend_m15 == 'UP' or trend_h1 == 'UP'): sell = -99

    required_len = max(v for k, v in params.items() if 'period' in k)
    if df is None or df.empty or len(df) < required_len: return 0, 0
    
    df['rsi'] = ta.momentum.RSIIndicator(df['Close'], window=params.get('rsi_period', 14)).rsi()
    macd = ta.trend.MACD(df['Close'], window_fast=params.get('macd_fast', 12), window_slow=params.get('macd_slow', 26), window_sign=params.get('macd_signal', 9))
    df['macd'], df['macd_signal'] = macd.macd(), macd.macd_signal()
    bollinger = ta.volatility.BollingerBands(df['Close'], window=params.get('bollinger_period', 20))
    df['bb_h'], df['bb_l'] = bollinger.bollinger_hband(), bollinger.bollinger_lband()
    stoch = ta.momentum.StochasticOscillator(df['High'], df['Low'], df['Close'], window=params.get('stochastic_period', 14))
    df['stoch_k'], df['stoch_d'] = stoch.stoch(), stoch.stoch_signal()
    adx = ta.trend.ADXIndicator(df['High'], df['Low'], df['Close'], window=params.get('adx_period', 14))
    df['adx'], df['dmp'], df['dmn'] = adx.adx(), adx.adx_pos(), adx.adx_neg()

    df.dropna(inplace=True)
    if df.empty: return 0, 0
    last, prev = df.iloc[-1], df.iloc[-2] if len(df) > 1 else df.iloc[-1]

    if last['rsi'] < 30: buy += 1
    if last['rsi'] > 70: sell += 1
    
    macd_strategy = bot_state.get('macd_strategy', 'dynamic')
    if macd_strategy == 'dynamic':
        if last['macd'] > last['macd_signal'] and prev['macd'] <= prev['macd_signal'] and last['macd'] < 0: buy += 1
        if last['macd'] < last['macd_signal'] and prev['macd'] >= prev['macd_signal'] and last['macd'] > 0: sell += 1
    else:
        if last['macd'] > last['macd_signal'] and prev['macd'] <= prev['macd_signal']: buy += 1
        if last['macd'] < last['macd_signal'] and prev['macd'] >= prev['macd_signal']: sell += 1

    if last['Close'] < last['bb_l']: buy += 1
    if last['Close'] > last['bb_h']: sell += 1
    if last['stoch_k'] > last['stoch_d'] and last['stoch_k'] < 30: buy += 1
    if last['stoch_k'] < last['stoch_d'] and last['stoch_k'] > 70: sell += 1
    if last['adx'] > 25 and last['dmp'] > last['dmn']: buy += 1
    if last['adx'] > 25 and last['dmn'] > last['dmp']: sell += 1

    candle_buy, candle_sell = analyze_candlestick_patterns(df)
    buy += candle_buy; sell += candle_sell

    return max(0, buy), max(0, sell)

# --- محرك الحاكم والمنطق (Governor and Logic Engine) ---

async def governor_loop(context: ContextTypes.DEFAULT_TYPE):
    logger.info("محرك الحاكم (Governor) بدأ بالعمل...")
    while True:
        await asyncio.sleep(1)
        
        now = datetime.now(timezone.utc)
        
        while api_call_timestamps and (now - api_call_timestamps[0]).total_seconds() > 60:
            api_call_timestamps.popleft()

        if len(api_call_timestamps) < 4 and not api_request_queue.empty():
            request = await api_request_queue.get()
            
            api_call_timestamps.append(now)
            logger.info(f"الحاكم: السماح بطلب API. الطلبات في آخر دقيقة: {len(api_call_timestamps)}/4")

            pair, timeframe, limit, callback = request['pair'], request['timeframe'], request['limit'], request['callback']
            df = await execute_get_forex_data(pair, timeframe, limit, context)
            
            if callback:
                asyncio.create_task(callback(df, pair, context))
            
            api_request_queue.task_done()

async def logic_loop(context: ContextTypes.DEFAULT_TYPE):
    if not bot_state.get('is_running', False): return

    current_time = datetime.now(timezone.utc)
    confirmation_minutes = bot_state.get('confirmation_minutes', 5)
    
    signal_to_confirm = next((s for s in pending_signals if (current_time - s['timestamp']).total_seconds() / 60 >= confirmation_minutes), None)

    if signal_to_confirm:
        logger.info(f"المنطق: إضافة طلب تأكيد للزوج {signal_to_confirm['pair']} إلى الطابور.")
        pending_signals.remove(signal_to_confirm)
        
        async def confirmation_callback(df, pair, context):
            logger.info(f"الكول باك: تم استلام بيانات التأكيد للزوج {pair}.")
            initial_type = signal_to_confirm['type']
            if df is not None and not df.empty:
                buy_strength, sell_strength = analyze_signal_strength(df, 'NEUTRAL', 'NEUTRAL')
                
                confirmed = False
                if initial_type == 'BUY' and buy_strength > sell_strength and buy_strength >= bot_state.get('confirmation_confidence', 4): confirmed = True
                elif initial_type == 'SELL' and sell_strength > buy_strength and sell_strength >= bot_state.get('confirmation_confidence', 4): confirmed = True
                
                if confirmed:
                    strength_meter = '⬆️' * buy_strength if initial_type == 'BUY' else '⬇️' * sell_strength
                    message = (f"✅ إشارة مؤكدة ✅\n\nالزوج: {pair}\nالنوع: {initial_type}\nقوة التأكيد: {strength_meter}")
                    try:
                        await context.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)
                        if pair in signals_statistics: signals_statistics[pair]['confirmed'] += 1
                    except Exception as e:
                        await send_error_to_telegram(context, f"فشل إرسال رسالة التأكيد للزوج {pair}: {e}")
                else:
                    if pair in signals_statistics: signals_statistics[pair]['failed_confirmation'] += 1
                save_bot_state()
            else:
                if pair in signals_statistics: signals_statistics[pair]['failed_confirmation'] += 1
                save_bot_state()

        await api_request_queue.put({
            'pair': signal_to_confirm['pair'], 'timeframe': 'M5', 'limit': 200, 'callback': confirmation_callback
        })
        return

    selected_pairs = bot_state.get('selected_pairs', [])
    if not selected_pairs: return

    pair_index = context.bot_data.get('pair_index', 0)
    if pair_index >= len(selected_pairs): pair_index = 0

    pair_to_process = selected_pairs[pair_index]
    
    if any(req.get('metadata') == f"analysis_{pair_to_process}" for req in api_request_queue._queue):
        logger.info(f"المنطق: تخطي إضافة طلب تحليل لـ {pair_to_process}، يوجد طلب بالفعل في الطابور.")
        context.bot_data['pair_index'] = (pair_index + 1) % len(selected_pairs)
        return

    logger.info(f"المنطق: إضافة طلبات تحليل للزوج {pair_to_process} إلى الطابور.")

    context.bot_data[f'trend_data_{pair_to_process}'] = {}

    async def h1_callback(df, pair, context):
        if df is not None and not df.empty:
            params = bot_state.get('indicator_params', {})
            period = params.get('h1_ema_period', 50)
            df[f'ema_{period}'] = ta.trend.EMAIndicator(df['Close'], window=period).ema_indicator()
            if not df[f'ema_{period}'].dropna().empty:
                trend = 'UP' if df['Close'].iloc[-1] > df[f'ema_{period}'].iloc[-1] else 'DOWN'
                context.bot_data[f'trend_data_{pair}']['h1'] = trend
        
        await api_request_queue.put({
            'pair': pair, 'timeframe': 'M5', 'limit': 200, 'callback': m5_callback, 'metadata': f"analysis_{pair}"
        })

    async def m15_callback(df, pair, context):
        if df is not None and not df.empty:
            params = bot_state.get('indicator_params', {})
            period = params.get('m15_ema_period', 50)
            df[f'ema_{period}'] = ta.trend.EMAIndicator(df['Close'], window=period).ema_indicator()
            if not df[f'ema_{period}'].dropna().empty:
                trend = 'UP' if df['Close'].iloc[-1] > df[f'ema_{period}'].iloc[-1] else 'DOWN'
                context.bot_data[f'trend_data_{pair}']['m15'] = trend

        await api_request_queue.put({
            'pair': pair, 'timeframe': 'H1', 'limit': 150, 'callback': h1_callback, 'metadata': f"analysis_{pair}"
        })

    async def m5_callback(df, pair, context):
        if df is None or df.empty: return

        trend_m15 = context.bot_data.get(f'trend_data_{pair}', {}).get('m15', 'NEUTRAL')
        trend_h1 = context.bot_data.get(f'trend_data_{pair}', {}).get('h1', 'NEUTRAL')
        
        buy_strength, sell_strength = analyze_signal_strength(df, trend_m15, trend_h1)
        
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
            trend_text = f" (M15: {trend_m15}, H1: {trend_h1})"
            message = (f"🔔 إشارة أولية محتملة 🔔\n\nالزوج: {pair}\nالنوع: {signal_type}\nالقوة: {strength_meter} ({confidence})\nالاتجاه العام: {trend_text}\n"
                       f"سيتم التأكيد بعد {bot_state.get('confirmation_minutes', 5)} دقيقة.")
            await context.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)
        
        if f'trend_data_{pair}' in context.bot_data:
            del context.bot_data[f'trend_data_{pair}']

    await api_request_queue.put({
        'pair': pair_to_process, 'timeframe': 'M15', 'limit': 150, 'callback': m15_callback, 'metadata': f"analysis_{pair_to_process}"
    })

    context.bot_data['pair_index'] = (pair_index + 1) % len(selected_pairs)

# --- تعريف حالات المحادثة ---
(SELECTING_ACTION, SELECTING_PAIR, SETTINGS_MENU, SETTING_CONFIDENCE, 
 SETTING_INDICATOR, AWAITING_VALUE, SETTING_MACD_STRATEGY, 
 SELECTING_STRATEGY, SELECTING_TREND_FILTER) = range(9)

# --- دوال واجهة المستخدم ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.bot_data.setdefault('pair_index', 0)
    user_name = update.effective_user.first_name
    message = (f"أهلاً بك يا {user_name} في ALNUSIRY BOT {{ VIP }} - v4.7 👋\n\n"
               "مساعدك الذكي للتداول (الإصدار النهائي المستقر)")
    await update.message.reply_text(message)
    return await send_main_menu(update, context)

async def send_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, message_text: str = 'القائمة الرئيسية:') -> int:
    status_text = "يعمل ✅" if bot_state.get('is_running', False) else "متوقف ❌"
    main_menu_keyboard = [
        [KeyboardButton(f"حالة البوت: {status_text}")],
        [KeyboardButton("اختيار الأزواج"), KeyboardButton("الإعدادات ⚙️")],
        [KeyboardButton("📊 عرض الإحصائيات"), KeyboardButton("⚙️ عرض الإعدادات الحالية")]
    ]
    reply_markup = ReplyKeyboardMarkup(main_menu_keyboard, resize_keyboard=True)
    await update.message.reply_text(message_text, reply_markup=reply_markup)
    return SELECTING_ACTION

async def show_current_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    status = "يعمل ✅" if bot_state.get('is_running', False) else "متوقف ❌"
    pairs = ", ".join(bot_state.get('selected_pairs', [])) or "لا يوجد"
    profile = bot_state.get('profile_name', 'غير معروف')
    
    trend_modes = {'NONE': '⚫️ إيقاف', 'M15': '🟢 M15 فقط', 'H1': '🟡 H1 فقط', 'M15_H1': '🔴 M15 + H1'}
    trend_filter = trend_modes.get(bot_state.get('trend_filter_mode', 'M15'))
    
    initial_conf = bot_state.get('initial_confidence', 'N/A')
    final_conf = bot_state.get('confirmation_confidence', 'N/A')
    macd_strategy = bot_state.get('macd_strategy', 'N/A')

    params_text = "\n".join([f"   - {key.replace('_', ' ').title()}: {value}" for key, value in bot_state.get('indicator_params', {}).items()])

    message = (
        f"📋 **ملخص الإعدادات الحالية للبوت** 📋\n\n"
        f"🔹 **الحالة العامة:**\n"
        f"   - حالة التشغيل: {status}\n"
        f"   - ملف الاستراتيجية: {profile}\n\n"
        f"🔹 **إعدادات التداول:**\n"
        f"   - الأزواج المحددة: {pairs}\n"
        f"   - فلتر الاتجاه: {trend_filter}\n\n"
        f"🔹 **عتبات الثقة:**\n"
        f"   - الإشارة الأولية: {initial_conf} مؤشرات\n"
        f"   - التأكيد النهائي: {final_conf} مؤشرات\n\n"
        f"🔹 **استراتيجية الماكد:** {macd_strategy.title()}\n\n"
        f"🔹 **قيم المؤشرات الفنية:**\n"
        f"{params_text}"
    )
    await update.message.reply_text(message, parse_mode='Markdown')
    return SELECTING_ACTION

async def toggle_bot_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not bot_state.get('selected_pairs') and not bot_state.get('is_running'):
        await update.message.reply_text("⚠️ خطأ: يرجى تحديد زوج عملات واحد على الأقل قبل البدء.")
        return await send_main_menu(update, context, "")
    bot_state['is_running'] = not bot_state.get('is_running', False)
    if not bot_state['is_running']: context.bot_data['pair_index'] = 0
    save_bot_state()
    message = "✅ تم تشغيل البوت. سيبدأ محرك الحاكم الآن." if bot_state['is_running'] else "❌ تم إيقاف البوت."
    await update.message.reply_text(message)
    return await send_main_menu(update, context, "")

async def select_pairs_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    selected = bot_state.get('selected_pairs', [])
    message = "اختر زوجًا لإضافته أو إزالته. الأزواج المختارة حاليًا:\n" + (", ".join(selected) or "لا يوجد")
    pairs_keyboard = [[KeyboardButton(f"{pair} {'✅' if pair in selected else '❌'}")] for pair in USER_DEFINED_PAIRS]
    pairs_keyboard.append([KeyboardButton("العودة إلى القائمة الرئيسية")])
    reply_markup = ReplyKeyboardMarkup(pairs_keyboard, resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text(message, reply_markup=reply_markup)
    return SELECTING_PAIR

async def toggle_pair(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    يقوم بإضافة أو إزالة زوج من قائمة الأزواج المختارة.
    يعيد تعيين مؤشر الطابور لضمان أن الدورة التالية تبدأ من جديد.
    """
    # استخراج اسم الزوج من نص الزر، مع تجاهل الرموز التعبيرية
    pair = update.message.text.split(" ")[0]
    
    # التأكد من وجود قائمة الأزواج في حالة البوت
    if 'selected_pairs' not in bot_state:
        bot_state['selected_pairs'] = []
    
    # التحقق مما إذا كان الزوج موجودًا بالفعل في القائمة
    if pair in bot_state['selected_pairs']:
        # إذا كان موجودًا، قم بإزالته
        bot_state['selected_pairs'].remove(pair)
    elif pair in USER_DEFINED_PAIRS:
        # إذا لم يكن موجودًا (وهو زوج صالح)، قم بإضافته
        bot_state['selected_pairs'].append(pair)
    
    # خطوة حاسمة: إعادة تعيين مؤشر دورة التحليل إلى الصفر
    # هذا يضمن أن الدورة القادمة للمحرك ستبدأ من أول زوج في القائمة الجديدة
    context.bot_data['pair_index'] = 0
    
    # حفظ التغييرات في ملف الحالة
    save_bot_state()
    
    # إعادة عرض قائمة اختيار الأزواج المحدثة للمستخدم
    return await select_pairs_menu(update, context)


async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    settings_keyboard = [
        [KeyboardButton("📁 ملفات تعريف الاستراتيجية"), KeyboardButton("🚦 فلاتر الاتجاه")],
        [KeyboardButton("تحديد عتبة الإشارة الأولية"), KeyboardButton("تحديد عتبة التأكيد النهائي")],
        [KeyboardButton("تعديل قيم المؤشرات"), KeyboardButton("📊 استراتيجية الماكد")],
        [KeyboardButton("العودة إلى القائمة الرئيسية")]
    ]
    reply_markup = ReplyKeyboardMarkup(settings_keyboard, resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text("اختر الإعداد الذي تريد تعديله:", reply_markup=reply_markup)
    return SETTINGS_MENU

async def trend_filter_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    current_mode = bot_state.get('trend_filter_mode', 'M15')
    modes = {'NONE': '⚫️ إيقاف الفلترة', 'M15': '🟢 M15 فقط', 'H1': '🟡 H1 فقط', 'M15_H1': '🔴 M15 + H1'}
    keyboard = [[KeyboardButton(f"{text} {'✅' if current_mode == mode else ''}")] for mode, text in modes.items()]
    keyboard.append([KeyboardButton("العودة إلى الإعدادات")])
    await update.message.reply_text(f"اختر وضع فلتر الاتجاه (الحالي: {modes.get(current_mode, 'غير معروف')}):",
                                  reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True))
    return SELECTING_TREND_FILTER

async def set_trend_filter_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    choice = update.message.text
    new_mode = 'NONE'
    if 'M15 فقط' in choice: new_mode = 'M15'
    elif 'H1 فقط' in choice: new_mode = 'H1'
    elif 'M15 + H1' in choice: new_mode = 'M15_H1'
    bot_state['trend_filter_mode'] = new_mode
    save_bot_state()
    await update.message.reply_text(f"تم تحديث وضع فلتر الاتجاه إلى: {new_mode}")
    return await settings_menu(update, context)

async def strategy_profile_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    profiles = get_strategy_files()
    keyboard = [[KeyboardButton(f"تحميل: {profile}")] for profile in profiles]
    keyboard.append([KeyboardButton("العودة إلى الإعدادات")])
    current_profile = bot_state.get('profile_name', 'غير معروف')
    await update.message.reply_text(f"اختر ملف تعريف لتحميله. (الحالي: {current_profile})",
                                  reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True))
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
    keyboard = [[KeyboardButton(f"{i} مؤشرات {'✅' if current == i else ''}") for i in range(2, 7)], [KeyboardButton("العودة إلى الإعدادات")]]
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
        await update.message.reply_text(
            f"أرسل القيمة الرقمية الجديدة لـ **{param_key_str}**:",
            reply_markup=ReplyKeyboardMarkup([["إلغاء"]], resize_keyboard=True, one_time_keyboard=True),
            parse_mode='Markdown'
        )
        return AWAITING_VALUE
    await update.message.reply_text("خيار غير صالح. الرجاء الاختيار من القائمة.")
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

async def show_statistics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not signals_statistics:
        await update.message.reply_text("لا توجد إحصائيات لعرضها حتى الآن.")
        return SELECTING_ACTION

    message = "📊 **إحصائيات البوت**:\n\n"
    totals = {'initial': 0, 'confirmed': 0, 'failed': 0}
    for pair, stats in signals_statistics.items():
        initial, confirmed, failed = stats.get('initial', 0), stats.get('confirmed', 0), stats.get('failed_confirmation', 0)
        totals['initial'] += initial; totals['confirmed'] += confirmed; totals['failed'] += failed
        if initial > 0:
            message += f"🔹 **{pair}**: أولية: {initial}, مؤكدة: {confirmed}, فاشلة: {failed}\n"

    message += f"\n**المجموع الكلي:**\n- إجمالي الإشارات الأولية: {totals['initial']}\n- إجمالي الإشارات المؤكدة: {totals['confirmed']}\n"
    if totals['initial'] > 0:
        rate = (totals['confirmed'] / totals['initial']) * 100
        message += f"- نسبة نجاح التأكيد: {rate:.2f}%\n"

    await update.message.reply_text(message, parse_mode='Markdown')
    return SELECTING_ACTION

async def done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("تم الإلغاء.")
    return await send_main_menu(update, context)

# --- دالة التشغيل بعد التهيئة (للإصلاح النهائي) ---
async def post_init(application: Application) -> None:
    """
    يتم استدعاؤها بعد تهيئة التطبيق. المكان الآمن لبدء المهام الخلفية.
    """
    logger.info("Application initialized. Starting background tasks.")
    # استنساخ الكائن لضمان أن المهمة الخلفية لديها السياق الصحيح
    context = ContextTypes.DEFAULT_TYPE(application=application)
    asyncio.create_task(governor_loop(context))

# --- نقطة انطلاق البوت ---
def main() -> None:
    if not all([TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, POLYGON_API_KEY]):
        logger.critical("خطأ فادح: أحد متغيرات البيئة غير موجود.")
        return

    load_bot_state()
    
    application = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    
    application.bot_data['pair_index'] = 0
    
    logic_interval = bot_state.get('scan_interval_seconds', 5)
    application.job_queue.run_repeating(logic_loop, interval=logic_interval, first=5)

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            SELECTING_ACTION: [
                MessageHandler(filters.Regex(r'^حالة البوت:'), toggle_bot_status),
                MessageHandler(filters.Regex(r'^اختيار الأزواج$'), select_pairs_menu),
                MessageHandler(filters.Regex(r'^الإعدادات ⚙️$'), settings_menu),
                MessageHandler(filters.Regex(r'^📊 عرض الإحصائيات$'), show_statistics),
                MessageHandler(filters.Regex(r'^⚙️ عرض الإعدادات الحالية$'), show_current_settings),
            ],
            SELECTING_PAIR: [
                MessageHandler(filters.Regex(r'^(EUR|USD|AUD|CAD|CHF|JPY)\/.*(✅|❌)$'), toggle_pair),
                MessageHandler(filters.Regex(r'^العودة إلى القائمة الرئيسية$'), start),
            ],
            SETTINGS_MENU: [
                MessageHandler(filters.Regex(r'^📁 ملفات تعريف الاستراتيجية$'), strategy_profile_menu),
                MessageHandler(filters.Regex(r'^🚦 فلاتر الاتجاه$'), trend_filter_menu),
                MessageHandler(filters.Regex(r'^تحديد عتبة'), set_confidence_menu),
                MessageHandler(filters.Regex(r'^تعديل قيم المؤشرات$'), set_indicator_menu),
                MessageHandler(filters.Regex(r'^📊 استراتيجية الماكد$'), set_macd_strategy_menu),
                MessageHandler(filters.Regex(r'^العودة إلى القائمة الرئيسية$'), start),
            ],
            SELECTING_STRATEGY: [
                MessageHandler(filters.Regex(r'^تحميل:'), set_strategy_profile),
                MessageHandler(filters.Regex(r'^العودة إلى الإعدادات$'), settings_menu),
            ],
            SELECTING_TREND_FILTER: [
                MessageHandler(filters.Regex(r'^(⚫️|🟢|🟡|🔴)'), set_trend_filter_mode),
                MessageHandler(filters.Regex(r'^العودة إلى الإعدادات$'), settings_menu),
            ],
            SETTING_CONFIDENCE: [
                MessageHandler(filters.Regex(r'^\d مؤشرات'), set_confidence_value),
                MessageHandler(filters.Regex(r'^العودة إلى الإعدادات$'), settings_menu),
            ],
            SETTING_INDICATOR: [
                # الإصلاح النهائي والحاسم: تعبير نمطي مرن يقبل أي اسم مؤشر
                MessageHandler(filters.Regex(r'^.* \(\d+\)$'), select_indicator_to_set),
                MessageHandler(filters.Regex(r'^العودة إلى الإعدادات$'), settings_menu),
            ],
            AWAITING_VALUE: [
                MessageHandler(filters.Regex(r'^\d+$'), receive_new_value),
                MessageHandler(filters.Regex(r'^إلغاء$'), set_indicator_menu),
            ],
            SETTING_MACD_STRATEGY: [
                MessageHandler(filters.Regex(r'^(ديناميكي|بسيط)'), set_macd_strategy_value),
                MessageHandler(filters.Regex(r'^العودة إلى الإعدادات$'), settings_menu),
            ],
        },
        fallbacks=[
            CommandHandler('start', start),
            MessageHandler(filters.Regex(r'^العودة'), start),
            MessageHandler(filters.TEXT, start) 
        ],
        allow_reentry=True
    )

    application.add_handler(conv_handler)

    flask_thread = Thread(target=run_flask_app)
    flask_thread.daemon = True
    flask_thread.start()

    logger.info("البوت (إصدار v4.7 النهائي) جاهز للعمل...")
    application.run_polling()

if __name__ == '__main__':
    main()
