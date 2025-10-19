import logging
import json
import os
import asyncio
from datetime import datetime, timedelta
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ContextTypes, ConversationHandler, CallbackQueryHandler
)
import pandas as pd
import requests
import ta

# --- قراءة المتغيرات الحساسة ---
TOKEN = os.environ.get('TELEGRAM_TOKEN')
CHAT_ID = int(os.environ.get('TELEGRAM_CHAT_ID'))
TWELVE_DATA_API_KEY = os.environ.get('TWELVE_DATA_API_KEY')

# --- إعدادات التسجيل ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- ** قائمة الأزواج المعتمدة من قبل المستخدم ** ---
USER_DEFINED_PAIRS = [
    "EUR/USD", "AED/CNY", "USD/JPY", "USD/CHF", "AUD/USD", "USD/CAD",
    "EUR/JPY", "AUD/JPY", "CHF/JPY", "EUR/CHF", "AUD/CHF", "CAD/CHF",
    "EUR/AUD", "EUR/CAD", "AUD/CAD", "CAD/JPY"
]

# --- الإعدادات الافتراضية ---
DEFAULT_SETTINGS = {
    'running': False, 'selected_pairs': [], 'confidence_threshold': 3,
    'indicator_params': {
        'rsi_period': 14, 'macd_fast': 12, 'macd_slow': 26, 'macd_signal': 9,
        'bollinger_period': 20, 'stochastic_period': 14, 'atr_period': 14, 'adx_period': 14
    }
}

# --- حالة البوت وذاكرة الإشارات ---
bot_state = DEFAULT_SETTINGS.copy()
bot_state.update({'chat_id': CHAT_ID, 'twelve_data_api_key': TWELVE_DATA_API_KEY})
last_signal_candle = {}

# --- حفظ وتحميل الحالة ---
STATE_FILE = 'bot_settings.json'
def save_bot_settings():
    settings_to_save = {k: v for k, v in bot_state.items() if k in DEFAULT_SETTINGS}
    with open(STATE_FILE, 'w') as f: json.dump(settings_to_save, f, indent=4)
    logger.info("Bot settings saved.")

def load_bot_settings():
    global bot_state
    try:
        with open(STATE_FILE, 'r') as f: bot_state.update(json.load(f))
        logger.info("Bot settings loaded.")
    except (FileNotFoundError, json.JSONDecodeError):
        logger.warning("Settings file not found or invalid. Starting with default settings.")
        save_bot_settings()

# --- حالات المحادثة ---
(SELECTING_ACTION, SELECTING_PAIR, SETTINGS_MENU, SETTING_CONFIDENCE,
 SETTING_INDICATOR, AWAITING_VALUE) = range(6)

# --- واجهة المستخدم ---
async def send_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, message_text='القائمة الرئيسية:'):
    status = "يعمل ✅" if bot_state['running'] else "متوقف ❌"
    main_menu_keyboard = [
        [KeyboardButton(f"حالة البوت: {status}")],
        [KeyboardButton("اختيار الأزواج"), KeyboardButton("الإعدادات ⚙️")],
        [KeyboardButton("🔍 اكتشاف الأزواج النشطة")],
        [KeyboardButton("عرض الإعدادات الحالية")]
    ]
    reply_markup = ReplyKeyboardMarkup(main_menu_keyboard, resize_keyboard=True)
    if update.callback_query:
        await update.callback_query.message.reply_text(message_text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(message_text, reply_markup=reply_markup)
    return SELECTING_ACTION

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text('مرحباً بك في بوت إشارات التداول!\nاستخدم الأزرار أدناه للتحكم.', parse_mode='Markdown')
    return await send_main_menu(update, context)

async def toggle_bot_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    bot_state['running'] = not bot_state['running']
    save_bot_settings()
    if bot_state['running']:
        await update.message.reply_text("تم تشغيل البوت. سيبدأ في البحث عن إشارات.")
        if not context.job_queue.get_jobs_by_name('signal_check'):
            context.job_queue.run_repeating(check_for_signals, interval=60, first=1, name='signal_check')
    else:
        await update.message.reply_text("تم إيقاف البوت.")
        for job in context.job_queue.get_jobs_by_name('signal_check'): job.schedule_removal()
    return await send_main_menu(update, context)

async def select_pairs_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = "اختر زوجًا لإضافته أو إزالته. الأزواج المختارة حاليًا:\n" + (", ".join(bot_state['selected_pairs']) or "لا يوجد")
    pairs_keyboard = [[KeyboardButton(f"{pair} {'✅' if pair in bot_state['selected_pairs'] else '❌'}")] for pair in USER_DEFINED_PAIRS]
    pairs_keyboard.append([KeyboardButton("العودة إلى القائمة الرئيسية")])
    reply_markup = ReplyKeyboardMarkup(pairs_keyboard, resize_keyboard=True)
    await update.message.reply_text(message, reply_markup=reply_markup)
    return SELECTING_PAIR

async def toggle_pair(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    pair = update.message.text.split(" ")[0]
    if pair in bot_state['selected_pairs']: bot_state['selected_pairs'].remove(pair)
    else: bot_state['selected_pairs'].append(pair)
    save_bot_settings()
    return await select_pairs_menu(update, context)

async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    settings_keyboard = [[KeyboardButton("تحديد عتبة الثقة")], [KeyboardButton("تعديل قيم المؤشرات")], [KeyboardButton("العودة إلى القائمة الرئيسية")]]
    reply_markup = ReplyKeyboardMarkup(settings_keyboard, resize_keyboard=True)
    await update.message.reply_text("اختر الإعداد الذي تريد تعديله:", reply_markup=reply_markup)
    return SETTINGS_MENU

async def set_confidence_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    current = bot_state['confidence_threshold']
    message = f"اختر الحد الأدنى من المؤشرات المتوافقة المطلوبة لإرسال إشارة.\nالحالي: {current}"
    keyboard = [
        [KeyboardButton(f"توافق مؤشرين (مغامر) {'✅' if current == 2 else ''}")],
        [KeyboardButton(f"توافق 3 مؤشرات (متوازن) {'✅' if current == 3 else ''}")],
        [KeyboardButton(f"توافق 4 مؤشرات (متحفظ) {'✅' if current == 4 else ''}")],
        [KeyboardButton("العودة إلى الإعدادات")]
    ]
    await update.message.reply_text(message, reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
    return SETTING_CONFIDENCE

async def set_confidence_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    choice = update.message.text
    if "مؤشرين" in choice: bot_state['confidence_threshold'] = 2
    elif "3 مؤشرات" in choice: bot_state['confidence_threshold'] = 3
    elif "4 مؤشرات" in choice: bot_state['confidence_threshold'] = 4
    save_bot_settings()
    await update.message.reply_text(f"تم تحديث عتبة الثقة إلى: {bot_state['confidence_threshold']}")
    return await set_confidence_menu(update, context)

async def set_indicator_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    params = bot_state['indicator_params']
    keyboard = [[KeyboardButton(f"{key.replace('_', ' ').title()} ({value})")] for key, value in params.items()]
    keyboard.append([KeyboardButton("العودة إلى الإعدادات")])
    await update.message.reply_text("اختر المؤشر الذي تريد تعديل قيمته:", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
    return SETTING_INDICATOR

async def select_indicator_to_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    param_key_str = update.message.text.split(" (")[0].lower().replace(' ', '_')
    if param_key_str in bot_state['indicator_params']:
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

async def view_current_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    pairs_str = ", ".join(bot_state['selected_pairs']) or "لا يوجد"
    params = bot_state['indicator_params']
    message = (f"**⚙️ الإعدادات الحالية**\n\n"
               f"**الفريم:** 5 دقائق\n"
               f"**الأزواج:** {pairs_str}\n"
               f"**عتبة الثقة:** {bot_state['confidence_threshold']} مؤشرات\n\n"
               f"**قيم المؤشرات:**\n" +
               "\n".join([f"- {key.replace('_', ' ').title()}: {value}" for key, value in params.items()]))
    await update.message.reply_text(message, parse_mode='Markdown')
    return SELECTING_ACTION

# --- اكتشاف الأزواج النشطة ---
async def analyze_pair_activity(pair: str) -> dict or None:
    try:
        data = await fetch_historical_data(pair, 100)
        params = bot_state['indicator_params']
        if data.empty or len(data) < max(params['adx_period'], params['atr_period']): return None
        adx_value = ta.trend.ADXIndicator(data['High'], data['Low'], data['Close'], window=params['adx_period']).adx().iloc[-1]
        atr_percent = (ta.volatility.ATRIndicator(data['High'], data['Low'], data['Close'], window=params['atr_period']).atr().iloc[-1] / data['Close'].iloc[-1]) * 100
        return {'pair': pair, 'adx': adx_value, 'atr_percent': atr_percent}
    except Exception as e:
        logger.error(f"Error analyzing activity for {pair}: {e}")
        return None

async def find_active_pairs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("🔍 جاري تحليل نشاط السوق... قد يستغرق هذا بعض الوقت.", reply_markup=ReplyKeyboardMarkup([[]], resize_keyboard=True))
    tasks = [analyze_pair_activity(pair) for pair in USER_DEFINED_PAIRS]
    results = [res for res in await asyncio.gather(*tasks) if res is not None]
    if not results:
        return await send_main_menu(update, context, "عذرًا، لم أتمكن من تحليل السوق.")
    results.sort(key=lambda x: x['adx'] + (x['atr_percent'] * 20), reverse=True)
    top_pairs = results[:4]
    message = "📈 **أفضل الأزواج النشطة للتداول الآن:**\n\n"
    keyboard = []
    for res in top_pairs:
        reason = "اتجاه قوي" if res['adx'] > 25 else "تقلب جيد" if res['atr_percent'] > 0.04 else "نشاط معتدل"
        message += f"• **{res['pair']}** ({reason})\n"
        keyboard.append([InlineKeyboardButton(f"✅ تفعيل مراقبة {res['pair']}", callback_data=f"addpair_{res['pair']}")])
    keyboard.append([InlineKeyboardButton("➕ تفعيل مراقبة الكل", callback_data="addpair_all_" + ",".join([p['pair'] for p in top_pairs]))])
    await update.message.reply_text(message, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return await send_main_menu(update, context, message_text="اختر إجراءً آخر من القائمة الرئيسية:")

async def add_pair_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    action, payload = query.data.split('_', 1)
    pairs_to_add = payload.split(',') if 'all' in action else [payload]
    added_now = [pair for pair in pairs_to_add if pair not in bot_state['selected_pairs']]
    if added_now:
        bot_state['selected_pairs'].extend(added_now)
        save_bot_settings()
        await query.edit_message_text(text=f"تم تفعيل المراقبة للأزواج:\n{', '.join(added_now)}")
    else:
        await query.edit_message_text(text="الأزواج المحددة مفعلة بالفعل.")

# --- منطق التحليل والإشارات ---
async def fetch_historical_data(pair: str, outputsize: int = 100) -> pd.DataFrame:
    api_key = bot_state["twelve_data_api_key"]
    if not api_key: return pd.DataFrame()
    url = f"https://api.twelvedata.com/time_series?symbol={pair}&interval=5min&outputsize={outputsize}&apikey={api_key}"
    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        if "values" in data:
            df = pd.DataFrame(data["values"])
            df["datetime"] = pd.to_datetime(df["datetime"])
            df = df.set_index("datetime").astype(float)
            df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close"}, inplace=True)
            return df.sort_index()
        return pd.DataFrame()
    except Exception as e:
        logger.error(f"Error fetching data for {pair}: {e}")
        return pd.DataFrame()

async def analyze_and_generate_signal(data: pd.DataFrame, pair: str) -> dict or None:
    params = bot_state['indicator_params']
    if data.empty or len(data) < max(params.values()): return None
    data["rsi"] = ta.momentum.RSIIndicator(data["Close"], window=params['rsi_period']).rsi()
    macd = ta.trend.MACD(data["Close"], window_fast=params['macd_fast'], window_slow=params['macd_slow'], window_sign=params['macd_signal'])
    data["macd"], data["macd_signal"] = macd.macd(), macd.macd_signal()
    bollinger = ta.volatility.BollingerBands(data["Close"], window=params['bollinger_period'])
    data["bb_h"], data["bb_l"] = bollinger.bollinger_hband(), bollinger.bollinger_lband()
    stoch = ta.momentum.StochasticOscillator(data["High"], data["Low"], data["Close"], window=params['stochastic_period'])
    data["stoch_k"], data["stoch_d"] = stoch.stoch(), stoch.stoch_signal()
    data.dropna(inplace=True)
    if data.empty or len(data) < 2: return None
    last, prev = data.iloc[-1], data.iloc[-2]
    buy_signals, sell_signals = 0, 0
    if last["rsi"] > 30 and prev["rsi"] <= 30: buy_signals += 1
    if last["rsi"] < 70 and prev["rsi"] >= 70: sell_signals += 1
    if last["macd"] > last["macd_signal"] and prev["macd"] <= prev["macd_signal"]: buy_signals += 1
    if last["macd"] < last["macd_signal"] and prev["macd"] >= prev["macd_signal"]: sell_signals += 1
    if last["Close"] < last["bb_l"] and prev["Close"] >= prev["bb_l"]: buy_signals += 1
    if last["Close"] > last["bb_h"] and prev["Close"] <= prev["bb_h"]: sell_signals += 1
    if last["stoch_k"] > last["stoch_d"] and prev["stoch_k"] <= prev["stoch_d"] and last["stoch_k"] < 20: buy_signals += 1
    if last["stoch_k"] < last["stoch_d"] and prev["stoch_k"] >= prev["stoch_d"] and last["stoch_k"] > 80: sell_signals += 1
    direction = None
    if buy_signals >= bot_state['confidence_threshold'] and sell_signals == 0: direction = "صعود ⬆️"
    elif sell_signals >= bot_state['confidence_threshold'] and buy_signals == 0: direction = "هبوط ⬇️"
    if direction:
        return {
            "pair": pair, "timeframe": "5min", "entry_time": (datetime.now() + timedelta(seconds=30)).strftime("%H:%M:%S"),
            "direction": direction, "confidence": f"{max(buy_signals, sell_signals)}/4", "duration": "300 ثانية"
        }
    return None

async def send_signal_to_telegram(context: ContextTypes.DEFAULT_TYPE, signal: dict):
    message = (f"⚠️ **إشارة جديدة** ⚠️\n\n"
               f"**الزوج:** {signal['pair']}\n**الفريم:** {signal['timeframe']}\n"
               f"**وقت الدخول:** {signal['entry_time']}\n**الاتجاه:** {signal['direction']}\n"
               f"**قوة الإشارة:** {signal['confidence']} مؤشرات متوافقة\n**مدة الصفقة:** {signal['duration']}")
    await context.bot.send_message(chat_id=bot_state["chat_id"], text=message, parse_mode='Markdown')

async def check_for_signals(context: ContextTypes.DEFAULT_TYPE):
    global last_signal_candle
    if not bot_state["running"] or not bot_state['selected_pairs']: return
    now = datetime.now()
    if now.minute % 5 != 0: return
    candle_id_minute = now.minute - (now.minute % 5)
    current_candle_id = now.strftime(f'%Y-%m-%d %H:{candle_id_minute:02d}')
    logger.info(f"Checking for signals on candle: {current_candle_id}")
    for pair in bot_state['selected_pairs']:
        try:
            if last_signal_candle.get(pair) == current_candle_id:
                logger.info(f"Signal already sent for {pair} on this candle. Skipping.")
                continue
            data = await fetch_historical_data(pair)
            if not data.empty:
                signal = await analyze_and_generate_signal(data, pair)
                if signal:
                    await send_signal_to_telegram(context, signal)
                    last_signal_candle[pair] = current_candle_id
                    logger.info(f"Signal sent for {pair}. Storing candle_id: {current_candle_id}")
            await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"An error occurred while processing pair {pair}: {e}")
            await asyncio.sleep(5)

# --- إعداد وتشغيل البوت ---
def main() -> None:
    if not TOKEN:
        logger.critical("TELEGRAM_TOKEN environment variable not set.")
        return
    load_bot_settings()
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CallbackQueryHandler(add_pair_callback, pattern=r'^addpair_'))
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            SELECTING_ACTION: [
                MessageHandler(filters.Regex(r'^(حالة البوت:)'), toggle_bot_status),
                MessageHandler(filters.Regex(r'^اختيار الأزواج$'), select_pairs_menu),
                MessageHandler(filters.Regex(r'^الإعدادات ⚙️$'), settings_menu),
                MessageHandler(filters.Regex(r'^عرض الإعدادات الحالية$'), view_current_settings),
                MessageHandler(filters.Regex(r'^🔍 اكتشاف الأزواج النشطة$'), find_active_pairs_command),
            ],
            SELECTING_PAIR: [MessageHandler(filters.Regex(r'العودة إلى القائمة الرئيسية'), start), MessageHandler(filters.TEXT & ~filters.COMMAND, toggle_pair)],
            SETTINGS_MENU: [
                MessageHandler(filters.Regex(r'^تحديد عتبة الثقة$'), set_confidence_menu),
                MessageHandler(filters.Regex(r'^تعديل قيم المؤشرات$'), set_indicator_menu),
                MessageHandler(filters.Regex(r'العودة إلى القائمة الرئيسية'), start),
            ],
            SETTING_CONFIDENCE: [MessageHandler(filters.Regex(r'العودة إلى الإعدادات'), settings_menu), MessageHandler(filters.TEXT & ~filters.COMMAND, set_confidence_value)],
            SETTING_INDICATOR: [MessageHandler(filters.Regex(r'العودة إلى الإعدادات'), settings_menu), MessageHandler(filters.TEXT & ~filters.COMMAND, select_indicator_to_set)],
            AWAITING_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_new_value)],
        },
        fallbacks=[CommandHandler('start', start)],
    )
    application.add_handler(conv_handler)
    if bot_state.get('running'):
        application.job_queue.run_repeating(check_for_signals, interval=60, first=1, name='signal_check')
    logger.info("Bot is starting...")
    application.run_polling()

if __name__ == '__main__':
    main()
