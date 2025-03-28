import os
import subprocess
import shutil
import telegram
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters
from datetime import datetime
import hashlib
import asyncio
import logging
import time

# Конфигурация
NDK_HOME = "/path/to/android-ndk-r16b"  # Замени на свой путь
MAX_SIZE = 200 * 1024 * 1024  # 200 МБ
MAX_CACHE_SIZE = 1 * 1024 * 1024 * 1024  # 1 ГБ для кэша
SUPPORTED_ABIS = ["armeabi-v7a", "arm64-v8a", "x86"]
CACHE_DIR = "cache"
LOG_DIR = "logs"

# Настройка логирования
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

# Разбиение файла
def split_file(file_path, chunk_size=50*1024*1024):
    parts = []
    with open(file_path, 'rb') as f:
        part_num = 1
        while chunk := f.read(chunk_size):
            part_path = f"{file_path}.part{part_num}"
            with open(part_path, 'wb') as part_file:
                part_file.write(chunk)
            parts.append(part_path)
            part_num += 1
    return parts

# Генерация Android.mk
def generate_android_mk(cpp_file, module_name="native", flags=""):
    return f"""\
LOCAL_PATH := $(call my-dir)
include $(CLEAR_VARS)
LOCAL_MODULE    := {module_name}
LOCAL_SRC_FILES := {cpp_file}
LOCAL_CFLAGS    := {flags}
include $(BUILD_SHARED_LIBRARY)
"""

# Генерация Application.mk
def generate_application_mk(abis):
    return f"APP_ABI := {' '.join(abis)}\n"

# Генерация хэша
def get_file_hash(file_path):
    hasher = hashlib.sha256()
    with open(file_path, 'rb') as f:
        hasher.update(f.read())
    return hasher.hexdigest()

# Управление кэшем
def get_cache_size():
    total_size = 0
    for root, _, files in os.walk(CACHE_DIR):
        for file in files:
            total_size += os.path.getsize(os.path.join(root, file))
    return total_size

def clean_cache():
    if get_cache_size() <= MAX_CACHE_SIZE:
        return
    cache_items = []
    for root, dirs, _ in os.walk(CACHE_DIR):
        for dir in dirs:
            path = os.path.join(root, dir)
            cache_items.append((os.path.getmtime(path), path))
    cache_items.sort()  # Сортировка по времени изменения (старые первыми)
    while get_cache_size() > MAX_CACHE_SIZE and cache_items:
        _, oldest = cache_items.pop(0)
        shutil.rmtree(oldest)
    logger.info("Кэш очищен до допустимого размера")

# Асинхронная компиляция для CMake
async def compile_cmake(temp_dir, abi, log_file, flags):
    build_dir = f"build_{abi}"
    os.makedirs(build_dir, exist_ok=True)
    os.chdir(build_dir)
    cmd = [
        "cmake", "..",
        f"-DCMAKE_TOOLCHAIN_FILE={NDK_HOME}/build/cmake/android.toolchain.cmake",
        f"-DANDROID_ABI={abi}",
        f"-DCMAKE_C_FLAGS={flags}"
    ]
    with open(log_file, "a") as log:
        subprocess.run(cmd, check=True, stdout=log, stderr=log)
        subprocess.run(["make"], check=True, stdout=log, stderr=log)
    os.chdir("..")

# Компиляция и отправка
async def compile_and_send(update, context, temp_dir, zip_path):
    chat_id = update.message.chat_id
    status_msg = context.bot.send_message(chat_id, "Обработка: 0%")
    log_file = os.path.join(LOG_DIR, f"compile_{chat_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")

    os.chdir(temp_dir)
    progress = 0

    # Проверяем кэш
    zip_hash = get_file_hash(zip_path)
    cache_path = os.path.join(CACHE_DIR, zip_hash)
    if os.path.exists(cache_path):
        context.bot.edit_message_text(chat_id=chat_id, message_id=status_msg.message_id, text="Найден кэш: 100%, отправляю файлы...")
        send_cached_files(context, chat_id, cache_path)
        clean_cache()
        return True
    progress += 20
    context.bot.edit_message_text(chat_id=chat_id, message_id=status_msg.message_id, text=f"Обработка: {progress}% (проверка файлов)")

    # Проверяем файлы
    java_files = os.listdir("src") if os.path.exists("src") else []
    cpp_files = os.listdir("jni") if os.path.exists("jni") else []
    has_android_mk = os.path.exists("Android.mk")
    has_application_mk = os.path.exists("Application.mk")
    has_cmake = os.path.exists("CMakeLists.txt")

    if not java_files or not cpp_files:
        context.bot.edit_message_text(chat_id=chat_id, message_id=status_msg.message_id, text="Ошибка: нет Java или C++ файлов в src/ или jni/. См. /help.")
        return False

    # Выбор архитектур и флагов
    abis = context.user_data.get("selected_abis", SUPPORTED_ABIS)
    flags = context.user_data.get("compile_flags", "")
    progress += 20
    context.bot.edit_message_text(chat_id=chat_id, message_id=status_msg.message_id, text=f"Обработка: {progress}% (подготовка)")

    # Компиляция
    so_files = []
    if has_cmake:
        context.bot.edit_message_text(chat_id=chat_id, message_id=status_msg.message_id, text=f"CMake: компилирую для {', '.join(abis)}...")
        tasks = [compile_cmake(temp_dir, abi, log_file, flags) for abi in abis]
        await asyncio.gather(*tasks)
        for abi in abis:
            for root, _, files in os.walk(f"build_{abi}"):
                for file in files:
                    if file.endswith(".so"):
                        so_files.append(os.path.join(root, file))
        progress = 80
    else:
        if not has_android_mk:
            cpp_file = cpp_files[0]
            with open("Android.mk", "w") as f:
                f.write(generate_android_mk(cpp_file, flags=flags))
        if not has_application_mk:
            with open("Application.mk", "w") as f:
                f.write(generate_application_mk(abis))

        context.bot.edit_message_text(chat_id=chat_id, message_id=status_msg.message_id, text=f"ndk-build: компилирую для {', '.join(abis)}...")
        with open(log_file, "a") as log:
            try:
                subprocess.run([f"{NDK_HOME}/ndk-build"], check=True, stdout=log, stderr=log)
            except subprocess.CalledProcessError:
                context.bot.edit_message_text(chat_id=chat_id, message_id=status_msg.message_id, text=f"Ошибка компиляции. Лог: {log_file}")
                return False
        for abi in abis:
            so_path = f"libs/{abi}/libnative.so"
            if os.path.exists(so_path):
                so_files.append(so_path)
        progress = 80

    if not so_files:
        context.bot.edit_message_text(chat_id=chat_id, message_id=status_msg.message_id, text=f"Ошибка: .so файлы не созданы. Лог: {log_file}")
        return False

    # Кэшируем
    os.makedirs(cache_path, exist_ok=True)
    for so_file in so_files:
        shutil.copy(so_file, cache_path)
    clean_cache()
    progress += 10
    context.bot.edit_message_text(chat_id=chat_id, message_id=status_msg.message_id, text=f"Обработка: {progress}% (кэширование)")

    # Отправляем
    context.bot.edit_message_text(chat_id=chat_id, message_id=status_msg.message_id, text="Компиляция завершена: 100%, отправляю файлы...")
    for so_file in so_files:
        file_size = os.path.getsize(so_file)
        if file_size > MAX_SIZE:
            context.bot.send_message(chat_id, f"Ошибка: {so_file} ({file_size / 1024 / 1024:.2f} МБ) превышает лимит 200 МБ.")
            continue
        if file_size <= 50 * 1024 * 1024:
            with open(so_file, 'rb') as f:
                context.bot.send_document(chat_id, document=f, filename=os.path.basename(so_file))
        else:
            parts = split_file(so_file)
            for part in parts:
                with open(part, 'rb') as f:
                    context.bot.send_document(chat_id, document=f, filename=os.path.basename(part))
            context.bot.send_message(chat_id, f"Собери {os.path.basename(so_file)}: `cat {os.path.basename(so_file)}.part* > {os.path.basename(so_file)}`")

    return True

def send_cached_files(context, chat_id, cache_path):
    for file in os.listdir(cache_path):
        file_path = os.path.join(cache_path, file)
        file_size = os.path.getsize(file_path)
        if file_size <= 50 * 1024 * 1024:
            with open(file_path, 'rb') as f:
                context.bot.send_document(chat_id, document=f, filename=file)
        else:
            parts = split_file(file_path)
            for part in parts:
                with open(part, 'rb') as f:
                    context.bot.send_document(chat_id, document=f, filename=os.path.basename(part))
            context.bot.send_message(chat_id, f"Собери {file}: `cat {file}.part* > {file}`")

# Обработчики
def set_abi(update, context):
    args = context.args
    if not args:
        update.message.reply_text(f"Укажи архитектуры: /setabi armeabi-v7a arm64-v8a\nДоступные: {', '.join(SUPPORTED_ABIS)}")
        return
    selected_abis = [abi for abi in args if abi in SUPPORTED_ABIS]
    if not selected_abis:
        update.message.reply_text("Ошибка: неверные архитектуры. Доступные: " + ", ".join(SUPPORTED_ABIS))
        return
    context.user_data["selected_abis"] = selected_abis
    update.message.reply_text("Выбраны: " + ", ".join(selected_abis))

def set_flags(update, context):
    flags = " ".join(context.args)
    if not flags:
        update.message.reply_text("Укажи флаги: /setflags -O2 -Wall")
        return
    context.user_data["compile_flags"] = flags
    update.message.reply_text(f"Установлены флаги: {flags}")

def start_compilation(update, context):
    update.message.reply_text(
        "Пришли zip-архив с проектом JNI:\n"
        "- src/Main.java (с native-методами)\n"
        "- jni/native.cpp (реализация)\n"
        "- (опционально) Android.mk/Application.mk или CMakeLists.txt\n"
        "Команды: /setabi, /setflags, /clearcache, /help"
    )
    context.user_data["waiting_for_files"] = True

def handle_files(update, context):
    if not context.user_data.get("waiting_for_files"):
        return
    loop = asyncio.get_event_loop()
    temp_dir = f"temp_project_{update.message.chat_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    os.makedirs(temp_dir)
    zip_path = update.message.document.get_file().download(os.path.join(temp_dir, "project.zip"))
    try:
        subprocess.run(["unzip", zip_path, "-d", temp_dir], check=True)
    except subprocess.CalledProcessError:
        context.bot.send_message(update.message.chat_id, "Ошибка: не удалось распаковать архив.")
        shutil.rmtree(temp_dir)
        return
    success = loop.run_until_complete(compile_and_send(update, context, temp_dir, zip_path))
    os.chdir("..")
    shutil.rmtree(temp_dir)
    context.user_data["waiting_for_files"] = False
    if success:
        context.bot.send_message(update.message.chat_id, "Готово!")

def clear_cache(update, context):
    if os.path.exists(CACHE_DIR):
        shutil.rmtree(CACHE_DIR)
        os.makedirs(CACHE_DIR)
        update.message.reply_text("Кэш очищен.")
    else:
        update.message.reply_text("Кэш уже пуст.")

def help_command(update, context):
    update.message.reply_text(
        "Пример структуры архива:\n"
        "project.zip/\n"
        "├── src/Main.java\n"
        "├── jni/native.cpp\n"
        "├── (опционально) Android.mk\n"
        "├── (опционально) Application.mk\n"
        "├── (или) CMakeLists.txt\n\n"
        "Команды:\n"
        "/setabi <abis> — выбрать архитектуры\n"
        "/setflags <flags> — установить флаги компиляции\n"
        "/compile — начать компиляцию\n"
        "/clearcache — очистить кэш\n"
        "/help — это сообщение"
    )

def check_environment():
    if not os.path.exists(NDK_HOME):
        logger.error(f"NDK не найден в {NDK_HOME}")
        exit(1)
    for cmd in ["cmake", "unzip"]:
        if subprocess.call(["which", cmd], stdout=subprocess.PIPE, stderr=subprocess.PIPE) != 0:
            logger.error(f"{cmd} не установлен")
            exit(1)
    logger.info("Окружение готово")

def main():
    check_environment()
    if not os.path.exists(CACHE_DIR):
        os.makedirs(CACHE_DIR)
    updater = Updater("YOUR_BOT_TOKEN")  # Замени на свой токен
    dp = updater.dispatcher
    dp.add_handler(CommandHandler("setabi", set_abi))
    dp.add_handler(CommandHandler("setflags", set_flags))
    dp.add_handler(CommandHandler("compile", start_compilation))
    dp.add_handler(CommandHandler("clearcache", clear_cache))
    dp.add_handler(CommandHandler("help", help_command))
    dp.add_handler(MessageHandler(Filters.document, handle_files))
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()