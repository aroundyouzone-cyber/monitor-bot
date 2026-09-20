 ('skip', skip_new_obj),
                MessageHandler(filters.TEXT & ~filters.COMMAND, new_object_text_handler),
            ],
            PLAN_MENU: [
                CallbackQueryHandler(plan_menu_callback, pattern='^plan_'),
                MessageHandler(filters.TEXT & ~filters.COMMAND, main_menu_handler),
            ],
            PLAN_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, plan_text_input),
            ],
            PLAN_DATE: [
                CallbackQueryHandler(plan_date_callback, pattern='^plandate_'),
                MessageHandler(filters.TEXT & ~filters.COMMAND, plan_date_text),
            ],
            PLAN_TIME: [
                CallbackQueryHandler(plan_time_callback, pattern='^plantime_'),
                MessageHandler(filters.TEXT & ~filters.COMMAND, plan_time_text),
            ],
        },
        fallbacks=[
            CommandHandler('cancel', cancel),
            CommandHandler('start', start),
        MessageHandler(filters.TEXT & ~filters.COMMAND, start),],
    )

    app.add_handler(conv_handler)
    # Report callback (outside conversation for /start re-entry)
    app.add_handler(CallbackQueryHandler(report_callback, pattern='^rep_'))
    # Ручна резервна копія на вимогу
    app.add_handler(CommandHandler('backup', backup_command))
    # Ручна синхронізація з Google-таблицею на вимогу
    app.add_handler(CommandHandler('syncsheets', sync_sheets_command))

    if app.job_queue:
        app.job_queue.run_daily(check_daily_reminder, time=dtime(hour=20, minute=0))
        app.job_queue.run_daily(morning_plan_digest, time=dtime(hour=8, minute=0))
        app.job_queue.run_daily(daily_backup_job, time=dtime(hour=2, minute=0))
        app.job_queue.run_repeating(sync_sheets_job, interval=900, first=60)
    else:
        logger.error("JobQueue недоступний — нагадування вимкнено (потрібен пакет python-telegram-bot[job-queue])")

    logger.info("Бот запущено! ✅")
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()


