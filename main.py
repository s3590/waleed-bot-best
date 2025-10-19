import logging
import json
import os  # <-- التغيير الأول: استيراد مكتبة os
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, JobQueue
import pandas as pd
import requests
import ta

# --- التغيير الثاني: قراءة المتغيرات من بيئة Railway ---
TOKEN = os.environ.get('TELEGRAM_TOKEN')
# يتم تحويل CHAT_ID إلى عدد صحيح لأنه سيأتي كنص من متغيرات البيئة
CHAT_ID = int(os.environ.get('TELEGRAM_CHAT_ID'))
TWELVE_DATA_API_KEY = os.environ.get('TWELVE_DATA_API_KEY')

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Global state for bot
# يتم الآن تعبئة هذه القيم من المتغيرات التي تم تحميلها أعلاه
bot_state = {
    'running': False,
    'selected_pairs': [],
    'selected_timeframes': [],
    'chat_id': CHAT_ID,
    'twelve_data_api_key': TWELVE_DATA_API_KEY,
}

# --- Persistence (Saving/Loading bot_state) ---
STATE_FILE = 'bot_state.json'

def save_bot_state():
    with open(STATE_FILE, 'w') as f:
        # --- التغيير الثالث: استثناء المتغيرات الحساسة من الحفظ في الملف ---
        # هذا يضمن عدم كتابة التوكن أو مفتاح API في ملف الحالة
        state_to_save = {k: v for k, v in bot_state.items() if k not in ['chat_id', 'twelve_data_api_key']}
        json.dump(state_to_save, f)
    logger.info("Bot state saved.")

def load_bot_state():
    global bot_state
    try:
        with open(STATE_FILE, 'r') as f:
            loaded_state = json.load(f)
            bot_state.update(loaded_state) # Update existing state to preserve default values if not in file
        logger.info("Bot state loaded.")
    except FileNotFoundError:
        logger.warning("Bot state file not found. Starting with default state.")
    except Exception as e:
        logger.error(f"Error loading bot state: {e}")

# --- باقي الكود يبقى كما هو تمامًا ---

# --- Telegram Bot UI Functions ---
async def get_main_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("تشغيل البوت", callback_data='start_bot')],
        [InlineKeyboardButton("إيقاف البوت", callback_data='stop_bot')],
        [InlineKeyboardButton("اختيار الأزواج", callback_data='select_pairs')],
        [InlineKeyboardButton("اختيار الفريمات", callback_data='select_timeframes')],
        [InlineKeyboardButton("الأزواج والفريمات المختارة", callback_data='view_selections')],
    ]
    return InlineKeyboardMarkup(keyboard)

async def get_pairs_keyboard() -> InlineKeyboardMarkup:
    all_pairs = [
        "EUR/USD", "AED/CNY", "USD/JPY", "USD/CHF", "AUD/USD", "USD/CAD",
        "EUR/JPY", "AUD/JPY", "CHF/JPY", "EUR/CHF", "AUD/CHF", "CAD/CHF",
        "EUR/AUD", "EUR/CAD", "AUD/CAD", "AUD/NZD", "CAD/JPY"
    ]
    keyboard = []
    for pair in all_pairs:
        status = "✅" if pair in bot_state['selected_pairs'] else ""
        keyboard.append([InlineKeyboardButton(f"{pair} {status}", callback_data=f'toggle_pair_{pair}')])
    keyboard.append([InlineKeyboardButton("العودة للقائمة الرئيسية", callback_data='main_menu')])
    return InlineKeyboardMarkup(keyboard)

async def get_timeframes_keyboard() -> InlineKeyboardMarkup:
    all_timeframes = ["1min", "5min", "15min"]
    keyboard = []
    for tf in all_timeframes:
        status = "✅" if tf in bot_state['selected_timeframes'] else ""
        keyboard.append([InlineKeyboardButton(f"{tf.replace('min', ' دقيقة')} {status}", callback_data=f'toggle_tf_{tf}')])
    keyboard.append([InlineKeyboardButton("العودة للقائمة الرئيسية", callback_data='main_menu')])
    return InlineKeyboardMarkup(keyboard)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a message with inline buttons on /start."""
    reply_markup = await get_main_keyboard()
    if update.message:
        await update.message.reply_text('مرحباً بك في بوت اشارات تداول بوكيت اوبشن! يرجى اختيار أحد الخيارات:', reply_markup=reply_markup)
    else: # For callback queries returning to main menu
        await context.bot.edit_message_text(chat_id=update.callback_query.message.chat_id,
                                            message_id=update.callback_query.message.message_id,
                                            text='مرحباً بك في بوت اشارات تداول بوكيت اوبشن! يرجى اختيار أحد الخيارات:', reply_markup=reply_markup)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Parses the CallbackQuery and updates the message text."""
    query = update.callback_query
    await query.answer()

    if query.data == 'start_bot':
        if not bot_state['running']:
            bot_state['running'] = True
            save_bot_state()
            await query.edit_message_text(text="تم تشغيل البوت وبدأ في مراقبة الإشارات.")
            # Ensure job is not duplicated if already running
            current_jobs = context.job_queue.get_jobs_by_name('signal_check')
            if not current_jobs:
                context.job_queue.run_repeating(check_for_signals, interval=60, first=0, name='signal_check', data=query.message.chat_id)
        else:
            await query.edit_message_text(text="البوت يعمل بالفعل.")
    elif query.data == 'stop_bot':
        if bot_state['running']:
            bot_state['running'] = False
            save_bot_state()
            await query.edit_message_text(text="تم إيقاف البوت.")
            for job in context.job_queue.get_jobs_by_name('signal_check'):
                job.schedule_removal()
        else:
            await query.edit_message_text(text="البوت متوقف بالفعل.")
    elif query.data == 'select_pairs':
        reply_markup = await get_pairs_keyboard()
        await query.edit_message_text(text="اختر الأزواج التي ترغب في مراقبتها:", reply_markup=reply_markup)
    elif query.data == 'select_timeframes':
        reply_markup = await get_timeframes_keyboard()
        await query.edit_message_text(text="اختر الفريمات التي ترغب في مراقبتها:", reply_markup=reply_markup)
    elif query.data.startswith('toggle_pair_'):
        pair = query.data.replace('toggle_pair_', '')
        if pair in bot_state['selected_pairs']:
            bot_state['selected_pairs'].remove(pair)
            message_text = f"تم إزالة الزوج: {pair}."
        else:
            bot_state['selected_pairs'].append(pair)
            message_text = f"تم إضافة الزوج: {pair}."
        save_bot_state()
        reply_markup = await get_pairs_keyboard()
        await query.edit_message_text(text=f"{message_text}\nالأزواج المختارة حالياً: {', '.join(bot_state['selected_pairs']) or 'لا يوجد'}", reply_markup=reply_markup)
    elif query.data.startswith('toggle_tf_'):
        timeframe = query.data.replace('toggle_tf_', '')
        if timeframe in bot_state['selected_timeframes']:
            bot_state['selected_timeframes'].remove(timeframe)
            message_text = f"تم إزالة الفريم: {timeframe.replace('min', ' دقيقة')}."
        else:
            bot_state['selected_timeframes'].append(timeframe)
            message_text = f"تم إضافة الفريم: {timeframe.replace('min', ' دقيقة')}."
        save_bot_state()
        reply_markup = await get_timeframes_keyboard()
        await query.edit_message_text(text=f"{message_text}\nالفريمات المختارة حالياً: {', '.join([tf.replace('min', ' دقيقة') for tf in bot_state['selected_timeframes']]) or 'لا يوجد'}", reply_markup=reply_markup)
    elif query.data == 'view_selections':
        pairs_str = ', '.join(bot_state['selected_pairs']) or 'لا يوجد'
        timeframes_str = ', '.join([tf.replace('min', ' دقيقة') for tf in bot_state['selected_timeframes']]) or 'لا يوجد'
        status_text = f"حالة البوت: {'يعمل' if bot_state['running'] else 'متوقف'}\nالأزواج المختارة: {pairs_str}\nالفريمات المختارة: {timeframes_str}"
        reply_markup = await get_main_keyboard()
        await query.edit_message_text(text=status_text, reply_markup=reply_markup)
    elif query.data == 'main_menu':
        await start_command(update, context)

# --- Data Fetching and Signal Generation ---
async def fetch_historical_data(pair: str, timeframe: str, outputsize: int = 100) -> pd.DataFrame:
    api_key = bot_state["twelve_data_api_key"]
    symbol = pair.replace("/", "/") # Twelve Data uses EUR/USD format

    twelve_data_interval_map = {
        "1min": "1min",
        "5min": "5min",
        "15min": "15min",
    }
    td_interval = twelve_data_interval_map.get(timeframe, "1min")

    url = f"https://api.twelvedata.com/time_series?symbol={symbol}&interval={td_interval}&outputsize={outputsize}&apikey={api_key}"

    try:
        response = requests.get(url)
        response.raise_for_status() # Raise an exception for HTTP errors
        data = response.json()

        if "values" in data:
            df = pd.DataFrame(data["values"])
            df["datetime"] = pd.to_datetime(df["datetime"])
            df = df.set_index("datetime")
            df = df.astype(float) # Convert all data columns to float
            df.rename(columns={
                "open": "Open",
                "high": "High",
                "low": "Low",
                "close": "Close",
                "volume": "Volume"
            }, inplace=True)
            return df.sort_index()
        else:
            logger.warning(f"No \"values\" in data for {pair} with timeframe {timeframe}: {data}")
            return pd.DataFrame()
    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching data from Twelve Data for {pair}: {e}")
        return pd.DataFrame()
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
        return pd.DataFrame()

async def analyze_and_generate_signal(data: pd.DataFrame, pair: str, timeframe: str) -> dict or None:
    if data.empty or len(data) < 30: # Need enough data for indicators, e.g., 30 for SMA(20) + some buffer
        return None

    # Apply various technical indicators
    data["rsi"] = ta.momentum.RSIIndicator(data["Close"], window=14).rsi()
    macd = ta.trend.MACD(data["Close"])
    data["macd"] = macd.macd()
    data["macd_signal"] = macd.macd_signal()
    bollinger = ta.volatility.BollingerBands(data["Close"])
    data["bb_bbm"] = bollinger.bollinger_mavg()
    data["bb_bbh"] = bollinger.bollinger_hband()
    data["bb_bbl"] = bollinger.bollinger_lband()
    stoch = ta.momentum.StochasticOscillator(data["High"], data["Low"], data["Close"])
    data["stoch_k"] = stoch.stoch()
    data["stoch_d"] = stoch.stoch_signal()
    data["sma_20"] = ta.trend.SMAIndicator(data["Close"], window=20).sma_indicator()

    # Ensure all indicator columns are present and not NaN for the last few rows
    data.dropna(inplace=True)
    if data.empty or len(data) < 2: # Need at least 2 rows for comparison
        return None

    last_row = data.iloc[-1]
    prev_row = data.iloc[-2]

    signal_direction = None
    confidence_score = 0 # Start with a base score

    # --- RSI Logic ---
    if last_row["rsi"] > 30 and prev_row["rsi"] <= 30: # Crossover from oversold
        signal_direction = "صعود ⬆️"
        confidence_score += 1
    elif last_row["rsi"] < 70 and prev_row["rsi"] >= 70: # Crossover from overbought
        signal_direction = "هبوط ⬇️"
        confidence_score += 1

    # --- MACD Logic ---
    if last_row["macd"] > last_row["macd_signal"] and prev_row["macd"] <= prev_row["macd_signal"]:
        if signal_direction == "صعود ⬆️": confidence_score += 1
        elif signal_direction is None: signal_direction = "صعود ⬆️"; confidence_score += 1
    elif last_row["macd"] < last_row["macd_signal"] and prev_row["macd"] >= prev_row["macd_signal"]:
        if signal_direction == "هبوط ⬇️": confidence_score += 1
        elif signal_direction is None: signal_direction = "هبوط ⬇️"; confidence_score += 1

    # --- Bollinger Bands Logic ---
    if last_row["Close"] < last_row["bb_bbl"] and prev_row["Close"] >= prev_row["bb_bbl"]:
        if signal_direction == "صعود ⬆️": confidence_score += 1
        elif signal_direction is None: signal_direction = "صعود ⬆️"; confidence_score += 1
    elif last_row["Close"] > last_row["bb_bbh"] and prev_row["Close"] <= prev_row["bb_bbh"]:
        if signal_direction == "هبوط ⬇️": confidence_score += 1
        elif signal_direction is None: signal_direction = "هبوط ⬇️"; confidence_score += 1

    # --- Stochastic Oscillator Logic ---
    if last_row["stoch_k"] > last_row["stoch_d"] and prev_row["stoch_k"] <= prev_row["stoch_d"] and last_row["stoch_k"] < 20:
        if signal_direction == "صعود ⬆️": confidence_score += 1
        elif signal_direction is None: signal_direction = "صعود ⬆️"; confidence_score += 1
    elif last_row["stoch_k"] < last_row["stoch_d"] and prev_row["stoch_k"] >= prev_row["stoch_d"] and last_row["stoch_k"] > 80:
        if signal_direction == "هبوط ⬇️": confidence_score += 1
        elif signal_direction is None: signal_direction = "هبوط ⬇️"; confidence_score += 1

    # Calculate final confidence based on score
    if signal_direction:
        confidence = 60 + (confidence_score * 10) # Base 60, +10 for each confirming indicator
        confidence = min(95, max(60, confidence)) # Ensure within 60-95 range
    else:
        confidence = 0 # No signal, or conflicting signals

    if signal_direction and confidence >= 70: # Only generate signal if confidence is reasonable
        entry_time = (datetime.now() + timedelta(seconds=30)).strftime("%H:%M:%S")
        return {
            "pair": pair,
            "timeframe": timeframe,
            "entry_time": entry_time,
            "direction": signal_direction,
            "confidence": confidence,
            "duration": f"{int(timeframe.replace('min', '')) * 60} ثانية" if timeframe.endswith('min') else "غير محدد"
        }
    return None

# --- Signal Sending Logic ---
async def send_signal_to_telegram(context: ContextTypes.DEFAULT_TYPE, signal: dict) -> None:
    message = (
        f"⚠️ إشارة لـ {signal['pair']} OTC\n"
        f"🕒 الفريم: {signal['timeframe'].replace('min', ' دقيقة')}\n"
        f"⏰ وقت الدخول: {signal['entry_time']}\n"
        f"📈 الاتجاه: {signal['direction']}\n"
        f"🔎 الثقة: {signal['confidence']}%\n"
        f"⏳ مدة الصفقة: {signal['duration']}"
    )
    await context.bot.send_message(chat_id=bot_state["chat_id"], text=message)

# --- Periodic Signal Checking ---
last_signal_time = {}

async def check_for_signals(context: ContextTypes.DEFAULT_TYPE) -> None:
    global last_signal_time

    if not bot_state["running"]:
        return

    if not bot_state['selected_pairs'] or not bot_state['selected_timeframes']:
        logger.info("No pairs or timeframes selected. Skipping signal check.")
        return

    current_minute = datetime.now().strftime("%Y-%m-%d %H:%M")

    for pair in bot_state['selected_pairs']:
        for timeframe in bot_state['selected_timeframes']:
            key = f"{pair}_{timeframe}"
            # Prevent duplicate signals for the same pair/timeframe within the same minute
            if key in last_signal_time and last_signal_time[key] == current_minute:
                logger.info(f"Skipping signal for {pair} {timeframe} as one was sent in the current minute.")
                continue

            signal = await analyze_and_generate_signal(await fetch_historical_data(pair, timeframe), pair, timeframe)
            if signal:
                await send_signal_to_telegram(context, signal)
                last_signal_time[key] = current_minute
            else:
                logger.info(f"No signal generated for {pair} {timeframe} at {current_minute}")

# --- Main Bot Setup ---
def main() -> None:
    """Run the bot."""
    # التأكد من وجود التوكن قبل البدء
    if not TOKEN:
        logger.critical("Error: TELEGRAM_TOKEN environment variable is not set. The bot cannot start.")
        return

    load_bot_state() # Load state at startup

    application = Application.builder().token(TOKEN).build()
    job_queue = application.job_queue

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CallbackQueryHandler(button_callback))

    # If bot was running before restart, reschedule the job
    if bot_state['running']:
        logger.info("Rescheduling signal check job from previous session.")
        job_queue.run_repeating(check_for_signals, interval=60, first=0, name='signal_check', data=bot_state['chat_id'])

    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
    