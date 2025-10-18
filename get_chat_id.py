import asyncio
from aiogram import Bot

TOKEN ="8457374369:AAH0pNwVCLbCvSXwp86_Zap68uYbK_rEtLc"

async def main():
    bot = Bot(token=TOKEN)
    me = await bot.get_me()
    print(f"✅ Бот запущен: @{me.username}")
    print("👉 Теперь добавь его в канал и отправь туда любое сообщение (например 'тест')")
    print("После этого останови этот скрипт (Ctrl+C) и запусти снова — тогда ID появится.")
    updates = await bot.get_updates()
    for u in updates:
        if u.message:
            chat = u.message.chat
            print(f"➡️ ID чата: {chat.id}, название: {chat.title}")
    await bot.session.close()

asyncio.run(main())
