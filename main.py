TYPE):
    if not bot_state.get("running") or not bot_state.get('selected_pairs'): return
    now = datetime.now()
    if now.minute % 5 != 0: return
    
    logger.info("Checking for potential signals on M5...")
    for pair in bot_state.get('selected_pairs', []):
        if pair in pending_signals: continue
        try:
            data = await fetch_historical_data(pair, "5min", 100)
            if data.empty: continue
            
            strength = await analyze_signal_strength(data)
            buy_strength, sell_strength = strength['buy'], strength['sell']
            
            direction = None
            if buy_strength >= bot_state.get('initial_confidence', 2) and sell_strength == 0:
                direction = "صعود"
            elif sell_strength >= bot_state.get('initial_confidence', 2) and buy_strength == 0:
                direction = "هبوط"
            
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
            
            await asyncio.sleep(5) # لتجنب استهلاك الـAPI بسرعة
        except Exception as e:
            await send_error_to_telegram(context, f"Error in check_for_signals for {pair}: {e}")

async def confirm_pending_signals(context: ContextTypes.DEFAULT_TYPE):
    if not bot_state.get("running") or not pending_signals: return
    now = datetime.now()
    
    if now.minute % 5 != 4 or now.second < 45: # نافذة التأكيد النهائية: آخر 15 ثانية من الشمعة
        return

    logger.info("Final confirmation window is open. Checking pending signals...")

    for pair, signal_info in list(pending_signals.items()):
        try:
            time_since_signal = (now - signal_info['timestamp']).total_seconds()
            if time_since_signal < 60: continue # تأكد من أن الإشارة من الشمعة السابقة

            # 1. تحليل القوة النهائية على فريم M5
            data_m5 = await fetch_historical_data(pair, "5min", 50)
            if data_m5.empty: raise Exception("Failed to fetch M5 data for final confirmation.")
            strength_m5 = await analyze_signal_strength(data_m5)
            
            # 2. فلتر الاتجاه العام على فريم M15
            data_m15 = await fetch_historical_data(pair, "15min", 50)
            if data_m15.empty: raise Exception("Failed to fetch M15 data for trend filter.")
            
            m15_ema_period = bot_state.get('indicator_params', {}).get('m15_ema_period', 20)
            ema_m15 = ta.trend.EMAIndicator(data_m15['Close'], window=m15_ema_period).ema_indicator().iloc[-1]
            last_close_m15 = data_m15['Close'].iloc[-1]

            m15_trend_ok = False
            if signal_info['direction'] == 'صعود' and last_close_m15 > ema_m15:
                m15_trend_ok = True
            elif signal_info['direction'] == 'هبوط' and last_close_m15 < ema_m15:
                m15_trend_ok = True

            # 3. القرار النهائي
            confirmed = False
            final_confidence_threshold = bot_state.get('final_confidence', 3)

            if m15_trend_ok:
                if signal_info['direction'] == 'صعود' and strength_m5['buy'] >= final_confidence_threshold and strength_m5['sell'] == 0:
                    confirmed = True
                elif signal_info['direction'] == 'هبوط' and strength_m5['sell'] >= final_confidence_threshold and strength_m5['buy'] == 0:
                    confirmed = True

            # 4. إرسال النتيجة
            if confirmed:
                confirmation_text = ( "✅✅✅   تــأكــيــد الــدخــول   ✅✅✅\n\n"
                                     f"الزوج: {pair} OTC\n"
                                     f"الاتجاه: {signal_info['direction']} {'⬆️' if signal_info['direction'] == 'صعود' else '⬇️'}\n\n"
                                     "          🔥 ادخــــــــل الآن 🔥")
                await context.bot.edit_message_text(chat_id=CHAT_ID, message_id=signal_info['message_id'], text=confirmation_text)
                logger.info(f"Signal CONFIRMED for {pair}")
            else:
                reason = "لم يتوافق مع اتجاه M15" if not m15_trend_ok else "ضعف تأكيد M5"
                cancellation_text = ("❌❌❌   إلــغــاء الــصــفــقــة   ❌❌❌\n\n"
                                     f"الزوج: {pair} OTC\n\n"
                                     f"السبب: {reason}. لا تقم بالدخول.")
                await context.bot.edit_message_text(chat_id=CHAT_ID, message_id=signal_info['message_id'], text=cancellation_text)
                logger.info(f"Signal CANCELED for {pair} due to: {reason}")
            
            del pending_signals[pair]

        except Exception as e:
            await send_error_to_telegram(context, f"Error in confirm_pending_signals for {pair}: {e}")
            if pair in pending_signals:
                try:
                    await context.bot.delete_message(chat_id=CHAT_ID, message_id=pending_signals[pair]['message_id'])
                except Exception as del_e:
                    logger.error(f"Could not delete message for canceled signal {pair}: {del_e}")
                del pending_signals[pair]

# --- إعداد وتشغيل البوت ---
def main() -> None:
    if not all([TOKEN, CHAT_ID, TWELVE_DATA_API_KEY]):
        logger.critical("One or more environment variables are missing (TOKEN, CHAT_ID, or API_KEY).")
        return
        
    load_bot_settings()
    
    persistence = PicklePersistence(filepath="bot_persistence")
    
    application = Application.builder().token(TOKEN).persistence(persistence).build()
    
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
            SELECTING_PAIR: [
                MessageHandler(filters.Regex(r'العودة إلى القائمة الرئيسية'), start), 
                MessageHandler(filters.TEXT & ~filters.COMMAND, toggle_pair)
            ],
            SETTINGS_MENU: [
                MessageHandler(filters.Regex(r'^تحديد عتبة الإشارة الأولية$'), set_confidence_menu),
                MessageHandler(filters.Regex(r'^تحديد عتبة التأكيد النهائي$'), set_confidence_menu),
                MessageHandler(filters.Regex(r'^تعديل قيم المؤشرات$'), set_indicator_menu),
                MessageHandler(filters.Regex(r'^📊 استراتيجية الماكد$'), set_macd_strategy_menu),
                MessageHandler(filters.Regex(r'^🔬 فحص اتصال API$'), check_api_connection),
                MessageHandler(filters.Regex(r'العودة إلى القائمة الرئيسية'), start),
            ],
            SETTING_MACD_STRATEGY: [
                MessageHandler(filters.Regex(r'العودة إلى الإعدادات'), settings_menu),
                MessageHandler(filters.TEXT & ~filters.COMMAND, set_macd_strategy_value)
            ],
            SETTING_CONFIDENCE: [
                MessageHandler(filters.Regex(r'العودة إلى الإعدادات'), settings_menu), 
                MessageHandler(filters.TEXT & ~filters.COMMAND, set_confidence_value)
            ],
            SETTING_INDICATOR: [
                MessageHandler(filters.Regex(r'العودة إلى الإعدادات'), settings_menu),
                MessageHandler(filters.Regex(r'^♻️ إعادة تعيين الكل للإعدادات الافتراضية$'), reset_indicators_to_default),
                MessageHandler(filters.TEXT & ~filters.COMMAND, select_indicator_to_set)
            ],
            AWAITING_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_new_value)],
        },
        fallbacks=[CommandHandler('start', start)],
        persistent=True, name="bot_conversation"
    )
    application.add_handler(conv_handler)
    
    if bot_state.get('running'):
        application.job_queue.run_repeating(check_for_signals, interval=60, first=1, name='signal_check')
        application.job_queue.run_repeating(confirm_pending_signals, interval=15, first=1, name='confirmation_check')
        
    logger.info("Bot is starting...")
    application.run_polling()

if __name__ == '__main__':
    main()
