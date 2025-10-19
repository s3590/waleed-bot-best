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
CHAT_ID_STR = os.environ.get('TELEGRAM_CHAT_ID')
CHAT_ID = int(CHAT_ID_STR) if CHAT_ID_STR else None
TWELVE_DATA_API_KEY = os.environ.get('TWELVE_DATA_API_KEY')

# --- إعدادات التسجيل ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- قائمة الأزواج المعتمدة ---
USER_DEFINED_PAIRS = [
    "EUR/USD", "AED/CNY", "USD/JPY", "USD/CHF", "AUD/USD", "USD/CAD",
    "EUR/JPY", "AUD/JPY", "CHF/JPY", "EUR/CHF", "AUD/CHF", "CAD/CHF",
    "EUR/AUD", "EUR/CAD", "AUD/CAD", "CAD/JPY"
]

# --- الإعدادات الافتراضية ---
DEFAULT_SETTINGS = {
    'running': False, 'selected_pairs': [],
    'initial_confidence': 2,
    'final_confidence': 3,
    'indicator_params': {
        'rsi_period': 14, 'macd_fast': 12, 'macd_slow': 26, 'macd_signal': 9,
        'bollinger_period': 20, 'stochastic_period': 14, 'atr_period': 14, 'adx_period': 14
    }
}

# --- حالة البوت وذاكرة الإشارات ---
bot_state = DEFAULT_SETTINGS.copy()
bot_state.update({'chat_id': CHAT_ID, 'twelve_data_api_key': TWELVE_DATA_API_KEY})
pending_signals = {}  # { 'pair': { 'direction': '...', 'message_id': ..., 'timestamp': ... } }

# --- دوال المساعدة ---
async def send_error_to_telegram(context: ContextTypes.DEFAULT_TYPE, error_message: str):
    logger.error(error_message)
    if CHAT_ID:
        try:
            await context.bot.send_message(chat_id=CHAT_ID, text=f"🤖⚠️ **حدث خطأ في البوت** ⚠️🤖\n\n**التفاصيل:**\n`{error_message}`", parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Could not send error message to Telegram: {e}")

STATE_FILE = 'bot_settings.json'
def save_bot_settings():
    settings_to_save = {k: v for k, v in bot_state.items() if k in DEFAULT_SETTINGS}
    with open(STATE_FILE, 'w') as f: json.dump(settings_to_save, f, indent=4)
    logger.info("Bot settings saved.")

def load_bot_settings():
    global bot_state
    try:
        with open(STATE_FILE, 'r') as f:
            loaded_settings = json.load(f)
            # التأكد من أن جميع المفاتيح الافتراضية موجودة
            bot_state.update(DEFAULT_SETTINGS)
            bot_state.update(loaded_settings)
            # التأكد من أن جميع قيم المؤشرات موجودة
            if 'indicator_params' not in bot_state:
                bot_state['indicator_params'] = DEFAULT_SETTINGS['indicator_params'].copy()
            else:
                 # دمج القيم المحملة مع ضمان وجود كل المفاتيح الافتراضية
                default_params = DEFAULT_SETTINGS['indicator_params'].copy()
                default_params.update(bot_state['indicator_params'])
                bot_state['indicator_params'] = default_params

        logger.info("Bot settings loaded.")
    except (FileNotFoundError, json.JSONDecodeError):
        logger.warning("Settings file not found or invalid. Starting with default settings.")
        bot_state = DEFAULT_SETTINGS.copy() # إعادة تعيين كامل إذا كان الملف تالفًا
        save_bot_settings()


# --- حالات المحادثة ---
(SELECTING_ACTION, SELECTING_PAIR, SETTINGS_MENU, SETTING_CONFIDENCE, SETTING_INDICATOR, AWAITING_VALUE) = range(6)

# --- واجهة المستخدم ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_name = update.effective_user.first_name
    message = (f"أهلاً بك يا {user_name} في ALNUSIRY BOT {{ VIP }} 👋\n\n"
               "مساعدك الذكي لإشارات التداول.\n\n"
               "استخدم الأزرار أدناه للتحكم.")
    await update.message.reply_text(message)
    return await send_main_menu(update, context)

async def send_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, message_text: str = 'القائمة الرئيسية:') -> int:
    status = "يعمل ✅" if bot_state['running'] else "متوقف ❌"
    main_menu_keyboard = [
        [KeyboardButton(f"حالة البوت: {status}")],
        [KeyboardButton("اختيار الأزواج"), KeyboardButton("الإعدادات ⚙️")],
        [KeyboardButton("🔍 اكتشاف الأزواج النشطة")],
        [KeyboardButton("عرض الإعدادات الحالية")]
    ]
    reply_markup = ReplyKeyboardMarkup(main_menu_keyboard, resize_keyboard=True)
    
    # نرسل لوحة المفاتيح مع رسالة فقط إذا لم تكن الرسالة الترحيبية
    is_start_command = update.message.text and update.message.text.startswith('/start')
    if not is_start_command:
        await update.message.reply_text(message_text, reply_markup=reply_markup)
    else:
        await update.message.reply_text("القائمة الرئيسية:", reply_markup=reply_markup)

    return SELECTING_ACTION

async def toggle_bot_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    was_running = bot_state['running']
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
    settings_keyboard = [
        [KeyboardButton("تحديد عتبة الإشارة الأولية")],
        [KeyboardButton("تحديد عتبة التأكيد النهائي")],
        [KeyboardButton("تعديل قيم المؤشرات")],
        [KeyboardButton("العودة إلى القائمة الرئيسية")]
    ]
    reply_markup = ReplyKeyboardMarkup(settings_keyboard, resize_keyboard=True)
    await update.message.reply_text("اختر الإعداد الذي تريد تعديله:", reply_markup=reply_markup)
    return SETTINGS_MENU

async def set_confidence_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['setting_type'] = 'initial' if 'الأولية' in update.message.text else 'final'
    setting_key = 'initial_confidence' if context.user_data['setting_type'] == 'initial' else 'final_confidence'
    current = bot_state[setting_key]
    
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
    await update.message.reply_text(f"تم تحديث عتبة {title} إلى: {bot_state[setting_key]}")
    
    update.message.text = f"تحديد عتبة {title}"
    return await set_confidence_menu(update, context)

async def set_indicator_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    params = bot_state['indicator_params']
    keyboard = [[KeyboardButton(f"{key.replace('_', ' ').title()} ({value})")] for key, value in params.items()]
    keyboard.append([KeyboardButton("♻️ إعادة تعيين الكل للإعدادات الافتراضية")]) # الزر الجديد
    keyboard.append([KeyboardButton("العودة إلى الإعدادات")])
    await update.message.reply_text("اختر المؤشر الذي تريد تعديل قيمته، أو قم بإعادة التعيين:", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
    return SETTING_INDICATOR

async def reset_indicators_to_default(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    bot_state['indicator_params'] = DEFAULT_SETTINGS['indicator_params'].copy()
    save_bot_settings()
    await update.message.reply_text("✅ تم استعادة الإعدادات الافتراضية لجميع المؤشرات.")
    return await set_indicator_menu(update, context)

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
               f"**عتبة الإشارة الأولية:** {bot_state['initial_confidence']} مؤشرات\n"
               f"**عتبة التأكيد النهائي:** {bot_state['final_confidence']} مؤشرات\n\n"
               f"**قيم المؤشرات:**\n" +
               "\n".join([f"- {key.replace('_', ' ').title()}: {value}" for key, value in params.items()]))
    await update.message.reply_text(message, parse_mode='Markdown')
    return SELECTING_ACTION

# --- اكتشاف الأزواج النشطة ---
async def analyze_pair_activity(pair: str, context: ContextTypes.DEFAULT_TYPE) -> dict or None:
    try:
        data = await fetch_historical_data(pair, 100)
        params = bot_state['indicator_params']
        if data.empty or len(data) < max(params['adx_period'], params['atr_period']): return None
        adx_value = ta.trend.ADXIndicator(data['High'], data['Low'], data['Close'], window=params['adx_period']).adx().iloc[-1]
        atr_value = ta.volatility.AverageTrueRange(data['High'], data['Low'], data['Close'], window=params['atr_period']).average_true_range().iloc[-1]
        atr_percent = (atr_value / data['Close'].iloc[-1]) * 100
        return {'pair': pair, 'adx': adx_value, 'atr_percent': atr_percent}
    except Exception as e:
        await send_error_to_telegram(context, f"Error analyzing activity for {pair}: {e}")
        return None

async def find_active_pairs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("🔍 جاري تحليل نشاط السوق... هذه العملية ستحترم حدود الـ API وقد تستغرق بضع دقائق.", reply_markup=ReplyKeyboardMarkup([[]], resize_keyboard=True))
    all_results = []
    for pair in USER_DEFINED_PAIRS:
        try:
            logger.info(f"Analyzing activity for pair: {pair}")
            result = await analyze_pair_activity(pair, context)
            if result: all_results.append(result)
            await asyncio.sleep(8)
        except Exception as e:
            await send_error_to_telegram(context, f"Error during active pair discovery for {pair}: {e}")
            await asyncio.sleep(8)
    if not all_results:
        return await send_main_menu(update, context, "عذرًا، لم أتمكن من تحليل السوق. تحقق من سجلات الأخطاء.")
    all_results.sort(key=lambda x: x['adx'] + (x['atr_percent'] * 20), reverse=True)
    top_pairs = all_results[:4]
    message = "📈 **أفضل الأزواج النشطة للتداول الآن:**\n\n"
    keyboard = []
    for res in top_pairs:
        reason = "اتجاه قوي" if res['adx'] > 25 else "تقلب جيد" if res['atr_percent'] > 0.04 else "نشاط معتدل"
        message += f"• **{res['pair']}** ({reason})\n"
        keyboard.append([InlineKeyboardButton(f"✅ تفعيل مراقبة {res['pair']}", callback_data=f"addpair_{res['pair']}")])
    keyboard.append([InlineKeyboardButton("➕ تفعيل مراقبة الكل", callback_data="addpairall_" + ",".join([p['pair'] for p in top_pairs]))])
    await update.message.reply_text(message, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return await send_main_menu(update, context, message_text="اختر إجراءً آخر من القائمة الرئيسية:")

async def add_pair_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    action, payload = query.data.split('_', 1)
    pairs_to_add = payload.split(',') if action == 'addpairall' else [payload]
    added_now = [pair for pair in pairs_to_add if pair not in bot_state['selected_pairs']]
    if added_now:
        bot_state['selected_pairs'].extend(added_now)
        save_bot_settings()
        await query.edit_message_text(text=f"تم تفعيل المراقبة للأزواج:\n{', '.join(added_now)}")
    else:
        await query.edit_message_text(text="الأزواج المحددة مفعلة بالفعل.")

# --- منطق التحليل والإشارات بنظام التأكيد ---
async def fetch_historical_data(pair: str, outputsize: int = 100) -> pd.DataFrame:
    api_key = bot_state["twelve_data_api_key"]
    if not api_key: return pd.DataFrame()
    url = f"https://api.twelvedata.com/time_series?symbol={pair}&interval=5min&outputsize={outputsize}&apikey={api_key}"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        if "values" in data:
            df = pd.DataFrame(data["values"])
            df["datetime"] = pd.to_datetime(df["datetime"])
            df = df.set_index("datetime").astype(float)
            df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close"}, inplace=True)
            return df.sort_index()
        return pd.DataFrame()
    except requests.exceptions.RequestException as e:
        logger.error(f"Network error fetching data for {pair}: {e}")
        return pd.DataFrame()

async def analyze_signal_strength(data: pd.DataFrame) -> dict:
    params = bot_state['indicator_params']
    if data.empty or len(data) < max(params.values()): return {'buy': 0, 'sell': 0}
    data["rsi"] = ta.momentum.RSIIndicator(data["Close"], window=params['rsi_period']).rsi()
    macd = ta.trend.MACD(data["Close"], window_fast=params['macd_fast'], window_slow=params['macd_slow'], window_sign=params['macd_signal'])
    data["macd"], data["macd_signal"] = macd.macd(), macd.macd_signal()
    bollinger = ta.volatility.BollingerBands(data["Close"], window=params['bollinger_period'])
    data["bb_h"], data["bb_l"] = bollinger.bollinger_hband(), bollinger.bollinger_lband()
    stoch = ta.momentum.StochasticOscillator(data["High"], data["Low"], data["Close"], window=params['stochastic_period'])
    data["stoch_k"], data["stoch_d"] = stoch.stoch(), stoch.stoch_signal()
    data.dropna(inplace=True)
    if data.empty or len(data) < 2: return {'buy': 0, 'sell': 0}
    last, prev = data.iloc[-1], data.iloc[-2]
    buy_signals, sell_signals = 0, 0
    if last["rsi"] < 35: buy_signals += 1
    if last["rsi"] > 30 and prev["rsi"] <= 30: buy_signals += 1
    if last["rsi"] > 65: sell_signals += 1
    if last["rsi"] < 70 and prev["rsi"] >= 70: sell_signals += 1
    if last["macd"] > last["macd_signal"] and last["macd"] < 0: buy_signals += 1
    if last["macd"] > last["macd_signal"] and prev["macd"] <= prev["macd_signal"]: buy_signals += 1
    if last["macd"] < last["macd_signal"] and last["macd"] > 0: sell_signals += 1
    if last["macd"] < last["macd_signal"] and prev["macd"] >= prev["macd_signal"]: sell_signals += 1
    if last["Close"] < last["bb_l"]: buy_signals += 1
    if last["Close"] > last["bb_h"]: sell_signals += 1
    if last["stoch_k"] > last["stoch_d"] and last["stoch_k"] < 30: buy_signals += 1
    if last["stoch_k"] > last["stoch_d"] and prev["stoch_k"] <= prev["stoch_d"] and last["stoch_k"] < 30: buy_signals += 1
    if last["stoch_k"] < last["stoch_d"] and last["stoch_k"] > 70: sell_signals += 1
    if last["stoch_k"] < last["stoch_d"] and prev["stoch_k"] >= prev["stoch_d"] and last["stoch_k"] > 70: sell_signals += 1
    return {'buy': buy_signals, 'sell': sell_signals}

async def check_for_signals(context: ContextTypes.DEFAULT_TYPE):
    if not bot_state["running"] or not bot_state['selected_pairs']: return
    now = datetime.now()
    if now.minute % 5 != 0: return
    logger.info("Checking for potential signals...")
    for pair in bot_state['selected_pairs']:
        if pair in pending_signals: continue
        try:
            data = await fetch_historical_data(pair)
            if data.empty: continue
            strength = await analyze_signal_strength(data)
            buy_strength, sell_strength = strength['buy'], strength['sell']
            direction = None
            if buy_strength >= bot_state['initial_confidence'] and sell_strength == 0: direction = "صعود"
            elif sell_strength >= bot_state['initial_confidence'] and buy_strength == 0: direction = "هبوط"
            if direction:
                entry_time = (now + timedelta(minutes=5) - timedelta(seconds=now.second)).strftime("%H:%M:00")
                direction_emoji = "🟢" if direction == "صعود" else "🔴"
                direction_arrow = "⬆️" if direction == "صعود" else "⬇️"
                signal_text = (f"   🔔   {direction_emoji} {{  اشارة   {direction}  }} {direction_emoji}   🔔       \n"
                               f"           📊 الزوج :  {pair} OTC\n"
                               f"           🕛  الفريم :  M5\n"
                               f"           📉  الاتجاه:  {direction} {direction_arrow}\n"
                               f"           ⏳ وقت الدخول : {entry_time}\n\n"
                               f"               🔍 {{  انتظر   التاكيد   }}")
                sent_message = await context.bot.send_message(chat_id=CHAT_ID, text=signal_text)
                pending_signals[pair] = {'direction': direction, 'message_id': sent_message.message_id, 'timestamp': now}
                logger.info(f"Potential signal found for {pair}. Awaiting confirmation.")
            await asyncio.sleep(5)
        except Exception as e:
            await send_error_to_telegram(context, f"Error in check_for_signals for {pair}: {e}")

async def confirm_pending_signals(context: ContextTypes.DEFAULT_TYPE):
    if not bot_state["running"] or not pending_signals: return
    now = datetime.now()
    for pair, signal_info in list(pending_signals.items()):
        try:
            time_since_signal = (now - signal_info['timestamp']).total_seconds()
            if 30 < time_since_signal < 75:
                data = await fetch_historical_data(pair, 50)
                if data.empty: continue
                strength = await analyze_signal_strength(data)
                buy_strength, sell_strength = strength['buy'], strength['sell']
                confirmed = False
                if signal_info['direction'] == 'صعود' and buy_strength >= bot_state['final_confidence'] and sell_strength == 0:
                    confirmed = True
                    confirmation_text = ( "✅✅✅   تــأكــيــد الــدخــول   ✅✅✅\n\n"
                                         f"الزوج: {pair} OTC\n"
                                         "الاتجاه: صعود ⬆️\n\n"
                                         "          🔥 ادخــــــــل الآن 🔥")
                elif signal_info['direction'] == 'هبوط' and sell_strength >= bot_state['final_confidence'] and buy_strength == 0:
                    confirmed = True
                    confirmation_text = ("✅✅✅   تــأكــيــد الــدخــول   ✅✅✅\n\n"
                                         f"الزوج: {pair} OTC\n"
                                         "الاتجاه: هبوط ⬇️\n\n"
                                         "          🔥 ادخــــــــل الآن 🔥")
                if confirmed:
                    await context.bot.delete_message(chat_id=CHAT_ID, message_id=signal_info['message_id'])
                    await context.bot.send_message(chat_id=CHAT_ID, text=confirmation_text)
                    logger.info(f"Signal CONFIRMED for {pair}")
                    del pending_signals[pair]
                    continue
            if time_since_signal >= 75:
                cancellation_text = ("❌❌❌   إلــغــاء الــصــفــقــة   ❌❌❌\n\n"
                                     f"الزوج: {pair} OTC\n\n"
                                     "الشروط لم تعد مثالية، لا تقم بالدخول.")
                await context.bot.delete_message(chat_id=CHAT_ID, message_id=signal_info['message_id'])
                await context.bot.send_message(chat_id=CHAT_ID, text=cancellation_text)
                logger.info(f"Signal CANCELED for {pair} due to timeout.")
                del pending_signals[pair]
        except Exception as e:
            await send_error_to_telegram(context, f"Error in confirm_pending_signals for {pair}: {e}")
            if pair in pending_signals: del pending_signals[pair]

# --- إعداد وتشغيل البوت ---
def main() -> None:
    if not all([TOKEN, CHAT_ID, TWELVE_DATA_API_KEY]):
        logger.critical("One or more environment variables are missing.")
        return
    load_bot_settings()
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CallbackQueryHandler(add_pair_callback, pattern=r'^addpair'))
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
                MessageHandler(filters.Regex(r'^تحديد عتبة الإشارة الأولية$'), set_confidence_menu),
                MessageHandler(filters.Regex(r'^تحديد عتبة التأكيد النهائي$'), set_confidence_menu),
                MessageHandler(filters.Regex(r'^تعديل قيم المؤشرات$'), set_indicator_menu),
                MessageHandler(filters.Regex(r'العودة إلى القائمة الرئيسية'), start),
            ],
            SETTING_CONFIDENCE: [MessageHandler(filters.Regex(r'العودة إلى الإعدادات'), settings_menu), MessageHandler(filters.TEXT & ~filters.COMMAND, set_confidence_value)],
            SETTING_INDICATOR: [
                MessageHandler(filters.Regex(r'العودة إلى الإعدادات'), settings_menu),
                MessageHandler(filters.Regex(r'^♻️ إعادة تعيين الكل للإعدادات الافتراضية$'), reset_indicators_to_default), # معالج الزر الجديد
                MessageHandler(filters.TEXT & ~filters.COMMAND, select_indicator_to_set)
            ],
            AWAITING_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_new_value)],
        },
        fallbacks=[CommandHandler('start', start)],
        persistent=True, name="bot_conversation"

    )
    application.add_handler(conv_handler)
    
    # إعادة جدولة المهام إذا كان البوت يعمل قبل إعادة التشغيل
    if bot_state.get('running'):
        application.job_queue.run_repeating(check_for_signals, interval=60, first=1, name='signal_check')
        application.job_queue.run_repeating(confirm_pending_signals, interval=15, first=1, name='confirmation_check')
        
    logger.info("Bot is starting...")
    application.run_polling()

if __name__ == '__main__':
    main()
